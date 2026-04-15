"""Persistent history storage for predictions, alerts, and backtesting.

Stores prediction snapshots in SQLite so we can:
- Backtest: compare past predictions against actual outcomes
- Alert: detect new entries or BULLISH flips
- Schedule: save each run's results
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional


DEFAULT_DB_PATH = os.path.expanduser("~/.stock_screener/history.db")


class History:
    """SQLite-backed history of screener predictions."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                max_price REAL,
                min_return REAL,
                stock_count INTEGER,
                top_n INTEGER
            );

            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                price_at_prediction REAL,
                prediction TEXT,
                confidence REAL,
                overall_score REAL,
                momentum_score REAL,
                sentiment_score REAL,
                technical_score REAL,
                change_3m_pct REAL,
                change_1m_pct REAL,
                change_1w_pct REAL,
                avg_sentiment REAL,
                rsi REAL,
                predicted_move TEXT,
                reasoning TEXT,
                predicted_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                price_at_prediction REAL,
                price_after_1d REAL,
                price_after_3d REAL,
                price_after_5d REAL,
                price_after_10d REAL,
                return_1d_pct REAL,
                return_3d_pct REAL,
                return_5d_pct REAL,
                return_10d_pct REAL,
                prediction_correct BOOLEAN,
                checked_at TEXT,
                FOREIGN KEY (prediction_id) REFERENCES predictions(id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                message TEXT,
                prediction TEXT,
                score REAL,
                price REAL,
                created_at TEXT DEFAULT (datetime('now')),
                seen BOOLEAN DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_predictions_symbol
                ON predictions(symbol, predicted_at);
            CREATE INDEX IF NOT EXISTS idx_predictions_run
                ON predictions(run_id);
            CREATE INDEX IF NOT EXISTS idx_outcomes_prediction
                ON outcomes(prediction_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_created
                ON alerts(created_at);
        """)
        self.conn.commit()

    def save_run(self, predictions: list, max_price: float, min_return: float, top_n: int) -> int:
        """Save a full screener run. Returns run_id."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "INSERT INTO runs (run_at, max_price, min_return, stock_count, top_n) VALUES (?, ?, ?, ?, ?)",
            (now, max_price, min_return, len(predictions), top_n),
        )
        run_id = cursor.lastrowid

        for p in predictions:
            self.conn.execute(
                """INSERT INTO predictions
                   (run_id, symbol, price_at_prediction, prediction, confidence,
                    overall_score, momentum_score, sentiment_score, technical_score,
                    change_3m_pct, change_1m_pct, change_1w_pct, avg_sentiment,
                    rsi, predicted_move, reasoning, predicted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, p.symbol, p.current_price, p.prediction, p.confidence,
                    p.overall_score, p.momentum_score, p.sentiment_score, p.technical_score,
                    p.change_3m_pct, p.change_1m_pct, p.change_1w_pct, p.avg_sentiment,
                    p.rsi, p.predicted_move, json.dumps(p.reasoning), now,
                ),
            )
        self.conn.commit()
        return run_id

    def get_latest_run(self) -> Optional[dict]:
        """Get the most recent run."""
        row = self.conn.execute(
            "SELECT * FROM runs ORDER BY run_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_predictions_for_run(self, run_id: int) -> list[dict]:
        """Get all predictions from a specific run."""
        rows = self.conn.execute(
            "SELECT * FROM predictions WHERE run_id = ? ORDER BY overall_score DESC",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_previous_predictions(self, symbol: str, limit: int = 5) -> list[dict]:
        """Get previous predictions for a symbol."""
        rows = self.conn.execute(
            "SELECT * FROM predictions WHERE symbol = ? ORDER BY predicted_at DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_symbols_from_last_run(self) -> set[str]:
        """Get set of symbols from the previous run."""
        last_run = self.get_latest_run()
        if not last_run:
            return set()
        rows = self.conn.execute(
            "SELECT symbol FROM predictions WHERE run_id = ?",
            (last_run["id"],),
        ).fetchall()
        return {r["symbol"] for r in rows}

    def get_prediction_by_symbol_and_run(self, symbol: str, run_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM predictions WHERE symbol = ? AND run_id = ?",
            (symbol, run_id),
        ).fetchone()
        return dict(row) if row else None

    # --- Outcomes (for backtesting) ---

    def save_outcome(self, prediction_id: int, symbol: str,
                     price_at_pred: float, prices: dict, prediction_correct: bool):
        """Save the actual outcome after N days."""
        self.conn.execute(
            """INSERT OR REPLACE INTO outcomes
               (prediction_id, symbol, price_at_prediction,
                price_after_1d, price_after_3d, price_after_5d, price_after_10d,
                return_1d_pct, return_3d_pct, return_5d_pct, return_10d_pct,
                prediction_correct, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prediction_id, symbol, price_at_pred,
                prices.get("1d"), prices.get("3d"), prices.get("5d"), prices.get("10d"),
                prices.get("ret_1d"), prices.get("ret_3d"), prices.get("ret_5d"), prices.get("ret_10d"),
                prediction_correct, datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def get_outcomes(self, limit: int = 100) -> list[dict]:
        """Get recent outcomes."""
        rows = self.conn.execute(
            """SELECT o.*, p.prediction, p.confidence, p.overall_score, p.predicted_move
               FROM outcomes o
               JOIN predictions p ON o.prediction_id = p.id
               ORDER BY o.checked_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_predictions_needing_backtest(self, min_age_days: int = 5) -> list[dict]:
        """Get predictions old enough to check outcomes but not yet checked."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
        rows = self.conn.execute(
            """SELECT p.* FROM predictions p
               LEFT JOIN outcomes o ON p.id = o.prediction_id
               WHERE p.predicted_at < ? AND o.id IS NULL
               ORDER BY p.predicted_at DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Alerts ---

    def save_alert(self, alert_type: str, symbol: str, message: str,
                   prediction: str = "", score: float = 0, price: float = 0):
        self.conn.execute(
            """INSERT INTO alerts (alert_type, symbol, message, prediction, score, price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (alert_type, symbol, message, prediction, score, price),
        )
        self.conn.commit()

    def get_unseen_alerts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alerts WHERE seen = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_alerts_seen(self):
        self.conn.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")
        self.conn.commit()

    def get_recent_alerts(self, hours: int = 24) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM alerts WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Stats ---

    def get_backtest_stats(self) -> dict:
        """Get aggregate backtest statistics."""
        total = self.conn.execute("SELECT COUNT(*) as c FROM outcomes").fetchone()["c"]
        if total == 0:
            return {"total": 0}

        correct = self.conn.execute(
            "SELECT COUNT(*) as c FROM outcomes WHERE prediction_correct = 1"
        ).fetchone()["c"]

        avg_returns = self.conn.execute(
            """SELECT
                 AVG(return_1d_pct) as avg_1d,
                 AVG(return_3d_pct) as avg_3d,
                 AVG(return_5d_pct) as avg_5d,
                 AVG(return_10d_pct) as avg_10d
               FROM outcomes"""
        ).fetchone()

        bullish_correct = self.conn.execute(
            """SELECT COUNT(*) as c FROM outcomes o
               JOIN predictions p ON o.prediction_id = p.id
               WHERE p.prediction = 'BULLISH' AND o.prediction_correct = 1"""
        ).fetchone()["c"]

        bullish_total = self.conn.execute(
            """SELECT COUNT(*) as c FROM outcomes o
               JOIN predictions p ON o.prediction_id = p.id
               WHERE p.prediction = 'BULLISH'"""
        ).fetchone()["c"]

        return {
            "total": total,
            "correct": correct,
            "accuracy": correct / total if total > 0 else 0,
            "avg_return_1d": avg_returns["avg_1d"],
            "avg_return_3d": avg_returns["avg_3d"],
            "avg_return_5d": avg_returns["avg_5d"],
            "avg_return_10d": avg_returns["avg_10d"],
            "bullish_accuracy": bullish_correct / bullish_total if bullish_total > 0 else 0,
            "bullish_total": bullish_total,
        }

    def close(self):
        self.conn.close()
