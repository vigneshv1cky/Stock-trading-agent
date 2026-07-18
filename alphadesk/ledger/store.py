"""The decision ledger — SQLite (WAL). Every evaluation, token, and funnel count.

One row per evaluation (team or solo). Closed-market picks carry
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
    arm             TEXT NOT NULL,                 -- TEAM | LONER
    edge            TEXT,                          -- SPILLOVER | THEME | MOMENTUM
    trigger_src     TEXT NOT NULL,                 -- STREAM | DEEP_RUN | REPLAY
    session         TEXT NOT NULL,                 -- PRE | OPEN | AFTER | CLOSED
    -- decision
    direction       TEXT NOT NULL,                 -- LONG | SHORT
    horizon_days    INTEGER NOT NULL,
    score           REAL NOT NULL,                 -- pre-debate
    adjusted_score  REAL,                          -- post-debate (team only)
    confidence      REAL NOT NULL,
    verdict         TEXT,                          -- STRONG | SOFT | PASS
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
    graded_at       TEXT,
    -- position lifecycle: set when the Chief marks TAKE; re-evaluated on later runs
    taken           INTEGER NOT NULL DEFAULT 0,
    exit_ts         TEXT,                          -- early exit stamped by a re-eval
    exit_reason     TEXT
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

-- Scout skips, graded forward for missed moves (anti-survivorship). A skip has
-- no direction, so 'missed' = a large |move vs SPY| we chose not to even look at.
CREATE TABLE IF NOT EXISTS skips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    reason     TEXT,
    abs_alpha  REAL,        -- |symbol return − SPY| over the grade window, %
    missed     INTEGER,     -- 1 if abs_alpha crossed the miss threshold
    graded_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_skips_ts ON skips (ts);

-- Earnings calendar: who reported (with the EPS surprise) and who's about to.
-- Drives "be ready" (upcoming) + post-earnings-drift candidates (recently reported).
CREATE TABLE IF NOT EXISTS earnings (
    symbol       TEXT NOT NULL,
    report_date  TEXT NOT NULL,     -- ISO datetime of the report
    session      TEXT,              -- BMO (pre-open) | AMC (post-close) | DAY
    eps_estimate REAL,
    eps_actual   REAL,              -- NULL until reported
    surprise_pct REAL,              -- NULL until reported
    fetched_at   TEXT,
    UNIQUE(symbol, report_date) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings (report_date);

-- One web-grounded read per earnings event (results/guidance/reaction), cached so
-- we never re-web-search the same report across runs. Separate from `earnings` so
-- calendar refreshes (ON CONFLICT REPLACE) don't wipe the read.
CREATE TABLE IF NOT EXISTS earnings_reads (
    symbol      TEXT NOT NULL,
    report_date TEXT NOT NULL,
    report_read TEXT,
    ts          TEXT,
    UNIQUE(symbol, report_date) ON CONFLICT REPLACE
);

-- Persistent enrichment cache: an article's sentiment/category never changes, so
-- enrich it once and reuse forever. Kills the biggest recurring token cost —
-- re-enriching the same overlapping news on every run/restart.
CREATE TABLE IF NOT EXISTS enrichment_cache (
    article_id TEXT PRIMARY KEY,
    sentiment  REAL,
    label      TEXT,
    category   TEXT,
    relations  TEXT,       -- JSON [{a, rel, b}]
    ts         TEXT
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
        # idempotent migrations for pre-existing DBs (no-op once the column exists)
        for col, decl in (("taken", "INTEGER NOT NULL DEFAULT 0"),
                          ("exit_ts", "TEXT"), ("exit_reason", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # already migrated


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


def symbol_traces(symbol: str, days: int = 21) -> list[dict]:
    """Miss post-mortem: every team/solo evaluation of this symbol in the
    last `days` — whether it was approved or rejected, with the full transcript.
    Tells us the desk DID look at it and what it concluded."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, arm, edge, direction, horizon_days, score, adjusted_score,"
            " confidence, verdict, approved, triage_reason, thesis, debate, alpha_net"
            " FROM picks WHERE symbol = ? AND ts >= datetime('now', ?) ORDER BY id DESC",
            (symbol.upper(), f"-{int(days)} days"),
        ).fetchall()
    return [dict(r) for r in rows]


