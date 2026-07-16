"""The decision ledger — SQLite (WAL). Every evaluation, token, and funnel count.

One row per evaluation (committee or solo). Closed-market picks carry
entry_price=NULL and are stamped with entry-at-next-open semantics by the
grader. All writes are single-process; the dashboard reads the same file.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from alphadesk.config import DATA_DIR

_DB = DATA_DIR / "ledger.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,                 -- decision time UTC ISO
    symbol          TEXT NOT NULL,
    arm             TEXT NOT NULL,                 -- COMMITTEE | SOLO
    edge            TEXT,                          -- RIPPLE | NARRATIVE | DRIFT
    trigger_src     TEXT NOT NULL,                 -- STREAM | DEEP_RUN | REPLAY
    session         TEXT NOT NULL,                 -- PRE | OPEN | AFTER | CLOSED
    -- decision
    direction       TEXT NOT NULL,                 -- LONG | SHORT
    horizon_days    INTEGER NOT NULL,
    score           REAL NOT NULL,                 -- pre-debate
    adjusted_score  REAL,                          -- post-debate (committee only)
    confidence      REAL NOT NULL,
    verdict         TEXT,                          -- CONFIRM | WEAKEN | REJECT
    approved        INTEGER NOT NULL DEFAULT 0,
    -- context
    triage_reason   TEXT,
    thesis          TEXT,
    debate          TEXT,                          -- JSON transcript
    briefs          TEXT,                          -- JSON
    model_tags      TEXT,                          -- JSON: stage → model actually used
    low_liquidity   INTEGER NOT NULL DEFAULT 0,
    -- attribution
    skeptic_moved_score REAL,
    arbiter_overrode    INTEGER DEFAULT 0,
    -- market snapshot
    entry_price     REAL,                          -- NULL when decided market-closed
    spy_price       REAL,
    -- outcomes
    ret_1d          REAL,
    ret_horizon     REAL,
    spy_ret_horizon REAL,
    alpha_net       REAL,
    graded_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_picks_ts ON picks (ts);
CREATE INDEX IF NOT EXISTS idx_picks_symbol ON picks (symbol);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,                       -- PREMARKET | EVENING | ADHOC
    top_picks TEXT                                 -- JSON
);

CREATE TABLE IF NOT EXISTS funnel (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    window_ts  TEXT NOT NULL,
    ingested   INTEGER DEFAULT 0,
    candidates INTEGER DEFAULT 0,
    picked     INTEGER DEFAULT 0,
    skipped    INTEGER DEFAULT 0,
    skip_reasons TEXT                              -- JSON [{symbol, reason}]
);

CREATE TABLE IF NOT EXISTS relationships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    from_sym    TEXT NOT NULL,      -- the shocked company
    to_sym      TEXT NOT NULL,      -- the exposed, tradable company
    direction   TEXT,              -- LONG | SHORT (the ripple's implied trade)
    chain       TEXT,              -- the causal chain, web-verified
    UNIQUE(from_sym, to_sym, direction) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships (from_sym);

CREATE TABLE IF NOT EXISTS token_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    role        TEXT NOT NULL,
    model       TEXT NOT NULL,
    input_tok   INTEGER NOT NULL,
    output_tok  INTEGER NOT NULL,
    decision_id TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------

_JSON_FIELDS = ("debate", "briefs", "model_tags")


def record_pick(row: dict[str, Any]) -> int:
    row = dict(row)
    row.setdefault("ts", _now())
    for field in _JSON_FIELDS:
        if field in row and not isinstance(row[field], (str, type(None))):
            row[field] = json.dumps(row[field])
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with _lock, _connect() as conn:
        cur = conn.execute(f"INSERT INTO picks ({cols}) VALUES ({marks})", list(row.values()))
        return int(cur.lastrowid or 0)


def update_pick(pick_id: int, **fields: Any) -> None:
    for field in _JSON_FIELDS:
        if field in fields and not isinstance(fields[field], (str, type(None))):
            fields[field] = json.dumps(fields[field])
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE picks SET {sets} WHERE id = ?", (*fields.values(), pick_id))


def due_for_grading(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM picks WHERE graded_at IS NULL ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _decode(row: dict) -> dict:
    for field in _JSON_FIELDS:
        if row.get(field):
            try:
                row[field] = json.loads(row[field])
            except Exception:
                pass
    return row


def recent(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM picks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_decode(dict(r)) for r in rows]


def get_pick(pick_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()
    return _decode(dict(row)) if row else None


def picks_today(arm: str | None = None) -> int:
    query = "SELECT count(*) FROM picks WHERE ts >= date('now')"
    args: list[Any] = []
    if arm:
        query += " AND arm = ?"
        args.append(arm)
    with _connect() as conn:
        return int(conn.execute(query, args).fetchone()[0])


def symbol_history(symbol: str, limit: int = 5) -> list[dict]:
    """Episodic memory: this symbol's graded track record."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, direction, horizon_days, confidence, alpha_net FROM picks"
            " WHERE symbol = ? AND graded_at IS NOT NULL ORDER BY id DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Stats — the scorecard: edge × horizon × confidence-bucket × arm
