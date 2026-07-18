"""Position re-evaluation — the exit half of the desk.

The committee only ever OPENS positions. On each new run, before hunting for new
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
    "has moved since, and FRESH news, decide whether the original thesis still holds.\n"
    "EXIT if: the thesis is invalidated, the catalyst is spent/played out, fresh news "
    "cuts against it, the move has effectively reached its target, or a new adverse "
    "development raises the risk. HOLD if the thesis is intact with time left to play "
    "out. Be decisive and honest — the user is managing a real position on this. Give "
    "ONE sentence grounded in the actual move and news, not generic caution.\n"
    'Return ONLY JSON: {"decision": "HOLD|EXIT", "reason": "<one sentence>"}'
)

_SCHEMA = {
    "decision": {"type": str, "enum": ["HOLD", "EXIT"]},
    "reason": {"type": str, "maxlen": 300},
}


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

    headlines = [a.get("title", "")[:140] for a in articles[:6]] or ["(no fresh news in window)"]
    user = (
        f"Original call: {pick['direction']} {pick['symbol']}, {pick['horizon_days']}-day horizon, "
        f"opened {(pick.get('ts') or '')[:16]} UTC (conviction {pick.get('adjusted_score')}).\n"
        f"Thesis: {pick.get('thesis') or pick.get('triage_reason') or ''}\n"
        f"Entry: {entry} | now: {now} | move since entry: "
        f"{f'{move}%' if move is not None else 'n/a'}\n\n"
        f"Fresh news on {pick['symbol']}:\n" + wrap_data("news", "\n".join(headlines))
    )
    try:
        out = call_role("review", _SYSTEM, user, schema=_SCHEMA, decision_id=decision_id)
        out.pop("_downgraded_model", None)
        return out
    except Exception as exc:  # never auto-exit on a system failure
        log.warning("Re-eval failed for %s (%s) — defaulting HOLD", pick.get("symbol"), exc)
        return {"decision": "HOLD", "reason": f"(re-evaluation unavailable: {exc})"}
