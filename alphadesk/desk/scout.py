"""The Scout desk — ALL attention judgment lives here, in a prompt.

Sees every news-active symbol in the window (with price fields as evidence)
plus the movers FYI ranking. Picks ≤MAX_PICKS_PER_WINDOW with stated reasons;
every skip gets a reason too. No thresholds anywhere — words, not numbers.
"""

import json
import logging

from alphadesk.config import MAX_PICKS_PER_WINDOW
from alphadesk.llm import call_role, wrap_data

log = logging.getLogger("alphadesk.scout")

_SYSTEM = (
    "You are the scout desk of a predictive stock research firm. Your team "
    "predicts which stocks will OUTPERFORM over the next 1-10 trading days, "
    "BEFORE the market fully digests information. You allocate the team's "
    "scarce attention.\n\n"
    "You receive a window of news-active symbols (headlines + sentiment + price "
    "context) and an FYI list of today's top movers.\n\n"
    "Each symbol carries rvol — the latest session's volume divided by its own "
    "20-session norm. >1 means unusually active (real participation confirming a "
    "move); ~1 means the crowd hasn't engaged yet. It's evidence to weigh, not a "
    "rule: pair it with price — a big price move ON high rvol is a confirmed, "
    "acted-on catalyst, while a connected/spillover name still near 1× rvol is the "
    "unmoved neighbor the repricing may be ahead of.\n\n"
    "PICK up to {max_picks} symbols that most merit full team analysis. "
    "STRONGLY favor post-earnings-drift setups — candidates tagged [EARNINGS] just "
    "reported a result; stocks tend to drift in the surprise direction for days "
    "(the cleanest MOMENTUM edge), so weigh the surprise size and how much has already "
    "moved. Also favor: material company-specific catalysts; supplier/customer/"
    "competitor spillover where the affected NEIGHBOR hasn't moved yet; building "
    "multi-day themes; big catalysts whose initial move may CONTINUE for days. "
    "Disfavor: vague listicles, already-fully-priced stories, tiny illiquid names "
    "with promotional-sounding coverage (note the liquidity field), duplicate "
    "coverage of something already picked recently.\n"
    "edge_hint: SPILLOVER (spillover to a connected, unmoved name) | THEME "
    "(building theme) | MOMENTUM (big fresh catalyst, betting continuation) | "
    "WORLD (candidate sourced from the world-news desk — headlines "
    "tagged [WORLD:...]; the stated exposure is a HYPOTHESIS the team "
    "must verify, so weigh the plausibility of the causal chain).\n"
    "Give every pick AND every skip a one-sentence reason.\n\n"
    'Return ONLY JSON: {{"picks": [{{"symbol": "...", "edge_hint": '
    '"SPILLOVER|THEME|MOMENTUM|WORLD", "reason": "..."}}], '
    '"skips": [{{"symbol": "...", "reason": "..."}}]}}'
)

_SCHEMA = {
    "picks": {
        "type": list, "maxitems": MAX_PICKS_PER_WINDOW,
        "items": {
            "symbol": {"type": str, "symbol": True},
            "edge_hint": {"type": str, "enum": ["SPILLOVER", "THEME", "MOMENTUM", "WORLD"]},
            "reason": {"type": str, "maxlen": 300},
        },
    },
    "skips": {
        "type": list, "optional": True, "maxitems": 60,
        "items": {
            "symbol": {"type": str, "maxlen": 10},
            "reason": {"type": str, "maxlen": 200},
        },
    },
}


def run_scout(window: dict[str, dict], movers: list[dict]) -> dict:
    """window: symbol → {headlines: [...], avg_sentiment, price: {...}|None}."""
    if not window:
        return {"picks": [], "skips": []}

    from alphadesk.config import SCOUT_MAX_CANDIDATES
    if len(window) > SCOUT_MAX_CANDIDATES:
        log.info("scout window truncated: %d candidates → %d shown, %d dropped (window is "
                 "materiality-ranked, so the drops are the least-material)",
                 len(window), SCOUT_MAX_CANDIDATES, len(window) - SCOUT_MAX_CANDIDATES)
    lines = []
    for sym, info in list(window.items())[:SCOUT_MAX_CANDIDATES]:
        price = info.get("price") or {}
        lines.append(json.dumps({
            "symbol": sym,
            "headlines": info.get("headlines", [])[:4],
            "avg_sentiment": info.get("avg_sentiment"),
            "today_pct": price.get("change_today_pct"),
            "5d_pct": price.get("change_5d_pct"),
            "dollar_vol": price.get("avg_dollar_vol"),
            "rvol": price.get("rvol"),
            "low_liquidity": price.get("low_liquidity"),
        }))
    from alphadesk.config import market_context_line
    from alphadesk.desk.team import false_negative_block
    fn = false_negative_block()
    user = (
        market_context_line() + "\n\n"
        + (f"{fn}\n\n" if fn else "")
        + "Candidate window:\n" + wrap_data("candidates", "\n".join(lines))
        + "\n\nToday's top movers (FYI ranking — investigate only if you judge it worthwhile):\n"
        + wrap_data("movers", json.dumps(movers) if movers else "unavailable")
    )
    out = call_role(
        "scout", _SYSTEM.format(max_picks=MAX_PICKS_PER_WINDOW), user, schema=_SCHEMA
    )
    # Drop skips carrying a hallucinated / non-universe ticker: skips[].symbol isn't
    # whitelisted in _SCHEMA (only length-capped), and they flow to grade_skips →
    # false_negative_block, which feeds the "names we skipped that then moved" stat back
    # into scout+judge. An invented ticker would pollute that self-referential prior.
    if out.get("skips"):
        from alphadesk.config import in_universe
        out["skips"] = [s for s in out["skips"]
                        if isinstance(s.get("symbol"), str) and in_universe(s["symbol"])]
    return out
