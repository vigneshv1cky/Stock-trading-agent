"""Position re-evaluation — the exit half of the desk.

The team only ever OPENS positions. On each new run, before hunting for new
trades, this re-checks every still-open TAKE against the current price and fresh
news and decides HOLD or EXIT — with a reason — because the user may have traded
the original call. Fail-safe: on any error it HOLDs (never auto-exits a real
position because the system hiccuped).
"""

import logging

from alphadesk.llm import call_role, wrap_data

log = logging.getLogger("alphadesk.review")

_SYSTEM = (
    "You are the position reviewer on a predictive trading desk. The desk earlier "
    "issued a call the user may have traded. Given the ORIGINAL call, how the stock "
    "has moved since, its RECENT intraday momentum, and FRESH news, decide whether the "
    "original thesis still holds.\n"
    "LET WINNERS RUN. A move STILL EXTENDING in your favor (at a fresh favorable "
    "extreme, momentum accelerating) is the continuation the desk is trying to ride — "
    "HOLD it. Do NOT bank a large unrealized gain merely because it's big or an options-"
    "implied 'spent move' window looks used up: a strong trend routinely overshoots the "
    "implied move, so 'spent' is a weak reason to exit a position still moving your way.\n"
    "EXIT only if: the thesis is invalidated, fresh news cuts against it, OR the move has "
    "clearly STALLED or REVERSED off its favorable peak (momentum rolling over) — then "
    "bank what's left. HOLD if the thesis is intact and the move is still working. Be "
    "decisive and honest — the user is managing a real position. Give ONE sentence "
    "grounded in the actual move, its recent momentum, and news — not generic caution.\n"
    'Return ONLY JSON: {"decision": "HOLD|EXIT", "reason": "<one sentence>"}'
)

_SCHEMA = {
    "decision": {"type": str, "enum": ["HOLD", "EXIT"]},
    "reason": {"type": str, "maxlen": 300},
}


def _momentum_read(pick: dict, now_price) -> dict | None:
    """Short-term trend of an OPEN position from intraday minute bars: is the move
    still EXTENDING in the position's favor, or STALLING / REVERSING off its peak? So
    the reviewer holds a running trend (the MOMENTUM edge) instead of banking it as a
    'spent move' — the NOW-short failure: exited +3% while it was still falling to +8%.
    Direction-aware. None if data/entry are unavailable (reviewer falls back to HOLD-safe)."""
    try:
        from alphadesk.config import entry_fill_time
        from alphadesk.ingest import prices
        if pick.get("entry_price") is None:
            return None
        entry = float(pick["entry_price"])
        fill = entry_fill_time(pick["ts"], pick.get("session"))
        bars = prices.intraday_bars(pick["symbol"], fill) if (fill and entry) else []
        if not bars:
            return None
        up = pick["direction"] == "LONG"

        def fav(p: float) -> float:            # favorable move %, direction-aware
            return ((p - entry) if up else (entry - p)) / entry * 100

        cur = float(now_price) if now_price else bars[-1]["close"]
        fav_now = fav(cur)
        ext = max(b["high"] for b in bars) if up else min(b["low"] for b in bars)
        peak = fav(ext)                        # best favorable move reached in the hold
        recent = bars[-20:] if len(bars) >= 20 else bars   # ~last 20 min
        slope = fav(recent[-1]["close"]) - fav(recent[0]["close"])   # favorable move over that window
        return {"fav_now": round(fav_now, 2), "peak": round(peak, 2),
                "slope_20m": round(slope, 2), "off_peak": round(peak - fav_now, 2)}
    except Exception:
        return None


def review_position(pick: dict, price_ctx: dict | None, articles: list[dict],
               decision_id: str | None = None) -> dict:
    """Re-check one open position → {decision, reason}. HOLD is the safe default."""
    move = None
    entry = pick.get("entry_price")
    now = (price_ctx or {}).get("last_price")
    try:
        if entry and now:
            move = round((float(now) - float(entry)) / float(entry) * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        move = None

    mo = _momentum_read(pick, now)
    if mo:
        if mo["slope_20m"] > 0.1 and mo["off_peak"] <= 0.3:
            trend = "STILL EXTENDING in its favor (fresh extreme, momentum intact) — bias HOLD"
        elif mo["off_peak"] <= 0.3:
            trend = "at its favorable peak but flat over the last ~20m (stalling)"
        else:
            trend = f"PULLED BACK {mo['off_peak']:.1f}% off its favorable peak (momentum rolling over)"
        momentum_line = (
            f"Recent momentum (intraday): now {mo['fav_now']:+.1f}% in favor, "
            f"peak {mo['peak']:+.1f}%, last ~20m {mo['slope_20m']:+.1f}% → {trend}.\n"
        )
    else:
        momentum_line = ""

    headlines = [a.get("title", "")[:140] for a in articles[:6]] or ["(no fresh news in window)"]
    user = (
        f"Original call: {pick['direction']} {pick['symbol']}, {pick['horizon_days']}-day horizon, "
        f"opened {(pick.get('ts') or '')[:16]} UTC (conviction {pick.get('adjusted_score')}).\n"
        f"Thesis: {pick.get('thesis') or pick.get('triage_reason') or ''}\n"
        f"Entry: {entry} | now: {now} | move since entry: "
        f"{f'{move}%' if move is not None else 'n/a'}\n"
        f"{momentum_line}\n"
        f"Fresh news on {pick['symbol']}:\n" + wrap_data("news", "\n".join(headlines))
    )
    try:
        out = call_role("review", _SYSTEM, user, schema=_SCHEMA, decision_id=decision_id)
        out.pop("_downgraded_model", None)
        return out
    except Exception as exc:  # never auto-exit on a system failure
        log.warning("Re-eval failed for %s (%s) — defaulting HOLD", pick.get("symbol"), exc)
        return {"decision": "HOLD", "reason": f"(re-evaluation unavailable: {exc})"}
