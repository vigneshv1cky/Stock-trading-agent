"""research_run() — THE pipeline. Pure composition; identical path for live
windows, deep runs, and (later) replay.

    candidates → SCOUT → per pick (parallel, capped):
        3 brief subagents (parallel) → RESEARCHER → CRITIC → fact-check →
        RESEARCHER rebuttal → JUDGE → ledger row
        (+ every Nth pick: LONER arm, independent, same briefs)

Fail-safe doctrine: any stage failure drops that candidate with a logged
reason. Hard caps enforced in code. Every window writes funnel counters.
"""

import asyncio
import logging
import time
import uuid

from alphadesk.config import (
    MAX_CONCURRENT_WORKFLOWS,
    MAX_DEBATES_PER_DAY,
    MODEL_MAP,
    SOLO_ARM_EVERY_N,
    SYMBOL_REPICK_COOLDOWN_MIN,
    session,
)
from alphadesk.desk import debate, gate, loner, notes, scout, team
from alphadesk.ingest import prices
from alphadesk.ledger import store
from alphadesk.llm import LLMError

log = logging.getLogger("alphadesk.workflow")

_repick_at: dict[str, float] = {}     # symbol → earliest next-pick monotonic time
_pick_counter = 0                     # drives the solo arm cadence
_cooldowns_seeded = False


def _seed_cooldowns_from_ledger() -> None:
    """Restart amnesia guard: rebuild re-pick cooldowns from recent ledger
    rows so a process restart never re-debates what it just decided."""
    global _cooldowns_seeded
    _cooldowns_seeded = True
    try:
        import sqlite3
        from datetime import datetime, timezone
        from alphadesk.config import DATA_DIR
        with sqlite3.connect(DATA_DIR / "ledger.db") as conn:
            rows = conn.execute(
                "SELECT symbol, max(ts) FROM picks WHERE arm='TEAM'"
                f" AND ts >= datetime('now', '-{SYMBOL_REPICK_COOLDOWN_MIN} minutes')"
                " GROUP BY symbol"
            ).fetchall()
        now_mono, now_utc = time.monotonic(), datetime.now(timezone.utc)
        for sym, ts in rows:
            age_s = (now_utc - datetime.fromisoformat(ts)).total_seconds()
            remaining = SYMBOL_REPICK_COOLDOWN_MIN * 60 - age_s
            if remaining > 0:
                _repick_at[sym] = now_mono + remaining
        if rows:
            log.info("Seeded %d re-pick cooldowns from ledger", len(rows))
    except Exception as exc:
        log.warning("Cooldown seeding failed: %s", exc)


def _headline_rows(articles: list[dict]) -> list[str]:
    return [
        f"[{a.get('category', '?')}] {a.get('title','')[:120]} "
        f"(sent={a['mentions'][0]['sentiment'] if a.get('mentions') else '?'})"
        for a in articles[:4]
    ]


def _avg_sentiment(articles: list[dict]) -> float:
    vals = [m["sentiment"] for a in articles for m in a.get("mentions", [])]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


async def _gather_briefs(loop, sym: str, articles: list[dict], price_ctx: dict | None,
                         decision_id: str) -> list[dict]:
    fundamentals, opts = await asyncio.gather(
        loop.run_in_executor(None, prices.get_fundamentals, sym),
        loop.run_in_executor(None, prices.get_options_context, sym),
    )
    return list(await asyncio.gather(
        loop.run_in_executor(None, notes.market_brief, sym, price_ctx, fundamentals, articles, decision_id, opts),
        loop.run_in_executor(None, notes.news_brief, sym, articles, decision_id),
    ))


