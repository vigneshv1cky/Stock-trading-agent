"""Specialist brief subagents — ephemeral, parallel, haiku.

Two one-shot workers per candidate: market (price structure + valuation +
priced-in/legs, from code-computed numbers) and news (what was actually said).
Each returns a compact evidence block the team argues from.
"""

import json
import logging

from alphadesk.llm import LLMError, call_role, wrap_data

log = logging.getLogger("alphadesk.notes")

_BRIEF_SCHEMA = {
    "summary": {"type": str, "maxlen": 600},
    "key_facts": {"type": list, "maxitems": 5, "items": {"fact": {"type": str, "maxlen": 200}}},
}


def _brief(kind: str, instructions: str, payload: str, decision_id: str | None) -> dict:
    try:
        out = call_role(
            "brief",
            f"You are the {kind} research specialist on a stock research desk. {instructions} "
            'Be factual and terse — no speculation. Return ONLY JSON: '
            '{"summary": "<4 sentences max>", "key_facts": [{"fact": "..."}]}',
            payload,
            schema=_BRIEF_SCHEMA,
            decision_id=decision_id,
        )
        out["kind"] = kind
        return out
    except LLMError as exc:
        log.warning("%s brief failed: %s", kind, exc)
        return {"kind": kind, "summary": f"({kind} brief unavailable)", "key_facts": []}


def news_brief(symbol: str, articles: list[dict], decision_id: str | None = None) -> dict:
    lines = "\n".join(
        f"- [{a.get('published_at','')[:16]}] ({a.get('source','')}) {a.get('title','')}"
        + (f" — {a.get('summary','')[:150]}" if a.get("summary") else "")
        + f" | sentiment={a['mentions'][0]['sentiment'] if a.get('mentions') else '?'}"
        for a in articles[:10]
    )
    return _brief(
        "news",
        f"Summarize what has actually been reported about {symbol}: the concrete "
        "catalyst(s), how fresh they are, whether sources corroborate, and what is "
        "claimed vs merely speculated.",
        f"Recent articles for {symbol}:\n" + wrap_data("articles", lines or "none"),
        decision_id,
    )


def _priced_in_digest(price_ctx: dict | None, options: dict | None) -> dict | None:
    """Explicit 'how much of the move is already done vs what the options market
    priced' — the entry-side counterpart to the exit give-back screen. Code owns
    the arithmetic; the note still judges. None when options data is unavailable
    (then the model falls back to the qualitative read). A realized move already
    well past the implied move = the drift is likely SPENT (the PEGA-at-entry case:
    it had already moved ~2.5x its implied move before we looked)."""
    if not (price_ctx and options):
        return None
    em = options.get("expected_move_to_expiry_pct")
    if not em:
        return None
    out: dict = {"implied_move_pct": em}
    t, f = price_ctx.get("change_today_pct"), price_ctx.get("change_5d_pct")
    if t is not None:
        out["moved_today_pct"] = t
        out["today_vs_implied"] = round(abs(t) / em, 2)
    if f is not None:
        out["moved_5d_pct"] = f
        out["5d_vs_implied"] = round(abs(f) / em, 2)
    return out


def market_brief(symbol: str, price_ctx: dict | None, fundamentals: dict | None,
                 articles: list[dict], decision_id: str | None = None,
                 options: dict | None = None) -> dict:
    """One call covering the three code-fact dimensions that used to be three
    briefs: technicals, valuation, and the priced-in / still-developing read."""
    payload = {
        "price": price_ctx or "none",
        "fundamentals": fundamentals or "none",
        "options": options or "none",
        "already_moved_vs_implied": _priced_in_digest(price_ctx, options) or "no options data",
        "catalyst_timestamps": [a.get("published_at", "")[:16] for a in articles[:6]],
    }
    return _brief(
        "market",
        f"Give the market backdrop for {symbol} in three tight parts, using ONLY "
        "the numbers provided (invent nothing). (1) TECHNICALS: trend, where price "
        "sits in its range, extended vs quiet, liquidity, and relative volume (rvol "
        "— latest session's volume vs its own norm; >1 confirms real participation "
        "behind the move, ~1 says the crowd hasn't engaged and repricing may be "
        "ahead). (2) VALUATION: cheap or "
        "rich, profitable/growing, whether the valuation leaves room for the "
        "catalyst or is priced for perfection. (3) PRICED-IN & LEGS: compare "
        "catalyst timing to the move — already moved hard (fade risk) vs barely "
        "moved (repricing may be ahead); and is this a spent POINT event or a "
        "STILL-DEVELOPING story with multi-day drift left. If options data is "
        "present, anchor the priced-in read on it: the options-implied "
        "expected_move_*_pct is the market's OWN estimate of the move over that "
        "window — a thesis whose move sits INSIDE the expected move for its horizon "
        "is largely priced in, while a move beyond it is either genuine underpricing "
        "or an overreach; elevated atm_iv_pct means a bigger move is already "
        "expected (and flags post-catalyst IV-crush risk). The already_moved_vs_implied "
        "block makes this explicit: *_vs_implied is how many times the realized move "
        "has already covered the market's implied move — ≳1.5 means the move is likely "
        "SPENT (the easy repricing is done → fade risk, size down or pass), while ≲0.5 "
        "means it has barely moved vs what's priced (repricing may be AHEAD → the drift "
        "the desk wants). State the spent/ahead read plainly.",
        "Data:\n" + wrap_data("market", json.dumps(payload, default=str)),
        decision_id,
    )


