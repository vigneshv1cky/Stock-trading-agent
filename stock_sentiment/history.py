"""Persistent history storage for predictions, alerts, and backtesting.

Stores prediction snapshots in DynamoDB so we can:
- Backtest: compare past predictions against actual outcomes
- Alert: detect new entries or BULLISH flips
- Schedule: save each run's results
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError


def _to_decimal(obj):
    """Recursively convert floats to Decimals for DynamoDB."""
    if isinstance(obj, float):
        # Prevent overly long decimals if they arise
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


class History:
    """DynamoDB-backed history of screener predictions."""

    def __init__(self, region_name="us-east-1"):
        # Configure endpoint for local testing if necessary, but use default for ECS
        self.dynamodb = boto3.resource('dynamodb', region_name=region_name)
        
        self.runs_table_name = "StockScreenerRuns"
        self.preds_table_name = "StockScreenerPredictions"
        self.alerts_table_name = "StockScreenerAlerts"

        self._init_schema()
        
        if self.dynamodb:
            self.runs_table = self.dynamodb.Table(self.runs_table_name)
            self.preds_table = self.dynamodb.Table(self.preds_table_name)
            self.alerts_table = self.dynamodb.Table(self.alerts_table_name)
        else:
            self.runs_table = None
            self.preds_table = None
            self.alerts_table = None

    def _init_schema(self):
        from botocore.exceptions import NoCredentialsError, BotoCoreError
        tables_to_create = []
        try:
            # list_tables is a client method, let's just get them
            existing_tables = [t.name for t in self.dynamodb.tables.all()]
        except (NoCredentialsError, BotoCoreError) as e:
            print(f"WARNING: AWS credentials not found. DynamoDB disabled. ({e})")
            self.dynamodb = None
            return
        except Exception:
            existing_tables = []
            
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
                'KeySchema': [
                    {'AttributeName': 'symbol', 'KeyType': 'HASH'},
                    {'AttributeName': 'predicted_at', 'KeyType': 'RANGE'}
                ],
                'AttributeDefinitions': [
                    {'AttributeName': 'symbol', 'AttributeType': 'S'},
                    {'AttributeName': 'predicted_at', 'AttributeType': 'S'}
                ],
                'BillingMode': 'PAY_PER_REQUEST'
            })
            
        if self.alerts_table_name not in existing_tables:
            tables_to_create.append({
                'TableName': self.alerts_table_name,
                'KeySchema': [{'AttributeName': 'alert_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'alert_id', 'AttributeType': 'S'}],
                'BillingMode': 'PAY_PER_REQUEST'
            })
            
        for config in tables_to_create:
            try:
                table = self.dynamodb.create_table(**config)
                table.wait_until_exists()
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceInUseException':
                    raise

    def save_run(self, predictions: list, min_return: float, top_n: int) -> str:
        """Save a full screener run. Returns run_id."""
        now = datetime.now(timezone.utc).isoformat()
        run_id = now  # Using ISO timestamp as run_id to easily sort latest
        
        if not self.runs_table or not self.preds_table:
            return run_id

        try:
            self.runs_table.put_item(Item=_to_decimal({
                'run_id': run_id,
                'run_at': now,
                'min_return': min_return,
                'stock_count': len(predictions),
                'top_n': top_n
            }))

            with self.preds_table.batch_writer() as batch:
                for p in predictions:
                    item = {
                        'symbol': p.symbol,
                        'predicted_at': now,
                        'run_id': run_id,
                        'price_at_prediction': p.current_price,
                        'prediction': p.prediction,
                        'confidence': p.confidence,
                        'overall_score': p.overall_score,
                        'momentum_score': p.momentum_score,
                        'sentiment_score': p.sentiment_score,
                        'technical_score': p.technical_score,
                        'change_3m_pct': p.change_3m_pct,
                        'change_1m_pct': p.change_1m_pct,
                        'change_1w_pct': p.change_1w_pct,
                        'avg_sentiment': p.avg_sentiment,
                        'rsi': p.rsi,
                        'days_to_earnings': p.days_to_earnings,
                        'predicted_move': p.predicted_move,
                        'reasoning': json.dumps(p.reasoning)                    }
                    batch.put_item(Item=_to_decimal(item))
        except Exception as e:
            print(f"Error saving run: {e}")
                
        return run_id
    def get_latest_run(self) -> Optional[dict]:
        """Get the most recent run."""
        if not hasattr(self, 'dynamodb') or self.dynamodb is None:
            return None
        try:
            # Using scan since data is small, and finding max run_id
            response = self.runs_table.scan()
            items = response.get('Items', [])
            if not items:
                return None
                
            latest = max(items, key=lambda x: x['run_at'])
            res = _from_decimal(latest)
            res['id'] = res['run_id']  # Alias for backward compatibility
            return res
        except Exception as e:
            print(f"Error fetching latest run: {e}")
            return None

    def get_predictions_for_run(self, run_id: str) -> list[dict]:
        """Get all predictions from a specific run."""
        if not self.preds_table:
            return []
        try:
            response = self.preds_table.scan(
                FilterExpression=Attr('run_id').eq(run_id)
            )
            items = response.get('Items', [])
            # Sort by overall_score DESC in memory
            items.sort(key=lambda x: x.get('overall_score', 0), reverse=True)
            res = [_from_decimal(item) for item in items]
            for r in res:
                r['id'] = r['predicted_at']  # Alias for outcomes
            return res
        except Exception as e:
            print(f"Error fetching predictions for run {run_id}: {e}")
            return []

    def get_previous_predictions(self, symbol: str, limit: int = 5) -> list[dict]:
        """Get previous predictions for a symbol."""
        if not self.preds_table:
            return []
        try:
            response = self.preds_table.query(
                KeyConditionExpression=Key('symbol').eq(symbol),
                ScanIndexForward=False,  # Descending order by predicted_at
                Limit=limit
            )
            res = [_from_decimal(item) for item in response.get('Items', [])]
            for r in res:
                r['id'] = r['predicted_at']
            return res
        except Exception as e:
            print(f"Error fetching previous predictions for {symbol}: {e}")
            return []

    def get_all_symbols_from_last_run(self) -> set[str]:
        """Get set of symbols from the previous run."""
        try:
            last_run = self.get_latest_run()
            if not last_run:
                return set()
            preds = self.get_predictions_for_run(last_run['run_id'])
            return {p['symbol'] for p in preds}
        except Exception as e:
            print(f"Error fetching symbols from last run: {e}")
            return set()

    def get_prediction_by_symbol_and_run(self, symbol: str, run_id: str) -> Optional[dict]:
        if not self.preds_table:
            return None
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
            print(f"Error fetching prediction for {symbol} in run {run_id}: {e}")
        return None

    # --- Outcomes (for backtesting) ---

    def save_outcome(self, prediction_id: str, symbol: str,
                     price_at_pred: float, prices: dict, prediction_correct: bool):
        """Save the actual outcome after N days (updates prediction item)."""
        if not self.preds_table:
            return
            
        update_expr = []
        expr_attr_vals = {}
        expr_attr_names = {}
        
        # Add outcome fields
        fields_to_update = {
            'price_after_1d': prices.get("1d"),
            'price_after_3d': prices.get("3d"),
            'price_after_5d': prices.get("5d"),
            'price_after_10d': prices.get("10d"),
            'return_1d_pct': prices.get("ret_1d"),
            'return_3d_pct': prices.get("ret_3d"),
            'return_5d_pct': prices.get("ret_5d"),
            'return_10d_pct': prices.get("ret_10d"),
            'prediction_correct': prediction_correct,
            'checked_at': datetime.now(timezone.utc).isoformat()
        }
        
        for k, v in fields_to_update.items():
            if v is not None:
                update_expr.append(f"#{k} = :{k}")
                expr_attr_names[f"#{k}"] = k
                expr_attr_vals[f":{k}"] = _to_decimal(v)
                
        if not update_expr:
            return

        try:
            self.preds_table.update_item(
                Key={'symbol': symbol, 'predicted_at': prediction_id},
                UpdateExpression="SET " + ", ".join(update_expr),
                ExpressionAttributeNames=expr_attr_names,
                ExpressionAttributeValues=expr_attr_vals
            )
        except Exception as e:
            print(f"Error saving outcome for {symbol}: {e}")

    def get_outcomes(self, limit: int = 100) -> list[dict]:
        """Get recent outcomes."""
        if not self.preds_table:
            return []
        try:
            # Scan for items that have been checked
            response = self.preds_table.scan(
                FilterExpression=Attr('checked_at').exists()
            )
            items = response.get('Items', [])
            # Sort by checked_at desc
            items.sort(key=lambda x: x.get('checked_at', ''), reverse=True)
            res = [_from_decimal(item) for item in items[:limit]]
            for r in res:
                r['id'] = r['predicted_at']
            return res
        except Exception as e:
            print(f"Error fetching outcomes: {e}")
            return []

    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]:
        """Get predictions old enough to check outcomes but not yet checked."""
        if not self.preds_table:
            return []
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
            
            response = self.preds_table.scan(
                FilterExpression=Attr('predicted_at').lt(cutoff) & Attr('checked_at').not_exists()
            )
            items = response.get('Items', [])
            items.sort(key=lambda x: x.get('predicted_at', ''), reverse=True)
            
            res = [_from_decimal(item) for item in items]
            for r in res:
                r['id'] = r['predicted_at']
            return res
        except Exception as e:
            print(f"Error fetching predictions needing backtest: {e}")
            return []

    # --- Alerts ---

    def save_alert(self, alert_type: str, symbol: str, message: str,
                   prediction: str = "", score: float = 0, price: float = 0):
        if not self.alerts_table:
            return
        try:
            self.alerts_table.put_item(Item=_to_decimal({
                'alert_id': str(uuid.uuid4()),
                'alert_type': alert_type,
                'symbol': symbol,
                'message': message,
                'prediction': prediction,
                'score': score,
                'price': price,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'seen': False
            }))
        except Exception as e:
            print(f"Error saving alert for {symbol}: {e}")

    def get_unseen_alerts(self) -> list[dict]:
        if not self.alerts_table:
            return []
        try:
            response = self.alerts_table.scan(
                FilterExpression=Attr('seen').eq(False)
            )
            items = response.get('Items', [])
            items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            return [_from_decimal(item) for item in items]
        except Exception as e:
            print(f"Error fetching unseen alerts: {e}")
            return []

    def mark_alerts_seen(self):
        if not self.alerts_table:
            return
        try:
            # Scan and update all unseen alerts
            response = self.alerts_table.scan(
                FilterExpression=Attr('seen').eq(False)
            )
            items = response.get('Items', [])
            for item in items:
                self.alerts_table.update_item(
                    Key={'alert_id': item['alert_id']},
                    UpdateExpression="SET seen = :s",
                    ExpressionAttributeValues={':s': True}
                )
        except Exception as e:
            print(f"Error marking alerts as seen: {e}")

    def get_recent_alerts(self, hours: int = 24) -> list[dict]:
        if not self.alerts_table:
            return []
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            response = self.alerts_table.scan(
                FilterExpression=Attr('created_at').gte(cutoff)
            )
            items = response.get('Items', [])
            items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            return [_from_decimal(item) for item in items]
        except Exception as e:
            print(f"Error fetching recent alerts: {e}")
            return []

    # --- Stats ---
    def get_backtest_stats(self) -> dict:
        """Get aggregate backtest statistics."""
        if not hasattr(self, 'dynamodb') or self.dynamodb is None:
            return {"total": 0, "correct": 0, "accuracy": 0, "avg_return_1d": 0, "bullish_accuracy": 0, "bullish_total": 0}
        try:
            response = self.preds_table.scan(
                FilterExpression=Attr('checked_at').exists()
            )
            outcomes = response.get('Items', [])
            
            total = len(outcomes)
            if total == 0:
                return {"total": 0}
                
            correct = sum(1 for o in outcomes if o.get('prediction_correct'))
            
            # Calculate averages safely
            def avg(key):
                vals = [float(o[key]) for o in outcomes if o.get(key) is not None]
                return sum(vals) / len(vals) if vals else None

            avg_1d = avg('return_1d_pct')
            avg_3d = avg('return_3d_pct')
            avg_5d = avg('return_5d_pct')
            avg_10d = avg('return_10d_pct')
            
            bullish = [o for o in outcomes if o.get('prediction') == 'BULLISH']
            bullish_total = len(bullish)
            bullish_correct = sum(1 for o in bullish if o.get('prediction_correct'))
            
            return {
                "total": total,
                "correct": correct,
                "accuracy": correct / total if total > 0 else 0,
                "avg_return_1d": avg_1d,
                "avg_return_3d": avg_3d,
                "avg_return_5d": avg_5d,
                "avg_return_10d": avg_10d,
                "bullish_accuracy": bullish_correct / bullish_total if bullish_total > 0 else 0,
                "bullish_total": bullish_total,
            }
        except Exception as e:
            print(f"Error calculating backtest stats: {e}")
            return {"total": 0, "error": str(e)}

    def close(self):
        pass
