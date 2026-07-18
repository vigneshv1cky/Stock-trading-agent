"""The Triage desk — ALL attention judgment lives here, in a prompt.

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
    "You are the triage desk of a predictive stock research firm. Your team "
    "predicts which stocks will OUTPERFORM over the next 1-10 trading days, "
    "BEFORE the market fully digests information. You allocate the committee's "
    "scarce attention.\n\n"
    "You receive a window of news-active symbols (headlines + sentiment + price "
    "context) and an FYI list of today's top movers.\n\n"
    "PICK up to {max_picks} symbols that most merit full committee analysis. "
    "STRONGLY favor post-earnings-drift setups — candidates tagged [EARNINGS] just "
    "reported a result; stocks tend to drift in the surprise direction for days "
    "(the cleanest DRIFT edge), so weigh the surprise size and how much has already "
    "moved. Also favor: material company-specific catalysts; supplier/customer/"
    "competitor spillover where the affected NEIGHBOR hasn't moved yet; building "
    "multi-day themes; big catalysts whose initial move may CONTINUE for days. "
    "Disfavor: vague listicles, already-fully-priced stories, tiny illiquid names "
    "with promotional-sounding coverage (note the liquidity field), duplicate "
    "coverage of something already picked recently.\n"
    "edge_hint: RIPPLE (spillover to a connected, unmoved name) | NARRATIVE "
    "(building theme) | DRIFT (big fresh catalyst, betting continuation) | "
    "WORLD_EVENT (candidate sourced from the world-news desk — headlines "
    "tagged [WORLD:...]; the stated exposure is a HYPOTHESIS the committee "
    "must verify, so weigh the plausibility of the causal chain).\n"
    "Give every pick AND every skip a one-sentence reason.\n\n"
    'Return ONLY JSON: {{"picks": [{{"symbol": "...", "edge_hint": '
    '"RIPPLE|NARRATIVE|DRIFT", "reason": "..."}}], '
    '"skips": [{{"symbol": "...", "reason": "..."}}]}}'
)

_SCHEMA = {
    "picks": {
        "type": list, "maxitems": MAX_PICKS_PER_WINDOW,
        "items": {
            "symbol": {"type": str, "symbol": True},
            "edge_hint": {"type": str, "enum": ["RIPPLE", "NARRATIVE", "DRIFT", "WORLD_EVENT"]},
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


def run_triage(window: dict[str, dict], movers: list[dict]) -> dict:
    """window: symbol → {headlines: [...], avg_sentiment, price: {...}|None}."""
    if not window:
        return {"picks": [], "skips": []}

    lines = []
    for sym, info in list(window.items())[:40]:
        price = info.get("price") or {}
        lines.append(json.dumps({
            "symbol": sym,
            "headlines": info.get("headlines", [])[:4],
            "avg_sentiment": info.get("avg_sentiment"),
            "today_pct": price.get("change_today_pct"),
            "5d_pct": price.get("change_5d_pct"),
            "dollar_vol": price.get("avg_dollar_vol"),
            "low_liquidity": price.get("low_liquidity"),
        }))
    from alphadesk.desk.team import false_negative_block
    fn = false_negative_block()
    user = (
        (f"{fn}\n\n" if fn else "")
        + "Candidate window:\n" + wrap_data("candidates", "\n".join(lines))
        + "\n\nToday's top movers (FYI ranking — investigate only if you judge it worthwhile):\n"
        + wrap_data("movers", json.dumps(movers) if movers else "unavailable")
    )
    return call_role(
        "scout", _SYSTEM.format(max_picks=MAX_PICKS_PER_WINDOW), user, schema=_SCHEMA
    )