async def _run_committee(loop, sym: str, pick: dict, articles: list[dict],
                         price_ctx: dict | None, trigger_src: str) -> int | None:
    decision_id = f"{sym}-{uuid.uuid4().hex[:8]}"
    try:
        briefs = await _gather_briefs(loop, sym, articles, price_ctx, decision_id)
        history = await loop.run_in_executor(None, store.symbol_history, sym)
        calibration = team.calibration_block(
            await loop.run_in_executor(None, store.stats))
        # shared team core — same debate + ledger write as the streaming path
        result = None
        async for ev in debate.deliberate(sym, pick, briefs, price_ctx, history,
                                          calibration, trigger_src, decision_id):
            if ev["type"] == "_result":
                result = ev
    except LLMError as exc:
        log.warning("Team dropped %s: %s", sym, exc)
        return None
    if result is None:
        return None

    pick_id = result["pick_id"]
    thesis, verdict = result["thesis"], result["verdict"]
    log.info(
        "DECISION #%d %s %s %dd score %.0f→%.0f [%s] approved=%s (%s)",
        pick_id, sym, thesis["direction"], thesis["horizon_days"],
        thesis["score"], verdict["adjusted_score"], verdict["verdict"],
        bool(verdict["approved"]), pick.get("edge_hint"),
    )

    # solo control arm on every Nth pick — independent, same evidence
    sess = session()
    global _pick_counter
    _pick_counter += 1
    if SOLO_ARM_EVERY_N and _pick_counter % SOLO_ARM_EVERY_N == 0:
        try:
            s = await loop.run_in_executor(
                None, lambda: loner.loner_analysis(sym, pick["reason"], briefs, history,
                                                 decision_id + "-solo", calibration))
            solo_model = s.pop("_downgraded_model", MODEL_MAP["loner"])
            store.record_pick({
                "symbol": sym, "arm": "LONER", "edge": pick.get("edge_hint"),
                "trigger_src": trigger_src, "session": sess,
                "direction": s["direction"], "horizon_days": s["horizon_days"],
                "score": s["score"], "confidence": s["confidence"],
                "approved": int(bool(s["approved"])),  # the solo agent's own call
                "triage_reason": pick["reason"], "thesis": s["thesis"],
                "briefs": briefs, "model_tags": {"loner": solo_model},
                "low_liquidity": int(bool(price_ctx and price_ctx.get("low_liquidity"))),
                "entry_price": (price_ctx or {}).get("last_price") if sess == "OPEN" else None,
                "spy_price": ((prices.get_context("SPY") or {}).get("last_price")),
            })
            log.info("LONER arm: %s %s %dd score=%.0f", sym, s["direction"],
                     s["horizon_days"], s["score"])
        except LLMError as exc:
            log.warning("Solo arm dropped %s: %s", sym, exc)

    return pick_id


async def research_run(candidates: dict[str, list[dict]], trigger_src: str = "STREAM") -> list[int]:
    """One full pass: scout the candidate window, deliberate the picks.

    candidates: symbol → fresh enriched articles. Returns ledger pick ids.
    """
    loop = asyncio.get_running_loop()
    if not _cooldowns_seeded:
        _seed_cooldowns_from_ledger()
    now = time.monotonic()

    eligible = {
        sym: arts for sym, arts in candidates.items()
        if _repick_at.get(sym, 0.0) <= now
    }
    if not eligible:
        return []

    if store.picks_today("TEAM") >= MAX_DEBATES_PER_DAY:
        log.warning("Daily debate cap reached (%d) — window skipped", MAX_DEBATES_PER_DAY)
        return []

    # window snapshot: headlines + price evidence per symbol
    window: dict[str, dict] = {}
    for sym, arts in list(eligible.items())[:40]:
        price_ctx = await loop.run_in_executor(None, prices.get_context, sym)
        window[sym] = {
            "headlines": _headline_rows(arts),
            "avg_sentiment": _avg_sentiment(arts),
            "price": price_ctx,
        }
    movers = await loop.run_in_executor(None, prices.movers)

    try:
        result = await loop.run_in_executor(None, scout.run_scout, window, movers)
    except LLMError as exc:
        log.warning("Scout failed — window dropped: %s", exc)
        store.funnel_add(len(candidates), len(window), 0, len(window),
                         [{"symbol": "*", "reason": f"scout failed: {exc}"}])
        return []

    picks = result.get("picks", [])
    skips = result.get("skips", []) or []
    store.funnel_add(len(candidates), len(window), len(picks), len(skips),
                     [{"symbol": s.get("symbol", "?"), "reason": s.get("reason", "")} for s in skips])
    store.record_skips(skips)  # grade forward: did we skip a mover? (anti-survivorship)
    for p in picks:
        log.info("SCOUT PICK %s [%s]: %s", p["symbol"], p["edge_hint"], p["reason"])

    # Pre-debate catalyst gate — drop phantom setups before the expensive debate
    # (cheap haiku, fail-open). Parity with the streaming path.
    verdicts = await asyncio.gather(*[
        loop.run_in_executor(None, gate.screen_catalyst, p["symbol"], p.get("reason", ""),
                             p.get("edge_hint"), eligible.get(p["symbol"], []), f"gate-{p['symbol']}")
        for p in picks
    ])
    gate_drops, kept = [], []
    for p, v in zip(picks, verdicts):
        if v["tradeable"]:
            kept.append(p)
        else:
            gate_drops.append({"symbol": p["symbol"], "reason": f"gated: {v['reason']}"})
            log.info("GATE drop %s: %s", p["symbol"], v["reason"])
    if gate_drops:
        store.record_skips(gate_drops)
    picks = kept
    if not picks:
        return []

    sem = asyncio.Semaphore(MAX_CONCURRENT_WORKFLOWS)

    async def _guarded(p: dict) -> int | None:
        async with sem:
            sym = p["symbol"]
            _repick_at[sym] = time.monotonic() + SYMBOL_REPICK_COOLDOWN_MIN * 60
            price_ctx = window.get(sym, {}).get("price")
            return await _run_committee(loop, sym, p, eligible.get(sym, []), price_ctx, trigger_src)

    ids = await asyncio.gather(*(_guarded(p) for p in picks))
    return [i for i in ids if i is not None]