def symbol_skips(symbol: str, days: int = 21, scan: int = 500) -> list[dict]:
    """Miss post-mortem: scout skips that NAMED this symbol in the last `days`,
    with the stated reason — the desk saw it as a candidate and passed."""
    sym = symbol.upper()
    out: list[dict] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT window_ts, skip_reasons FROM funnel WHERE window_ts >= datetime('now', ?)"
            " ORDER BY id DESC LIMIT ?",
            (f"-{int(days)} days", scan),
        ).fetchall()
    for r in rows:
        try:
            for s in json.loads(r["skip_reasons"] or "[]"):
                if (s.get("symbol") or "").upper() == sym:
                    out.append({"window_ts": r["window_ts"], "reason": s.get("reason", "")})
        except Exception:
            continue
    return out


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
            " FROM picks WHERE arm = 'TEAM' AND graded_at IS NOT NULL"
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


def get_relationships(from_sym: str, days: int = 7) -> list[dict]:
    """Pre-search cache: ripple neighbors mapped for this shocked company within
    the last `days`. Lets the Connections desk reuse a prior web-verified mapping
    instead of re-running the web specialists for the same shock."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT to_sym, direction, chain, max(ts) AS ts FROM relationships"
            " WHERE from_sym = ? AND ts >= datetime('now', ?)"
            " GROUP BY to_sym, direction ORDER BY ts DESC",
            (from_sym.upper(), f"-{int(days)} days"),
        ).fetchall()
    return [dict(r) for r in rows]


def last_debate(symbol: str) -> dict | None:
    """The most recent team debate for `symbol` (ts + what it was about) — so a
    later run can tell 'same story' from a genuinely new catalyst."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ts, triage_reason, thesis FROM picks WHERE arm='TEAM' AND symbol=?"
            " ORDER BY id DESC LIMIT 1", (symbol.upper(),),
        ).fetchone()
    return dict(row) if row else None


def symbols_debated_since(hours: int = 12) -> set:
    """Symbols with a team debate in the last `hours` — skip re-debating them
    (anti-double-dip: an earnings/news name lingers as a candidate for days)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM picks WHERE arm='TEAM'"
            " AND ts >= datetime('now', ?)", (f"-{int(hours)} hours",),
        ).fetchall()
    return {r["symbol"].upper() for r in rows}


def mark_taken(pick_ids: list[int]) -> None:
    """Flag the picks the Chief chose to TAKE — the open positions later runs re-check."""
    if not pick_ids:
        return
    with _lock, _connect() as conn:
        conn.executemany("UPDATE picks SET taken=1 WHERE id=?", [(int(i),) for i in pick_ids])


def open_taken_picks() -> list[dict]:
    """TAKE picks still within their horizon, not exited, not yet graded — the
    open positions a fresh run should re-evaluate ('are you still in this trade?')."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, symbol, direction, horizon_days, adjusted_score, confidence,"
            " edge, thesis, entry_price, triage_reason FROM picks"
            " WHERE taken=1 AND exit_ts IS NULL AND graded_at IS NULL"
            "   AND datetime(ts, '+' || (horizon_days + 3) || ' days') >= datetime('now')"
            " ORDER BY id DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def record_exit(pick_id: int, reason: str) -> None:
    """Stamp an early exit issued by a position re-evaluation."""
    with _lock, _connect() as conn:
        conn.execute("UPDATE picks SET exit_ts=?, exit_reason=? WHERE id=?",
                     (_now(), reason, int(pick_id)))


def record_skips(skips: list[dict], cap: int = 30) -> None:
    """Persist scout skips individually so their forward moves can be graded
    (anti-survivorship: did we skip a name that then moved big?). Capped per
    window to bound later grading cost."""
    rows = [(_now(), (s.get("symbol") or "").upper(), (s.get("reason") or "")[:200])
            for s in (skips or [])[:cap] if s.get("symbol")]
    if not rows:
        return
    with _lock, _connect() as conn:
        conn.executemany("INSERT INTO skips (ts, symbol, reason) VALUES (?,?,?)", rows)


