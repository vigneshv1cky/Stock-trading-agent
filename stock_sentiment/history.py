"""Persistent history storage for predictions, alerts, and backtesting.

Supports dual backends:
- DynamoDB: For production AWS deployments (ENV=PROD)
- SQLite: For local development (default)
"""

import os
import json
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Set, Any

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError

# --- Helper Utilities ---

def _to_decimal(obj):
    """Recursively convert floats to Decimals for DynamoDB."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj

def _from_decimal(obj):
    """Recursively convert Decimals to floats when reading from DynamoDB."""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _from_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_from_decimal(v) for v in obj]
    return obj

# --- Storage Interface & Implementations ---

class BaseStorage:
    def save_run(self, predictions: list, min_return: float, top_n: int, trigger_type: str = "MANUAL") -> str: raise NotImplementedError
    def get_latest_run(self) -> Optional[dict]: raise NotImplementedError
    def get_predictions_for_run(self, run_id: str) -> list[dict]: raise NotImplementedError
    def get_previous_predictions(self, symbol: str, limit: int = 5) -> list[dict]: raise NotImplementedError
    def save_outcome(self, prediction_id: str, symbol: str, price_at_pred: float, prices: dict, prediction_correct: bool): raise NotImplementedError
    def get_outcomes(self, limit: int = 100) -> list[dict]: raise NotImplementedError
    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]: raise NotImplementedError
    def save_alert(self, alert_type: str, symbol: str, message: str, prediction: str = "", score: float = 0, price: float = 0): raise NotImplementedError
    def get_unseen_alerts(self) -> list[dict]: raise NotImplementedError
    def mark_alerts_seen(self): raise NotImplementedError
    def get_recent_alerts(self, hours: int = 24) -> list[dict]: raise NotImplementedError
    def get_backtest_stats(self) -> dict: raise NotImplementedError
    def get_all_symbols_from_last_run(self) -> set[str]: raise NotImplementedError
    def get_prediction_by_symbol_and_run(self, symbol: str, run_id: str) -> Optional[dict]: raise NotImplementedError
    def save_heartbeat(self, status: str, message: str = ""): raise NotImplementedError
    def get_heartbeat(self) -> Optional[dict]: raise NotImplementedError
    def close(self): pass

class DynamoDBStorage(BaseStorage):
    def __init__(self, region_name="us-east-1"):
        self.dynamodb = boto3.resource('dynamodb', region_name=region_name)
        env_prefix = os.environ.get("ENV", "DEV").upper()
        self.runs_table_name = f"{env_prefix}_StockScreenerRuns"
        self.preds_table_name = f"{env_prefix}_StockScreenerPredictions"
        self.alerts_table_name = f"{env_prefix}_StockScreenerAlerts"
        self.status_table_name = f"{env_prefix}_StockScreenerStatus"

        self._init_schema()
        
        self.runs_table = self.dynamodb.Table(self.runs_table_name)
        self.preds_table = self.dynamodb.Table(self.preds_table_name)
        self.alerts_table = self.dynamodb.Table(self.alerts_table_name)
        self.status_table = self.dynamodb.Table(self.status_table_name)

    def _init_schema(self):
        try:
            existing_tables = [t.name for t in self.dynamodb.tables.all()]
        except (NoCredentialsError, BotoCoreError):
            print("WARNING: AWS credentials not found. DynamoDB storage unavailable.")
            raise

        tables_to_create = []
        if self.runs_table_name not in existing_tables:
            tables_to_create.append({
                'TableName': self.runs_table_name,
                'KeySchema': [{'AttributeName': 'run_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'run_id', 'AttributeType': 'S'}],
                'BillingMode': 'PAY_PER_REQUEST'
            })
        if self.preds_table_name not in existing_tables:
            tables_to_create.append({
                'TableName': self.preds_table_name,
                'KeySchema': [{'AttributeName': 'symbol', 'KeyType': 'HASH'}, {'AttributeName': 'predicted_at', 'KeyType': 'RANGE'}],
                'AttributeDefinitions': [{'AttributeName': 'symbol', 'AttributeType': 'S'}, {'AttributeName': 'predicted_at', 'AttributeType': 'S'}],
                'BillingMode': 'PAY_PER_REQUEST'
            })
        if self.alerts_table_name not in existing_tables:
            tables_to_create.append({
                'TableName': self.alerts_table_name,
                'KeySchema': [{'AttributeName': 'alert_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'alert_id', 'AttributeType': 'S'}],
                'BillingMode': 'PAY_PER_REQUEST'
            })
        if self.status_table_name not in existing_tables:
            tables_to_create.append({
                'TableName': self.status_table_name,
                'KeySchema': [{'AttributeName': 'system_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'system_id', 'AttributeType': 'S'}],
                'BillingMode': 'PAY_PER_REQUEST'
            })
            
        for config in tables_to_create:
            table = self.dynamodb.create_table(**config)
            table.wait_until_exists()

    def save_run(self, predictions: list, min_return: float, top_n: int, trigger_type: str = "MANUAL") -> str:
        print(f"[DynamoDB] Saving {trigger_type} run with {len(predictions)} predictions...")
        now = datetime.now(timezone.utc).isoformat()
        run_id = now
        self.runs_table.put_item(Item=_to_decimal({
            'run_id': run_id, 'run_at': now, 'min_return': min_return,
            'stock_count': len(predictions), 'top_n': top_n, 'trigger_type': trigger_type
        }))
        with self.preds_table.batch_writer() as batch:
            for p in predictions:
                item = {
                    'symbol': p.symbol, 'predicted_at': now, 'run_id': run_id,
                    'price_at_prediction': p.current_price, 'prediction': p.prediction,
                    'confidence': p.confidence, 'overall_score': p.overall_score,
                    'momentum_score': p.momentum_score, 'sentiment_score': p.sentiment_score,
                    'technical_score': p.technical_score, 'change_3m_pct': p.change_3m_pct,
                    'change_1m_pct': p.change_1m_pct, 'change_1w_pct': p.change_1w_pct,
                    'avg_sentiment': p.avg_sentiment, 'rsi': p.rsi,
                    'days_to_earnings': p.days_to_earnings, 'predicted_move': p.predicted_move,
                    'reasoning': json.dumps(p.reasoning)
                }
                batch.put_item(Item=_to_decimal(item))
        print(f"[DynamoDB] Run saved successfully (ID: {run_id})")
        return run_id

    def get_latest_run(self) -> Optional[dict]:
        print("[DynamoDB] Fetching latest run...")
        try:
            response = self.runs_table.scan()
            items = response.get('Items', [])
            if not items: 
                print("[DynamoDB] No runs found.")
                return None
            latest = max(items, key=lambda x: x['run_at'])
            res = _from_decimal(latest)
            res['id'] = res['run_id']
            # Fallback for old runs
            if 'trigger_type' not in res:
                res['trigger_type'] = 'UNKNOWN'
            print(f"[DynamoDB] Found latest run: {res['id']} ({res['trigger_type']})")
            return res
        except Exception as e:
            print(f"[DynamoDB] Error fetching latest run: {e}")
            return None

    def get_predictions_for_run(self, run_id: str) -> list[dict]:
        print(f"[DynamoDB] Fetching predictions for run {run_id}...")
        try:
            response = self.preds_table.scan(FilterExpression=Attr('run_id').eq(run_id))
            items = sorted(response.get('Items', []), key=lambda x: x.get('overall_score', 0), reverse=True)
            res = [_from_decimal(item) for item in items]
            for r in res: r['id'] = r['predicted_at']
            print(f"[DynamoDB] Found {len(res)} predictions.")
            return res
        except Exception as e:
            print(f"[DynamoDB] Error fetching predictions: {e}")
            return []

    def get_previous_predictions(self, symbol: str, limit: int = 5) -> list[dict]:
        print(f"[DynamoDB] Fetching prev predictions for {symbol} (limit {limit})...")
        response = self.preds_table.query(KeyConditionExpression=Key('symbol').eq(symbol), ScanIndexForward=False, Limit=limit)
        res = [_from_decimal(item) for item in response.get('Items', [])]
        for r in res: r['id'] = r['predicted_at']
        return res

    def save_outcome(self, prediction_id: str, symbol: str, price_at_pred: float, prices: dict, prediction_correct: bool):
        fields = {
            'price_after_1d': prices.get("1d"), 'price_after_3d': prices.get("3d"),
            'price_after_5d': prices.get("5d"), 'price_after_10d': prices.get("10d"),
            'return_1d_pct': prices.get("ret_1d"), 'return_3d_pct': prices.get("ret_3d"),
            'return_5d_pct': prices.get("ret_5d"), 'return_10d_pct': prices.get("ret_10d"),
            'prediction_correct': prediction_correct, 'checked_at': datetime.now(timezone.utc).isoformat()
        }
        update_expr, expr_names, expr_vals = [], {}, {}
        for k, v in fields.items():
            if v is not None:
                update_expr.append(f"#{k} = :{k}")
                expr_names[f"#{k}"] = k
                expr_vals[f":{k}"] = _to_decimal(v)
        if update_expr:
            self.preds_table.update_item(
                Key={'symbol': symbol, 'predicted_at': prediction_id},
                UpdateExpression="SET " + ", ".join(update_expr),
                ExpressionAttributeNames=expr_names, ExpressionAttributeValues=expr_vals
            )

    def get_outcomes(self, limit: int = 100) -> list[dict]:
        response = self.preds_table.scan(FilterExpression=Attr('checked_at').exists())
        items = sorted(response.get('Items', []), key=lambda x: x.get('checked_at', ''), reverse=True)
        res = [_from_decimal(item) for item in items[:limit]]
        for r in res: r['id'] = r['predicted_at']
        return res

    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
        response = self.preds_table.scan(FilterExpression=Attr('predicted_at').lt(cutoff) & Attr('checked_at').not_exists())
        items = sorted(response.get('Items', []), key=lambda x: x.get('predicted_at', ''), reverse=True)
        res = [_from_decimal(item) for item in items]
        for r in res: r['id'] = r['predicted_at']
        return res

    def save_alert(self, alert_type: str, symbol: str, message: str, prediction: str = "", score: float = 0, price: float = 0):
        self.alerts_table.put_item(Item=_to_decimal({
            'alert_id': str(uuid.uuid4()), 'alert_type': alert_type, 'symbol': symbol,
            'message': message, 'prediction': prediction, 'score': score, 'price': price,
            'created_at': datetime.now(timezone.utc).isoformat(), 'seen': False
        }))

    def get_unseen_alerts(self) -> list[dict]:
        response = self.alerts_table.scan(FilterExpression=Attr('seen').eq(False))
        items = sorted(response.get('Items', []), key=lambda x: x.get('created_at', ''), reverse=True)
        return [_from_decimal(item) for item in items]

    def mark_alerts_seen(self):
        response = self.alerts_table.scan(FilterExpression=Attr('seen').eq(False))
        for item in response.get('Items', []):
            self.alerts_table.update_item(Key={'alert_id': item['alert_id']}, UpdateExpression="SET seen = :s", ExpressionAttributeValues={':s': True})

    def get_recent_alerts(self, hours: int = 24) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        response = self.alerts_table.scan(FilterExpression=Attr('created_at').gte(cutoff))
        items = sorted(response.get('Items', []), key=lambda x: x.get('created_at', ''), reverse=True)
        return [_from_decimal(item) for item in items]

    def get_backtest_stats(self) -> dict:
        response = self.preds_table.scan(FilterExpression=Attr('checked_at').exists())
        outcomes = response.get('Items', [])
        total = len(outcomes)
        if total == 0: return {"total": 0}
        correct = sum(1 for o in outcomes if o.get('prediction_correct'))
        def avg(key):
            vals = [float(o[key]) for o in outcomes if o.get(key) is not None]
            return sum(vals) / len(vals) if vals else None
        bullish = [o for o in outcomes if o.get('prediction') == 'BULLISH']
        b_total = len(bullish)
        b_correct = sum(1 for o in bullish if o.get('prediction_correct'))
        return {
            "total": total, "correct": correct, "accuracy": correct / total,
            "avg_return_1d": avg('return_1d_pct'), "avg_return_3d": avg('return_3d_pct'),
            "avg_return_5d": avg('return_5d_pct'), "avg_return_10d": avg('return_10d_pct'),
            "bullish_accuracy": b_correct / b_total if b_total > 0 else 0, "bullish_total": b_total
        }

    def get_all_symbols_from_last_run(self) -> set[str]:
        last_run = self.get_latest_run()
        if not last_run: return set()
        preds = self.get_predictions_for_run(last_run['run_id'])
        return {p['symbol'] for p in preds}

    def get_prediction_by_symbol_and_run(self, symbol: str, run_id: str) -> Optional[dict]:
        print(f"[DynamoDB] Fetching prediction for {symbol} in run {run_id}...")
        try:
            response = self.preds_table.query(
                KeyConditionExpression=Key('symbol').eq(symbol),
                FilterExpression=Attr('run_id').eq(run_id)
            )
            items = response.get('Items', [])
            if items:
                res = _from_decimal(items[0])
                res['id'] = res['predicted_at']
                return res
        except Exception as e:
            print(f"[DynamoDB] Error fetching prediction: {e}")
            pass
        return None

    def save_heartbeat(self, status: str, message: str = ""):
        print(f"[DynamoDB] Saving heartbeat: {status} - {message}")
        self.status_table.put_item(Item=_to_decimal({
            'system_id': 'bot_heartbeat',
            'status': status,
            'message': message,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }))

    def get_heartbeat(self) -> Optional[dict]:
        print("[DynamoDB] Fetching heartbeat...")
        try:
            response = self.status_table.get_item(Key={'system_id': 'bot_heartbeat'})
            res = _from_decimal(response.get('Item'))
            print(f"[DynamoDB] Heartbeat: {res['status'] if res else 'None'}")
            return res
        except Exception as e:
            print(f"[DynamoDB] Error fetching heartbeat: {e}")
            return None

class SQLiteStorage(BaseStorage):
    def __init__(self, db_path="~/.stock_screener/local_history.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, run_at TEXT, min_return REAL, stock_count INTEGER, top_n INTEGER, trigger_type TEXT
            )
        """)
        
        # Migration: Add trigger_type column if it doesn't exist (for existing DBs)
        try:
            cursor.execute("SELECT trigger_type FROM runs LIMIT 1")
        except sqlite3.OperationalError:
            print("[SQLite] Migration: Adding 'trigger_type' column to 'runs' table...")
            cursor.execute("ALTER TABLE runs ADD COLUMN trigger_type TEXT DEFAULT 'UNKNOWN'")
            self.conn.commit()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                symbol TEXT, predicted_at TEXT, run_id TEXT, price_at_prediction REAL,
                prediction TEXT, confidence REAL, overall_score REAL, momentum_score REAL,
                sentiment_score REAL, technical_score REAL, change_3m_pct REAL,
                change_1m_pct REAL, change_1w_pct REAL, avg_sentiment REAL, rsi REAL,
                days_to_earnings REAL, predicted_move REAL, reasoning TEXT,
                price_after_1d REAL, price_after_3d REAL, price_after_5d REAL, price_after_10d REAL,
                return_1d_pct REAL, return_3d_pct REAL, return_5d_pct REAL, return_10d_pct REAL,
                prediction_correct INTEGER, checked_at TEXT,
                PRIMARY KEY (symbol, predicted_at)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY, alert_type TEXT, symbol TEXT, message TEXT,
                prediction TEXT, score REAL, price REAL, created_at TEXT, seen INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_status (
                system_id TEXT PRIMARY KEY, status TEXT, message TEXT, last_updated TEXT
            )
        """)
        self.conn.commit()

    def save_run(self, predictions: list, min_return: float, top_n: int, trigger_type: str = "MANUAL") -> str:
        print(f"[SQLite] Saving {trigger_type} run with {len(predictions)} predictions...")
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)", (now, now, min_return, len(predictions), top_n, trigger_type))
        for p in predictions:
            cursor.execute("""
                INSERT INTO predictions (symbol, predicted_at, run_id, price_at_prediction, prediction, confidence, overall_score, momentum_score, sentiment_score, technical_score, change_3m_pct, change_1m_pct, change_1w_pct, avg_sentiment, rsi, days_to_earnings, predicted_move, reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (p.symbol, now, now, p.current_price, p.prediction, p.confidence, p.overall_score, p.momentum_score, p.sentiment_score, p.technical_score, p.change_3m_pct, p.change_1m_pct, p.change_1w_pct, p.avg_sentiment, p.rsi, p.days_to_earnings, p.predicted_move, json.dumps(p.reasoning)))
        self.conn.commit()
        print(f"[SQLite] Run saved successfully (ID: {now})")
        return now

    def get_latest_run(self) -> Optional[dict]:
        print("[SQLite] Fetching latest run...")
        row = self.conn.execute("SELECT * FROM runs ORDER BY run_at DESC LIMIT 1").fetchone()
        if not row: 
            print("[SQLite] No runs found.")
            return None
        res = dict(row)
        res['id'] = res['run_id']
        print(f"[SQLite] Found latest run: {res['id']}")
        return res

    def get_predictions_for_run(self, run_id: str) -> list[dict]:
        print(f"[SQLite] Fetching predictions for run {run_id}...")
        rows = self.conn.execute("SELECT * FROM predictions WHERE run_id = ? ORDER BY overall_score DESC", (run_id,)).fetchall()
        res = [dict(r) for r in rows]
        for r in res: r['id'] = r['predicted_at']
        print(f"[SQLite] Found {len(res)} predictions.")
        return res

    def get_previous_predictions(self, symbol: str, limit: int = 5) -> list[dict]:
        print(f"[SQLite] Fetching prev predictions for {symbol} (limit {limit})...")
        rows = self.conn.execute("SELECT * FROM predictions WHERE symbol = ? ORDER BY predicted_at DESC LIMIT ?", (symbol, limit)).fetchall()
        res = [dict(r) for r in rows]
        for r in res: r['id'] = r['predicted_at']
        return res

    def save_outcome(self, prediction_id: str, symbol: str, price_at_pred: float, prices: dict, prediction_correct: bool):
        self.conn.execute("""
            UPDATE predictions SET 
                price_after_1d=?, price_after_3d=?, price_after_5d=?, price_after_10d=?,
                return_1d_pct=?, return_3d_pct=?, return_5d_pct=?, return_10d_pct=?,
                prediction_correct=?, checked_at=?
            WHERE symbol=? AND predicted_at=?
        """, (prices.get("1d"), prices.get("3d"), prices.get("5d"), prices.get("10d"),
              prices.get("ret_1d"), prices.get("ret_3d"), prices.get("ret_5d"), prices.get("ret_10d"),
              1 if prediction_correct else 0, datetime.now(timezone.utc).isoformat(), symbol, prediction_id))
        self.conn.commit()

    def get_outcomes(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM predictions WHERE checked_at IS NOT NULL ORDER BY checked_at DESC LIMIT ?", (limit,)).fetchall()
        res = [dict(r) for r in rows]
        for r in res: r['id'] = r['predicted_at']
        return res

    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
        rows = self.conn.execute("SELECT * FROM predictions WHERE predicted_at < ? AND checked_at IS NULL ORDER BY predicted_at DESC", (cutoff,)).fetchall()
        res = [dict(r) for r in rows]
        for r in res: r['id'] = r['predicted_at']
        return res

    def save_alert(self, alert_type: str, symbol: str, message: str, prediction: str = "", score: float = 0, price: float = 0):
        self.conn.execute("INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), alert_type, symbol, message, prediction, score, price, datetime.now(timezone.utc).isoformat(), 0))
        self.conn.commit()

    def get_unseen_alerts(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM alerts WHERE seen = 0 ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def mark_alerts_seen(self):
        self.conn.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")
        self.conn.commit()

    def get_recent_alerts(self, hours: int = 24) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute("SELECT * FROM alerts WHERE created_at >= ? ORDER BY created_at DESC", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def get_backtest_stats(self) -> dict:
        rows = self.conn.execute("SELECT * FROM predictions WHERE checked_at IS NOT NULL").fetchall()
        outcomes = [dict(r) for r in rows]
        total = len(outcomes)
        if total == 0: return {"total": 0}
        correct = sum(1 for o in outcomes if o.get('prediction_correct'))
        def avg(key):
            vals = [float(o[key]) for o in outcomes if o.get(key) is not None]
            return sum(vals) / len(vals) if vals else None
        bullish = [o for o in outcomes if o.get('prediction') == 'BULLISH']
        b_total = len(bullish)
        b_correct = sum(1 for o in bullish if o.get('prediction_correct'))
        return {
            "total": total, "correct": correct, "accuracy": correct / total,
            "avg_return_1d": avg('return_1d_pct'), "avg_return_3d": avg('return_3d_pct'),
            "avg_return_5d": avg('return_5d_pct'), "avg_return_10d": avg('return_10d_pct'),
            "bullish_accuracy": b_correct / b_total if b_total > 0 else 0, "bullish_total": b_total
        }

    def get_all_symbols_from_last_run(self) -> set[str]:
        last_run = self.get_latest_run()
        if not last_run: return set()
        preds = self.get_predictions_for_run(last_run['run_id'])
        return {p['symbol'] for p in preds}

    def get_prediction_by_symbol_and_run(self, symbol: str, run_id: str) -> Optional[dict]:
        print(f"[SQLite] Fetching prediction for {symbol} in run {run_id}...")
        row = self.conn.execute("SELECT * FROM predictions WHERE symbol = ? AND run_id = ?", (symbol, run_id)).fetchone()
        if not row: return None
        res = dict(row)
        res['id'] = res['predicted_at']
        return res

    def save_heartbeat(self, status: str, message: str = ""):
        print(f"[SQLite] Saving heartbeat: {status} - {message}")
        self.conn.execute("""
            INSERT OR REPLACE INTO system_status (system_id, status, message, last_updated)
            VALUES (?, ?, ?, ?)
        """, ('bot_heartbeat', status, message, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def get_heartbeat(self) -> Optional[dict]:
        print("[SQLite] Fetching heartbeat...")
        row = self.conn.execute("SELECT * FROM system_status WHERE system_id = ?", ('bot_heartbeat',)).fetchone()
        res = dict(row) if row else None
        print(f"[SQLite] Heartbeat: {res['status'] if res else 'None'}")
        return res

    def close(self):
        self.conn.close()

# --- Factory Function ---

def History(region_name="us-east-1"):
    """Factory that returns either DynamoDBStorage or SQLiteStorage based on ENV."""
    env = os.environ.get("ENV", "DEV").upper()
    print(f"[History] Detected ENV: {env}")
    if env == "PROD":
        try:
            return DynamoDBStorage(region_name=region_name)
        except Exception as e:
            print(f"Falling back to SQLite: DynamoDB initialization failed: {e}")
            return SQLiteStorage()
    else:
        return SQLiteStorage()
