"""Persistent history storage for predictions, alerts, and backtesting.

Supports dual backends: DynamoDB and SQLite (with WAL mode for concurrency).
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

# --- Helper Utilities ---

def _to_decimal(obj):
    if isinstance(obj, float): return Decimal(str(obj))
    if isinstance(obj, dict): return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_to_decimal(v) for v in obj]
    return obj

def _from_decimal(obj):
    if isinstance(obj, Decimal): return float(obj)
    if isinstance(obj, dict): return {k: _from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_from_decimal(v) for v in obj]
    return obj

# --- Storage Interface ---

class BaseStorage:
    def save_run(self, predictions: list, min_return: float, top_n: int, trigger_type: str = "MANUAL") -> str: raise NotImplementedError
    def get_latest_run(self, exclude_triggers: list | None = None) -> Optional[dict]: raise NotImplementedError
    def get_predictions_for_run(self, run_id: str) -> list[dict]: raise NotImplementedError
    def save_heartbeat(self, status: str, message: str = ""): raise NotImplementedError
    def get_heartbeat(self) -> Optional[dict]: raise NotImplementedError
    def get_backtest_stats(self) -> dict: raise NotImplementedError
    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]: raise NotImplementedError
    def save_outcome(self, prediction_id: str, symbol: str, price_at_pred: float, prices: dict, prediction_correct: bool, ret_5d_pct: Optional[float] = None): raise NotImplementedError
    def get_outcomes_with_subscores(self) -> list[dict]: raise NotImplementedError
    def close(self): pass

    def get_all_symbols_from_last_run(self) -> list[str]:
        latest = self.get_latest_run()
        if not latest: return []
        preds = self.get_predictions_for_run(latest.get('id') or latest.get('run_id'))
        return [p.get('symbol') for p in preds if p.get('symbol')]

    def get_prediction_by_symbol_and_run(self, symbol: str, run_id: str) -> Optional[dict]:
        preds = self.get_predictions_for_run(run_id)
        for p in preds:
            if p.get('symbol') == symbol: return p
        return None

    def save_alert(self, **kwargs):
        pass # Can be extended to persist alerts

class DynamoDBStorage(BaseStorage):
    def __init__(self, region_name="us-east-1"):
        self.dynamodb = boto3.resource('dynamodb', region_name=region_name)
        env = os.environ.get("ENV", "DEV").upper()
        self.runs_table = self.dynamodb.Table(f"{env}_StockScreenerRuns")
        self.preds_table = self.dynamodb.Table(f"{env}_StockScreenerPredictions")
        self.status_table = self.dynamodb.Table(f"{env}_StockScreenerStatus")

    def save_run(self, predictions: list, min_return: float, top_n: int, trigger_type: str = "MANUAL") -> str:
        now = datetime.now(timezone.utc).isoformat()
        run_id = now
        print(f"[DynamoDB] Saving {trigger_type} run...")
        self.runs_table.put_item(Item=_to_decimal({
            'run_id': run_id, 'run_at': now, 'min_return': min_return,
            'stock_count': len(predictions), 'top_n': top_n, 'trigger_type': trigger_type
        }))
        with self.preds_table.batch_writer() as batch:
            for p in predictions:
                batch.put_item(Item=_to_decimal({
                    'symbol': p.symbol, 'predicted_at': now, 'run_id': run_id,
                    'price_at_prediction': p.current_price, 'prediction': p.prediction,
                    'overall_score': p.overall_score, 'archetype': p.archetype,
                    'volume_ratio': p.volume_ratio, 'rsi': p.rsi, 'predicted_move': p.predicted_move,
                    'reasoning': json.dumps(p.reasoning),
                    'momentum_score': p.momentum_score, 'volume_score': p.volume_score,
                    'technical_score': p.technical_score, 'sentiment_score': p.sentiment_score,
                    'avg_sentiment': p.avg_sentiment, 'bullish_count': p.bullish_count,
                }))
        return run_id

    def get_latest_run(self, exclude_triggers: list | None = None) -> Optional[dict]:
        try:
            items = self.runs_table.scan().get('Items', [])
            if not items: return None
            if exclude_triggers:
                items = [i for i in items if i.get('trigger_type') not in exclude_triggers]
            if not items: return None
            latest = max(items, key=lambda x: x['run_at'])
            res = _from_decimal(latest)
            res['id'] = res['run_id']
            return res
        except: return None

    def get_predictions_for_run(self, run_id: str) -> list[dict]:
        try:
            response = self.preds_table.scan(FilterExpression=Attr('run_id').eq(run_id))
            return [_from_decimal(i) for i in response.get('Items', [])]
        except: return []

    def save_heartbeat(self, status: str, message: str = ""):
        self.status_table.put_item(Item=_to_decimal({
            'system_id': 'bot_heartbeat', 'status': status, 'message': message,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }))

    def get_heartbeat(self) -> Optional[dict]:
        try:
            res = self.status_table.get_item(Key={'system_id': 'bot_heartbeat'}).get('Item')
            return _from_decimal(res)
        except: return None

    def get_backtest_stats(self) -> dict:
        try:
            response = self.preds_table.scan(FilterExpression=Attr('checked_at').exists())
            outcomes = response.get('Items', [])
            total = len(outcomes)
            if total == 0: return {"total": 0}
            correct = sum(1 for o in outcomes if o.get('prediction_correct'))
            return {"total": total, "correct": correct, "accuracy": correct / total}
        except: return {"total": 0}

    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
        response = self.preds_table.scan(FilterExpression=Attr('predicted_at').lt(cutoff) & Attr('checked_at').not_exists())
        return [_from_decimal(item) for item in response.get('Items', [])]

    def save_outcome(self, prediction_id: str, symbol: str, price_at_pred: float, prices: dict, prediction_correct: bool, ret_5d_pct: Optional[float] = None):
        update_expr = "SET checked_at = :now, prediction_correct = :correct"
        attr_vals: dict = {':now': datetime.now(timezone.utc).isoformat(), ':correct': prediction_correct}
        if ret_5d_pct is not None:
            update_expr += ", ret_5d_pct = :ret5d"
            attr_vals[':ret5d'] = _to_decimal(ret_5d_pct)
        self.preds_table.update_item(
            Key={'symbol': symbol, 'predicted_at': prediction_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=attr_vals,
        )

    def get_outcomes_with_subscores(self) -> list[dict]:
        try:
            from boto3.dynamodb.conditions import Attr
            response = self.preds_table.scan(
                FilterExpression=Attr('checked_at').exists() & Attr('momentum_score').exists()
            )
            return [_from_decimal(i) for i in response.get('Items', [])]
        except Exception:
            return []

class SQLiteStorage(BaseStorage):
    def __init__(self, db_path="~/.stock_screener/local_history.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        
        # --- WAL MODE (Institutional Fix for Concurrency) ---
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, run_at TEXT, min_return REAL, stock_count INTEGER, top_n INTEGER)")
        try:
            cursor.execute("SELECT trigger_type FROM runs LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE runs ADD COLUMN trigger_type TEXT DEFAULT 'UNKNOWN'")
        
        cursor.execute("CREATE TABLE IF NOT EXISTS system_status (system_id TEXT PRIMARY KEY, status TEXT, message TEXT, last_updated TEXT)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS predictions (
            symbol TEXT, predicted_at TEXT, run_id TEXT, price_at_prediction REAL, prediction TEXT, overall_score REAL, archetype TEXT, volume_ratio REAL, rsi REAL, reasoning TEXT, 
            prediction_correct INTEGER, checked_at TEXT, PRIMARY KEY (symbol, predicted_at))""")
            
        # Migration: Add new columns if missing
        new_columns = {
            "archetype": "TEXT",
            "volume_ratio": "REAL",
            "rsi": "REAL",
            "reasoning": "TEXT",
            "prediction_correct": "INTEGER",
            "checked_at": "TEXT",
            "momentum_score": "REAL",
            "volume_score": "REAL",
            "technical_score": "REAL",
            "sentiment_score": "REAL",
            "avg_sentiment": "REAL",
            "bullish_count": "INTEGER",
            "ret_5d_pct": "REAL",
        }
        for col, col_type in new_columns.items():
            try:
                cursor.execute(f"SELECT {col} FROM predictions LIMIT 1")
            except sqlite3.OperationalError:
                print(f"[SQLite] Migration: Adding '{col}' column to predictions...")
                cursor.execute(f"ALTER TABLE predictions ADD COLUMN {col} {col_type}")
                
        self.conn.commit()

    def save_run(self, predictions: list, min_return: float, top_n: int, trigger_type: str = "MANUAL") -> str:
        now = datetime.now(timezone.utc).isoformat()
        print(f"[SQLite] Saving {trigger_type} run...")
        self.conn.execute("INSERT INTO runs (run_id, run_at, min_return, stock_count, top_n, trigger_type) VALUES (?, ?, ?, ?, ?, ?)", 
                          (now, now, min_return, len(predictions), top_n, trigger_type))
        for p in predictions:
            self.conn.execute(
                "INSERT INTO predictions (symbol, predicted_at, run_id, price_at_prediction, prediction, "
                "overall_score, archetype, volume_ratio, rsi, reasoning, "
                "momentum_score, volume_score, technical_score, sentiment_score, avg_sentiment, bullish_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.symbol, now, now, p.current_price, p.prediction, p.overall_score, p.archetype,
                 p.volume_ratio, p.rsi, json.dumps(p.reasoning),
                 p.momentum_score, p.volume_score, p.technical_score, p.sentiment_score,
                 p.avg_sentiment, p.bullish_count)
            )
        self.conn.commit()
        print(f"[SQLite] {trigger_type} saved at {now}")
        return now

    def get_latest_run(self, exclude_triggers: list | None = None) -> Optional[dict]:
        if exclude_triggers:
            placeholders = ",".join("?" * len(exclude_triggers))
            row = self.conn.execute(
                f"SELECT * FROM runs WHERE trigger_type NOT IN ({placeholders}) ORDER BY ROWID DESC LIMIT 1",
                exclude_triggers,
            ).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM runs ORDER BY ROWID DESC LIMIT 1").fetchone()
        if not row: return None
        res = dict(row); res['id'] = res['run_id']
        return res

    def get_predictions_for_run(self, run_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM predictions WHERE run_id = ?", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def save_heartbeat(self, status: str, message: str = ""):
        self.conn.execute("INSERT OR REPLACE INTO system_status (system_id, status, message, last_updated) VALUES (?,?,?,?)",
                          ('bot_heartbeat', status, message, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def get_heartbeat(self) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM system_status WHERE system_id = 'bot_heartbeat'").fetchone()
        return dict(row) if row else None

    def get_backtest_stats(self) -> dict:
        rows = self.conn.execute("SELECT * FROM predictions WHERE checked_at IS NOT NULL").fetchall()
        outcomes = [dict(r) for r in rows]
        total = len(outcomes)
        if total == 0: return {"total": 0}
        correct = sum(1 for o in outcomes if o.get('prediction_correct'))
        return {"total": total, "correct": correct, "accuracy": correct / total}

    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
        rows = self.conn.execute("SELECT * FROM predictions WHERE predicted_at < ? AND checked_at IS NULL", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def save_outcome(self, prediction_id: str, symbol: str, price_at_pred: float, prices: dict, prediction_correct: bool, ret_5d_pct: Optional[float] = None):
        self.conn.execute(
            "UPDATE predictions SET checked_at = ?, prediction_correct = ?, ret_5d_pct = ? WHERE symbol = ? AND predicted_at = ?",
            (datetime.now(timezone.utc).isoformat(), 1 if prediction_correct else 0, ret_5d_pct, symbol, prediction_id)
        )
        self.conn.commit()

    def get_outcomes_with_subscores(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM predictions WHERE checked_at IS NOT NULL AND momentum_score IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self): self.conn.close()

def History():
    if os.environ.get("ENV") == "PROD": return DynamoDBStorage()
    return SQLiteStorage()
