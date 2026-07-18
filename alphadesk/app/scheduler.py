"""The scheduler — AlphaDesk thinks 24/7; only entries follow the market clock.

  • ingestion loop: 24/7 news polling → graph + candidate accumulation
  • window loop:    120s scout windows AROUND THE CLOCK — the world doesn't
                    close; closed-market decisions enter at the next open
  • batch runs:     pre-market (~07:30 ET) and evening (~17:30 ET) synthesis
                    passes over the full accumulated picture
  • grader loop:    hourly outcome grading
  • sentinel loop:  hourly anomaly checks (approval rate, token burn) — alarms
                    pause new windows until the process is restarted/acked
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from alphadesk.config import (
    NEWS_POLL_INTERVAL_S,
    TRIAGE_WINDOW_S,
    now_et,
    session,
)
from alphadesk.desk.workflow import research_run
from alphadesk.ingest import news
from alphadesk.ledger import store
from alphadesk.ledger.grader import grade_due

log = logging.getLogger("alphadesk.scheduler")

_pending: dict[str, list[dict]] = {}
_paused_reason: str | None = None
_batch_done: dict[str, str] = {}  # kind → date string of last run
_last_heartbeat: float = 0.0      # monotonic ts of the last completed ingest cycle


def heartbeat_age_s() -> float:
    """Seconds since the ingest loop last completed a cycle (inf if never)."""
    import time
    if _last_heartbeat == 0.0:
        return float("inf")
    return time.monotonic() - _last_heartbeat


def _merge_candidates(new: dict[str, list[dict]]) -> None:
    for sym, arts in new.items():
        _pending.setdefault(sym, []).extend(arts)
        _pending[sym] = _pending[sym][-8:]


def _drain_pending() -> dict[str, list[dict]]:
    global _pending
    out, _pending = _pending, {}
    return out


async def _ingest_loop() -> None:
    global _last_heartbeat
    import time as _time
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    while True:
        try:
            loop = asyncio.get_running_loop()
            n, candidates = await loop.run_in_executor(None, news.poll, since)
            since = datetime.now(timezone.utc) - timedelta(minutes=10)
            if candidates:
                _merge_candidates(candidates)
            _last_heartbeat = _time.monotonic()  # liveness for /healthz
        except Exception as exc:
            log.error("ingest loop error: %s", exc)
        await asyncio.sleep(NEWS_POLL_INTERVAL_S)


async def _world_loop() -> None:
    """GDELT world-news tick every 15 min — 3 taxonomy categories per tick,
    full 11-category coverage roughly hourly. Candidates join the same
    pending pool the scout windows drain."""
    from alphadesk.ingest import world
    while True:
        try:
            loop = asyncio.get_running_loop()
            n, candidates = await loop.run_in_executor(None, world.poll)
            if candidates:
                _merge_candidates(candidates)
        except Exception as exc:
            log.error("world loop error: %s", exc)
        await asyncio.sleep(900)


async def _window_loop() -> None:
    """Deliberation runs 24/7 — the engine understands world trade, and the
    world doesn't close. Decisions made while US markets are closed are
    stamped session=CLOSED/PRE/AFTER and enter at the next open (grader
    handles it) — and a call timestamped BEFORE any market could react is
    the purest evidence of prediction."""
    while True:
        await asyncio.sleep(TRIAGE_WINDOW_S)
        if _paused_reason:
            continue
        if not _pending:
            continue
        try:
            ids = await research_run(_drain_pending(), trigger_src="STREAM")
            if ids:
                log.info("Window produced %d decisions (session=%s)", len(ids), session())
        except Exception as exc:
            log.error("window loop error: %s", exc)


async def _batch_loop() -> None:
    """Pre-market and evening deep passes over accumulated closed-market news."""
    while True:
        await asyncio.sleep(60)
        if _paused_reason:
            continue
        et = now_et()
        today = et.date().isoformat()
        try:
            if et.hour == 7 and et.minute >= 30 and _batch_done.get("PREMARKET") != today:
                _batch_done["PREMARKET"] = today
                if _pending:
                    log.info("PRE-MARKET deep run: %d accumulated symbols", len(_pending))
                    await research_run(_drain_pending(), trigger_src="DEEP_RUN")
            if et.hour == 17 and et.minute >= 30 and _batch_done.get("EVENING") != today:
                _batch_done["EVENING"] = today
                if _pending:
                    log.info("EVENING deep run: %d accumulated symbols", len(_pending))
                    await research_run(_drain_pending(), trigger_src="DEEP_RUN")
        except Exception as exc:
            log.error("batch loop error: %s", exc)


async def _grader_loop() -> None:
    while True:
        try:
            loop = asyncio.get_running_loop()
            n = await loop.run_in_executor(None, grade_due)
            if n:
                log.info("Graded %d picks", n)
        except Exception as exc:
            log.error("grader loop error: %s", exc)
        await asyncio.sleep(3600)


async def _sentinel_loop() -> None:
    """Anomaly alarms: pause new windows, never touch existing records."""
    global _paused_reason
    while True:
        await asyncio.sleep(3600)
        try:
            s = store.stats()
            total = s["total"]
            today_committee = store.picks_today("TEAM")
            if today_committee >= 10:
                with_approval = [b for b in s["by"]["arm"] if b["bucket"] == "TEAM"]
                if with_approval and total["picks"]:
                    # crude day-level approval-rate alarm
                    import sqlite3
                    from alphadesk.config import DATA_DIR
                    with sqlite3.connect(DATA_DIR / "ledger.db") as c:
                        row = c.execute(
                            "SELECT avg(approved) FROM picks WHERE arm='TEAM'"
                            " AND ts >= date('now')"
                        ).fetchone()
                    if row and row[0] is not None and row[0] > 0.8:
                        _paused_reason = f"approval rate {row[0]:.0%} today — possible prompt drift"
                        log.critical("SENTINEL PAUSE: %s", _paused_reason)
            tokens = store.token_summary(days=1)
            burn_today = sum(t["output_tok"] for t in tokens)
            week = store.token_summary(days=7)
            burn_week_avg = sum(t["output_tok"] for t in week) / 7 if week else 0
            if burn_week_avg and burn_today > 2 * burn_week_avg and burn_today > 200_000:
                _paused_reason = f"token burn {burn_today:,} > 2× trailing avg"
                log.critical("SENTINEL PAUSE: %s", _paused_reason)
        except Exception as exc:
            log.error("sentinel error: %s", exc)


def paused() -> str | None:
    return _paused_reason


async def run_forever() -> None:
    store.install_token_sink()
    log.info("AlphaDesk scheduler up — session=%s", session())
    await asyncio.gather(
        _ingest_loop(), _world_loop(), _window_loop(), _batch_loop(),
        _grader_loop(), _sentinel_loop(),
    )
