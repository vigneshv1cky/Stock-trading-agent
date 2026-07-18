"""The Solo control arm — one strong single agent (opus), no team.

Runs on every ~Nth pick with the SAME briefs and memory the committee gets,
producing the same output schema. Its graded track record vs the committee's
answers the field's central question on our own data: does multi-agent
deliberation beat one good agent, net of tokens? (Kill criterion #2.)
"""

import json
import logging

from alphadesk.llm import call_role, wrap_data

log = logging.getLogger("alphadesk.loner")

_SYSTEM = (
    "You are a senior stock analyst working ALONE on a predictive research "
    "desk. The question: will this stock OUTPERFORM or UNDERPERFORM the market "
    "over the next 1-10 TRADING DAYS from now? If a move already happened, the "
    "only question is what happens next.\n"
    "Reason carefully from the briefs: form a thesis, then genuinely stress-test "
    "it yourself (what would a skeptic say? is the story already priced?), then "
    "commit. Choose the horizon that matches the mechanism. score: 0-100 (>50 "
    "LONG conviction, <50 SHORT); confidence: 0-100. approved: YOUR final call — "
    "would you put this prediction on the book, true or false?\n"
    'Return ONLY JSON: {"direction": "LONG|SHORT", "horizon_days": <1-10>, '
    '"score": <0-100>, "confidence": <0-100>, "approved": <true|false>, '
    '"thesis": "<6 sentences max>"}'
)

_SCHEMA = {
    "direction": {"type": str, "enum": ["LONG", "SHORT"]},
    "horizon_days": {"type": int, "min": 1, "max": 10},
    "score": {"type": (int, float), "min": 0, "max": 100},
    "confidence": {"type": (int, float), "min": 0, "max": 100},
    "approved": {"type": bool},
    "thesis": {"type": str, "maxlen": 1800},
}


def solo_analysis(symbol: str, triage_reason: str, briefs: list[dict],
                  history: list[dict], decision_id: str | None,
                  calibration: str = "") -> dict:
    memory = (
        "\n".join(
            f"- {h['ts'][:10]}: {h['direction']} {h['horizon_days']}d → alpha_net={h['alpha_net']}%"
            for h in history
        ) or "none"
    )
    calib = f"{calibration}\n\n" if calibration else ""
    user = (
        f"Symbol: {symbol}\nWhy it surfaced: {triage_reason}\n"
        f"Past graded calls on this symbol: {memory}\n\n"
        f"{calib}Specialist briefs:\n" + wrap_data("briefs", json.dumps(briefs, default=str))
    )
    return call_role("loner", _SYSTEM, user, schema=_SCHEMA, decision_id=decision_id)
