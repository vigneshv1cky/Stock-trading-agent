"""Specialist brief subagents — ephemeral, parallel, haiku.

Three one-shot workers per candidate: technical (price structure), news
(what was actually said), graph (neighborhood + priced-check evidence).
Each returns a compact evidence block the committee argues from.
"""

import json
import logging

from alphadesk.llm import LLMError, call_role, wrap_data

log = logging.getLogger("alphadesk.briefs")

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


def technical_brief(symbol: str, price_ctx: dict | None, decision_id: str | None = None) -> dict:
    if not price_ctx:
        return {"kind": "technical", "summary": "(no price data)", "key_facts": []}
    return _brief(
        "technical",
        "Describe the price structure: trend, where price sits vs its range, "
        "whether recent action looks extended or quiet, and liquidity.",
        "Price data:\n" + wrap_data("prices", json.dumps(price_ctx)),
        decision_id,
    )


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


def fundamentals_brief(symbol: str, fundamentals: dict | None,
                       decision_id: str | None = None) -> dict:
    if not fundamentals:
        return {"kind": "fundamentals", "summary": "(no fundamentals data)", "key_facts": []}
    return _brief(
        "fundamentals",
        f"Give the fundamental backdrop for {symbol}: is it richly or cheaply "
        "valued, is it profitable and growing, and does the valuation leave room "
        "for the catalyst to move it — or is it already priced for perfection? "
        "Be factual about the numbers given; do not invent any.",
        "Fundamentals:\n" + wrap_data("fundamentals", json.dumps(fundamentals, default=str)),
        decision_id,
    )


def freshness_brief(symbol: str, price_ctx: dict | None, articles: list[dict],
                    decision_id: str | None = None) -> dict:
    """The 'already-priced?' check — the crux of every drift/ripple thesis."""
    ages = [a.get("published_at", "")[:16] for a in articles[:6]]
    payload = {
        "catalyst_timestamps": ages,
        "move_today_pct": (price_ctx or {}).get("change_today_pct"),
        "move_5d_pct": (price_ctx or {}).get("change_5d_pct"),
        "move_20d_pct": (price_ctx or {}).get("change_20d_pct"),
        "vs_90d_high": (price_ctx or {}).get("high_90d"),
        "vs_90d_low": (price_ctx or {}).get("low_90d"),
        "last_price": (price_ctx or {}).get("last_price"),
    }
    return _brief(
        "freshness",
        f"Judge two things about {symbol}'s catalyst. (1) PRICED-IN: compare the "
        "catalyst timing to the price move — if the stock already moved hard in the "
        "catalyst's direction the edge may be gone (fade risk); if it barely moved, "
        "the repricing may still be ahead. (2) LEGS: is this a POINT event that's "
        "essentially over, or a STILL-DEVELOPING story likely to keep generating "
        "moves over the coming days/weeks (earnings→estimate revisions, policy→"
        "phased rollout, M&A→regulatory steps)? A developing story can carry a "
        "multi-day drift even if the first move is priced. State plainly whether "
        "there's room left to run and whether the story still has legs.",
        "Timing & move data:\n" + wrap_data("freshness", json.dumps(payload, default=str)),
        decision_id,
    )


def graph_brief(symbol: str, neighborhood: dict, neighbor_moves: dict[str, float],
                decision_id: str | None = None) -> dict:
    payload = {
        "typed_relations": neighborhood.get("typed_relations", []),
        "co_mentioned_30d": neighborhood.get("co_mentioned", []),
        "recent_articles": neighborhood.get("recent_articles", [])[:6],
        "neighbor_5d_moves_pct": neighbor_moves,
    }
    return _brief(
        "graph",
        f"Describe {symbol}'s relationship neighborhood: which connected companies "
        "had significant news, the evidence for each connection, and — critically — "
        "whether connected-company moves suggest any spillover is ALREADY PRICED "
        "(compare event direction vs the neighbor 5-day moves provided).",
        "Neighborhood data:\n" + wrap_data("graph", json.dumps(payload, default=str)),
        decision_id,
    )