def due_skips(limit: int = 300) -> list[dict]:
    """Ungraded skips (the grader filters by whether the window has elapsed)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM skips WHERE graded_at IS NULL ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_skip(skip_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE skips SET {cols} WHERE id=?", (*fields.values(), int(skip_id)))


def false_negative_stats() -> dict:
    """The survivorship scorecard: how often the desk was wrong to say NO.
    - reject: graded TEAM picks it REJECTED that would have beaten SPY
      (alpha_net > 0 in the proposed direction — a passed-over winner).
    - skip:   graded scout skips that made a big move we never looked at."""
    with _connect() as conn:
        rej = dict(conn.execute(
            "SELECT count(*) AS graded,"
            " sum(CASE WHEN alpha_net > 0 THEN 1 ELSE 0 END) AS missed"
            " FROM picks WHERE arm='TEAM' AND approved=0 AND graded_at IS NOT NULL"
        ).fetchone())
        skp = dict(conn.execute(
            "SELECT count(*) AS graded, sum(CASE WHEN missed=1 THEN 1 ELSE 0 END) AS missed"
            " FROM skips WHERE graded_at IS NOT NULL"
        ).fetchone())
    return {"reject": rej, "skip": skp}


def get_enrichment(article_ids: list[str]) -> dict[str, dict]:
    """Cached enrichments for these article ids → {id: {sentiment,label,category,relations}}."""
    if not article_ids:
        return {}
    ph = ",".join("?" * len(article_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT article_id, sentiment, label, category, relations"
            f" FROM enrichment_cache WHERE article_id IN ({ph})", article_ids
        ).fetchall()
    return {r["article_id"]: dict(r) for r in rows}


def save_enrichment(items: list[dict]) -> None:
    """Persist genuine enrichment results (not failure fallbacks). Each item:
    {article_id, sentiment, label, category, relations:list}."""
    rows = [(i["article_id"], i["sentiment"], i["label"], i["category"],
             json.dumps(i["relations"]), _now()) for i in (items or [])]
    if not rows:
        return
    with _lock, _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO enrichment_cache"
            " (article_id, sentiment, label, category, relations, ts) VALUES (?,?,?,?,?,?)", rows)


def upsert_earnings(rows: list[dict]) -> None:
    """Insert/replace earnings-calendar rows. Each: {symbol, report_date, session,
    eps_estimate, eps_actual, surprise_pct}."""
    data = [(r["symbol"].upper(), r["report_date"], r.get("session"),
             r.get("eps_estimate"), r.get("eps_actual"), r.get("surprise_pct"), _now())
            for r in (rows or []) if r.get("symbol") and r.get("report_date")]
    if not data:
        return
    with _lock, _connect() as conn:
        conn.executemany(
            "INSERT INTO earnings (symbol, report_date, session, eps_estimate,"
            " eps_actual, surprise_pct, fetched_at) VALUES (?,?,?,?,?,?,?)", data)


def recently_reported(days: int = 3) -> list[dict]:
    """Companies that REPORTED in the last `days` (actual EPS known) — the
    post-earnings-drift candidate pool."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT symbol, report_date, session, eps_estimate, eps_actual, surprise_pct"
            " FROM earnings WHERE eps_actual IS NOT NULL"
            "   AND report_date >= datetime('now', ?) AND report_date <= datetime('now')"
            " ORDER BY report_date DESC", (f"-{int(days)} days",),
        ).fetchall()
    return [dict(r) for r in rows]


def upcoming_earnings(days: int = 7) -> list[dict]:
    """Companies REPORTING in the next `days` — the 'be ready' watch."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT symbol, report_date, session, eps_estimate FROM earnings"
            " WHERE eps_actual IS NULL AND report_date >= datetime('now')"
            "   AND report_date <= datetime('now', ?) ORDER BY report_date", (f"+{int(days)} days",),
        ).fetchall()
    return [dict(r) for r in rows]


def earnings_row(symbol: str, days: int = 4) -> dict | None:
    """The most recent report for `symbol` within `days` (if it has one) — used at
    brief time to decide whether to web-read the report."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT symbol, report_date, session, eps_estimate, eps_actual, surprise_pct"
            " FROM earnings WHERE symbol=? AND eps_actual IS NOT NULL"
            "   AND report_date >= datetime('now', ?) AND report_date <= datetime('now')"
            " ORDER BY report_date DESC LIMIT 1", (symbol.upper(), f"-{int(days)} days"),
        ).fetchone()
    return dict(row) if row else None


def get_earnings_read(symbol: str, report_date: str) -> str | None:
    """Cached web-grounded read for one earnings event (None if not yet read)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT report_read FROM earnings_reads WHERE symbol=? AND report_date=?",
            (symbol.upper(), report_date),
        ).fetchone()
    return row["report_read"] if row else None


def save_earnings_read(symbol: str, report_date: str, read: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO earnings_reads (symbol, report_date, report_read, ts) VALUES (?,?,?,?)",
            (symbol.upper(), report_date, read, _now()))


def add_run(kind: str, top_picks: list[dict]) -> None:
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO runs (ts, kind, top_picks) VALUES (?,?,?)",
                     (_now(), kind, json.dumps(top_picks)))


init()