# ---------------------------------------------------------------------------

def stats() -> dict:
    with _connect() as conn:
        total = dict(conn.execute(
            "SELECT count(*) AS picks, count(graded_at) AS graded,"
            " round(avg(alpha_net), 3) AS avg_alpha_net,"
            " sum(CASE WHEN alpha_net > 0 THEN 1 ELSE 0 END) AS wins"
            " FROM picks"
        ).fetchone())
        by = {}
        for dim, expr in (
            ("edge", "edge"),
            ("arm", "arm"),
            ("horizon", "CASE WHEN horizon_days <= 2 THEN '1-2d' WHEN horizon_days <= 5 THEN '3-5d' ELSE '6-10d' END"),
            ("confidence", "CASE WHEN confidence < 50 THEN '<50' WHEN confidence < 70 THEN '50-70' ELSE '70+' END"),
        ):
            rows = conn.execute(
                f"SELECT {expr} AS bucket, count(*) AS n, count(graded_at) AS graded,"
                f" round(avg(alpha_net), 3) AS avg_alpha_net,"
                f" sum(CASE WHEN alpha_net > 0 THEN 1 ELSE 0 END) AS wins"
                f" FROM picks GROUP BY bucket"
            ).fetchall()
            by[dim] = [dict(r) for r in rows]
        debate = dict(conn.execute(
            "SELECT round(avg(CASE WHEN alpha_net IS NOT NULL AND"
            " ((adjusted_score > 50) = (alpha_net > 0)) THEN 1.0 ELSE 0.0 END), 3) AS post_debate_acc,"
            " round(avg(CASE WHEN alpha_net IS NOT NULL AND"
            " ((score > 50) = (alpha_net > 0)) THEN 1.0 ELSE 0.0 END), 3) AS pre_debate_acc"
            " FROM picks WHERE arm = 'COMMITTEE' AND graded_at IS NOT NULL"
        ).fetchone())
    return {"total": total, "by": by, "debate_lift": debate}


# ---------------------------------------------------------------------------
# Funnel + tokens
# ---------------------------------------------------------------------------

def funnel_add(ingested: int, candidates: int, picked: int, skipped: int,
               skip_reasons: list[dict]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO funnel (window_ts, ingested, candidates, picked, skipped, skip_reasons)"
            " VALUES (?,?,?,?,?,?)",
            (_now(), ingested, candidates, picked, skipped, json.dumps(skip_reasons[:20])),
        )


def funnel_recent(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM funnel ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def token_sink(role: str, model: str, tin: int, tout: int, decision_id: str | None) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO token_usage (ts, role, model, input_tok, output_tok, decision_id)"
            " VALUES (?,?,?,?,?,?)", (_now(), role, model, tin, tout, decision_id),
        )


def token_summary(days: int = 1) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, model, count(*) AS calls, sum(input_tok) AS input_tok,"
            " sum(output_tok) AS output_tok FROM token_usage"
            f" WHERE ts >= datetime('now', '-{int(days)} day') GROUP BY role, model"
            " ORDER BY output_tok DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def install_token_sink() -> None:
    from alphadesk import llm
    llm.set_token_sink(token_sink)


def save_relationship(from_sym: str, to_sym: str, direction: str, chain: str) -> None:
    """Cache a web-verified ripple relationship (the graph-lite grows on use)."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO relationships (ts, from_sym, to_sym, direction, chain)"
            " VALUES (?,?,?,?,?)",
            (_now(), from_sym.upper(), to_sym.upper(), direction, chain),
        )


def get_relationships(from_sym: str) -> list[dict]:
    """Previously-discovered ripple neighbors for a shocked company."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT to_sym, direction, chain FROM relationships WHERE from_sym = ?"
            " ORDER BY ts DESC", (from_sym.upper(),),
        ).fetchall()
    return [dict(r) for r in rows]


def relationship_count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT count(*) FROM relationships").fetchone()[0])


def add_run(kind: str, top_picks: list[dict]) -> None:
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO runs (ts, kind, top_picks) VALUES (?,?,?)",
                     (_now(), kind, json.dumps(top_picks)))


init()
