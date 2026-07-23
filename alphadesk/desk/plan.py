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
    "  • order — 'market' if the thesis needs you IN immediately (momentum already "
    "running, a catalyst you must not miss — fill at the open/current price), or "
    "'limit' if the plan is to wait for a specific entry level (a pullback / better "
    "price); a limit fills ONLY if price reaches the entry, else the trade is skipped.\n"
    "COHERENCE (required): for LONG, stop < entry < target. For SHORT, "
    "target < entry < stop. Keep entry within a few percent of the current price.\n"
    'Return ONLY JSON: {"entry": <price>, "target": <price>, "stop": <price>, '
    '"note": "<one line>", "order": "market|limit"}'
)

_SCHEMA = {
    "entry": {"type": (int, float), "min": 0},
    "target": {"type": (int, float), "min": 0},
    "stop": {"type": (int, float), "min": 0},
    "note": {"type": str, "maxlen": 240},
    "order": {"type": str, "enum": ["market", "limit"], "optional": True},
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


def realized_exit(direction: str, entry, exit_price, spy_then, spy_now) -> dict:
    """Realized performance of a position closed at exit_price: raw return
    (direction-aware) and alpha vs SPY over the SAME holding window, net of
    round-trip friction. Fields are None when a baseline is missing. Frozen at the
    exit — distinct from the horizon grade (alpha_net), which still settles at the
    declared horizon and measures the CALL's edge regardless of when we got out.
    This is the ONE definition the live mark, the exit stamp, and the UI share."""
    from alphadesk.config import FRICTION_BPS_PER_SIDE
    out: dict = {"exit_price": round(float(exit_price), 4) if exit_price else None,
                 "exit_return_pct": None, "exit_alpha": None}
    if not (entry and exit_price):
        return out
    sign = 1.0 if direction == "LONG" else -1.0
    ret = sign * (exit_price - entry) / entry * 100
    out["exit_return_pct"] = round(ret, 3)
    if spy_then and spy_now:
        spy_ret = sign * (spy_now - spy_then) / spy_then * 100
        friction = 2 * FRICTION_BPS_PER_SIDE / 100.0
        out["exit_alpha"] = round(ret - spy_ret - friction, 3)
    return out


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


def limit_fill(direction: str, order_type: str | None, entry: float | None,
               open_px: float | None, high_px: float | None, low_px: float | None,
               buffer_pct: float, stop: float | None = None,
               min_cushion_frac: float = 0.0) -> float | None:
    """The Model-A fill PRICE for a pick, given its fill-day OHLC — or None if a
    LIMIT order didn't fill ('not taken'). A fact, not a judgment:
      • market (or no entry) → fill at the open (you're in immediately).
      • limit LONG → filled if price traded down to the entry (within buffer): at the
        open if it gapped at/below the level, else at the level; not filled if it
        never came down.
      • limit SHORT → mirror (filled if price traded UP to the entry within buffer).
    The buffer widens the trigger so a near-miss still fills.

    Gap-toward-invalidation guard (limit only): the stop is the plan's invalidation.
    If the fill already sits most of the way from the planned entry TO the stop — i.e.
    the reaction gapped against the thesis before you'd enter — the edge is gone and
    you'd fill one nudge from being stopped, so it's NOT TAKEN. Keeps ≥
    min_cushion_frac of the planned entry→stop cushion."""
    b = max(0.0, buffer_pct) / 100.0
    if order_type != "limit" or not entry or open_px is None:
        px: float | None = open_px
    elif direction == "LONG":
        if open_px <= entry:                       # gapped at/below the limit → fill at open
            px = round(open_px, 4)
        elif low_px is not None and low_px <= entry * (1 + b):   # dipped into the (buffered) limit
            px = round(entry, 4)
        else:
            px = None                              # never reached → not taken
    elif open_px >= entry:                         # SHORT: gapped at/above the limit → fill at open
        px = round(open_px, 4)
    elif high_px is not None and high_px >= entry * (1 - b):
        px = round(entry, 4)
    else:
        px = None
    if px is None:
        return None
    if order_type == "limit" and stop and entry and min_cushion_frac > 0:
        planned = abs(entry - stop)                # planned distance to invalidation
        cushion = (stop - px) if direction == "SHORT" else (px - stop)   # actual room left
        if planned > 0 and cushion < min_cushion_frac * planned:
            return None                            # gapped against the thesis → not taken
    return px


def exit_signal(direction: str, entry: float | None, cur: float | None,
                target: float | None, stop: float | None,
                peak_fav_pct: float) -> str | None:
    """Cheap, pure-code SCREEN that flags an open position for a (costly) opus
    thesis re-review — it is NOT an exit itself. Returns a short reason string or
    None. Two triggers, both about a move that has largely played out:
      • near target — most of the entry→target move is captured (take it before it
        gives back, rather than waiting for the exact level the watcher keys on).
      • give-back — the favorable move ran up past a floor then faded a chunk of
        its peak (the MFE-decay case: a beat that popped and is now leaking).
    Code owns this cheap watching (physics/rails); the reviewer owns the judgment.
    Deliberately generous — false flags just cost one review, which HOLDs."""
    from alphadesk.config import (EXIT_GIVEBACK_FRAC, EXIT_GIVEBACK_MIN_PEAK,
                                  EXIT_NEAR_TARGET_FRAC)
    if not (entry and cur and target and stop):
        return None
    up = direction == "LONG"
    span = (target - entry) if up else (entry - target)
    if span > 0:
        progress = ((cur - entry) if up else (entry - cur)) / span
        if progress >= EXIT_NEAR_TARGET_FRAC:
            return f"near target — {progress:.0%} of the move captured"
    if peak_fav_pct >= EXIT_GIVEBACK_MIN_PEAK:
        fav = (cur - entry) / entry * 100 * (1 if up else -1)
        if fav <= peak_fav_pct * (1 - EXIT_GIVEBACK_FRAC):
            return f"faded to {fav:+.1f}% from a +{peak_fav_pct:.1f}% peak"
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
    order = out.get("order") if out.get("order") in ("market", "limit") else "market"
    return {"entry": round(entry, 4), "target": round(target, 4),
            "stop": round(stop, 4), "note": out["note"], "hold": hold, "order": order}
