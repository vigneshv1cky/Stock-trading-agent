"""The execution desk — turns a committed directional call into an ACTIONABLE
swing trade plan: entry, target, stop, and a plain one-line instruction.

The direction and horizon are already FIXED by the team (researcher→critic→judge);
the planner does NOT re-decide them. It only reads the stock's recent price
behaviour (facts the desk already pulled) and sets levels sized to that behaviour
and the horizon. Levels come from an agent (judgment), never a hardcoded ATR
multiple (Design law: agents own judgment, code owns arithmetic + the coherence
rails below).

Fail-open: a missing or incoherent plan never drops the pick — it just books
without a plan (the directional call still stands). So trade_plan() returns None
rather than raising.
"""

import logging

from alphadesk.llm import LLMError, call_role

log = logging.getLogger("alphadesk.plan")

_SYSTEM = (
    "You are the execution desk of a swing-trading research team. The desk has "
    "ALREADY committed to a directional call — you do NOT re-decide the direction "
    "or horizon. Turn that call into a concrete, actionable trade plan from the "
    "stock's recent price behaviour:\n"
    "  • entry — the price to get in, at or near the current price (a swing entry "
    "you'd act on now or on a minor pullback), NOT a far-off limit.\n"
    "  • target — a realistic objective for THIS horizon, sized to the stock's "
    "recent range/volatility. Do not invent a move far larger than it typically "
    "makes over that many days.\n"
    "  • stop — the invalidation: the price that says the thesis is wrong. Place it "
    "beyond normal daily noise but keep the risk sane (a tighter stop for a "
    "single-day hold, more room for a multi-day one).\n"
    "  • note — ONE plain-English line telling a trader exactly what to do.\n"
    "COHERENCE (required): for LONG, stop < entry < target. For SHORT, "
    "target < entry < stop. Keep entry within a few percent of the current price.\n"
    'Return ONLY JSON: {"entry": <price>, "target": <price>, "stop": <price>, '
    '"note": "<one line>"}'
)

_SCHEMA = {
    "entry": {"type": (int, float), "min": 0},
    "target": {"type": (int, float), "min": 0},
    "stop": {"type": (int, float), "min": 0},
    "note": {"type": str, "maxlen": 240},
}


def _coherent(direction: str, entry: float, target: float, stop: float,
              last_price: float | None) -> bool:
    """Rails on the agent's levels — reject a plan that contradicts the direction
    or floats absurdly far from the current price (keep the pick, drop the plan)."""
    if min(entry, target, stop) <= 0:
        return False
    if direction == "LONG" and not (stop < entry < target):
        return False
    if direction == "SHORT" and not (target < entry < stop):
        return False
    if last_price and abs(entry - last_price) / last_price > 0.20:
        return False  # entry way off the current price — not an executable swing entry
    return True


def level_crossed(direction: str, price: float, target: float, stop: float) -> str | None:
    """Which committed plan level the current price has reached, if any: 'target',
    'stop', or None. Pure arithmetic — a crossed level is a FACT that both the live
    view and the position watcher key on (code owns physics/rails, not judgment),
    so they share this one definition and can never disagree on what 'hit' means."""
    up = direction == "LONG"
    if (price >= target) if up else (price <= target):
        return "target"
    if (price <= stop) if up else (price >= stop):
        return "stop"
    return None


def trade_plan(symbol: str, direction: str, horizon_days: int,
               price_ctx: dict | None, thesis: str,
               decision_id: str | None = None) -> dict | None:
    """Actionable plan for a committed call → {entry, target, stop, note, hold}
    or None (fail-open: no plan, pick still stands)."""
    if not price_ctx or price_ctx.get("last_price") is None:
        return None  # no price to anchor levels — skip the plan, keep the pick
    last = price_ctx.get("last_price")
    hold = "single-day" if horizon_days <= 1 else "multi-day"
    user = (
        f"Symbol: {symbol}\n"
        f"Committed call: {direction} over {horizon_days} trading day(s) ({hold} swing).\n"
        f"Thesis: {thesis[:400]}\n\n"
        "Recent price context (facts):\n"
        f"- current price: {last}\n"
        f"- move today: {price_ctx.get('change_today_pct')}%\n"
        f"- move 5d: {price_ctx.get('change_5d_pct')}%\n"
        f"- move 20d: {price_ctx.get('change_20d_pct')}%\n"
        f"- 90d high / low: {price_ctx.get('high_90d')} / {price_ctx.get('low_90d')}\n"
        f"- last 10 daily closes: {price_ctx.get('closes_10d')}\n"
    )
    try:
        out = call_role("plan", _SYSTEM, user, schema=_SCHEMA, decision_id=decision_id)
    except LLMError as exc:
        log.info("Trade plan skipped for %s (%s)", symbol, exc)
        return None
    out.pop("_downgraded_model", None)
    entry, target, stop = float(out["entry"]), float(out["target"]), float(out["stop"])
    if not _coherent(direction, entry, target, stop, last):
        log.info("Incoherent plan for %s %s (e=%s t=%s s=%s) — dropped",
                 symbol, direction, entry, target, stop)
        return None
    return {"entry": round(entry, 4), "target": round(target, 4),
            "stop": round(stop, 4), "note": out["note"], "hold": hold}
