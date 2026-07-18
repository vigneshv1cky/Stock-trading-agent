"""Same-story vs new-catalyst check.

The anti-double-dip cooldown skips a recently-debated name — but a BIG new
development (fresh lawsuit/ruling, new guidance, M&A, major contract, regulatory
action) on that same name SHOULD get a fresh look. This haiku check reads the
news that arrived SINCE the last debate and decides: genuinely new material
catalyst, or the same story rehashed. Fails OPEN (treat as fresh) — missing a
real catalyst is worse than one extra debate.
"""

import logging

from alphadesk.llm import call_role, wrap_data

log = logging.getLogger("alphadesk.materiality")

_SYSTEM = (
    "A company was analyzed recently. Decide whether the news SINCE THEN is a "
    "genuinely NEW, material catalyst — a distinct event (new lawsuit or ruling, "
    "fresh guidance, M&A, major contract/product, regulatory action, leadership "
    "change) that could move the stock ON ITS OWN — or just the SAME story "
    "(follow-up coverage, rehash, opinion/analysis of what was already known).\n"
    "Bias toward FRESH when genuinely uncertain: missing a real new catalyst is "
    "worse than one extra look. But do NOT call routine follow-up coverage 'new'.\n"
    'Return ONLY JSON: {"fresh_catalyst": true|false, "reason": "<one sentence>"}'
)

_SCHEMA = {
    "fresh_catalyst": {"type": bool},
    "reason": {"type": str, "maxlen": 300},
}


def fresh_catalyst(symbol: str, last: dict | None, new_articles: list[dict],
                   decision_id: str | None = None) -> dict:
    """Is the news since the last debate a new material catalyst? {fresh_catalyst, reason}."""
    prior = (last or {}).get("triage_reason") or (last or {}).get("thesis") or "n/a"
    heads = "\n".join(f"- {a.get('title', '')[:150]}" for a in new_articles[:8]) or "none"
    user = (
        f"Company: {symbol}\nLast analyzed: {((last or {}).get('ts') or '?')[:16]}\n"
        f"What it was about: {prior[:400]}\n\nNews that arrived SINCE then:\n"
        + wrap_data("news", heads)
    )
    try:
        out = call_role("materiality", _SYSTEM, user, schema=_SCHEMA, decision_id=decision_id)
        out.pop("_downgraded_model", None)
        return out
    except Exception as exc:  # fail open — don't let a check error hide a real catalyst
        log.warning("materiality check failed for %s (%s) — treating as fresh", symbol, exc)
        return {"fresh_catalyst": True, "reason": f"(check unavailable: {exc})"}
