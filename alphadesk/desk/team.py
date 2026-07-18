"""The committee — Analyst (sonnet) ⇄ Skeptic (opus) → Arbiter (opus).

Sequential deliberation over the specialist briefs. Every numeric price claim
the Skeptic makes is fact-checkable against the price context (a claim the
data can't support gets flagged in the transcript before the Arbiter rules).
"""

import json
import logging
import re

from alphadesk.ledger import store
from alphadesk.llm import call_role, wrap_data

FN_MIN_SAMPLES = 8   # graded no's needed before the false-negative record is shown


def false_negative_block(min_samples: int = FN_MIN_SAMPLES) -> str:
    """The desk's false-negative record — how often saying NO was wrong. Facts to
    weigh ("is the desk too cautious?"), not obey; sample-gated, empty until enough
    graded no's to be signal. EXPERIMENT: whether feeding this back actually changes
    behavior is itself unproven — the decisions it touches should be measured, not
    assumed to improve. It builds the evidence regardless (see store.grade_skips)."""
    stats = store.false_negative_stats()
    rej, skp = stats.get("reject") or {}, stats.get("skip") or {}
    lines = []
    rg, rm = rej.get("graded") or 0, rej.get("missed") or 0
    if rg >= min_samples:
        lines.append(f"- rejections: {rm}/{rg} ({round(100 * rm / rg)}%) of your REJECTED "
                     "calls would have beaten SPY — winners you passed on")
    sg, sm = skp.get("graded") or 0, skp.get("missed") or 0
    if sg >= min_samples:
        lines.append(f"- skips: {sm}/{sg} ({round(100 * sm / sg)}%) of names you SKIPPED then "
                     "made a big move vs SPY you never looked at")
    if not lines:
        return ""
    return ("The desk's false-negative record so far (weigh it — is the desk too cautious "
            "in saying no?):\n" + "\n".join(lines))

log = logging.getLogger("alphadesk.team")

_PREDICTIVE_FRAME = (
    "You work for a PREDICTIVE research desk: the question is always whether "
    "this stock will OUTPERFORM or UNDERPERFORM the market over the next "
    "1-10 TRADING DAYS from now — not whether past news was good. If a move "
    "already happened, the only question is what happens NEXT (continuation, "
    "fade, or spillover to connected names)."
)

_ANALYST_SYSTEM = (
    "You are the Analyst. " + _PREDICTIVE_FRAME + "\n"
    "From the specialist briefs and your own track record on this symbol, form "
    "a directional thesis. Choose the horizon that matches the mechanism "
    "(drift: 1-3d; spillover repricing: 3-5d; theme: 5-10d). score: 0-100 where "
    ">50 favors LONG conviction, <50 favors SHORT; be decisive. confidence: 0-100 "
    "how sure you are.\n"
    'Return ONLY JSON: {"direction": "LONG|SHORT", "horizon_days": <1-10>, '
    '"score": <0-100>, "confidence": <0-100>, "thesis": "<5 sentences max>"}'
)

_THESIS_SCHEMA = {
    "direction": {"type": str, "enum": ["LONG", "SHORT"]},
    "horizon_days": {"type": int, "min": 1, "max": 10},
    "score": {"type": (int, float), "min": 0, "max": 100},
    "confidence": {"type": (int, float), "min": 0, "max": 100},
    "thesis": {"type": str, "maxlen": 1500},
}

_SKEPTIC_SYSTEM = (
    "You are the Skeptic. " + _PREDICTIVE_FRAME + "\n"
    "Your job is to find the strongest reasons this thesis FAILS. Attack the "
    "mechanism, the timing, the already-priced risk, crowding, upcoming events "
    "inside the horizon, data quality, and liquidity. Every concern must cite "
    "specific evidence from the briefs — no generic worries. Exactly 3 concerns, "
    "strongest first.\n"
    'Return ONLY JSON: {"concerns": [{"claim": "...", "evidence": "..."}]}'
)

_SKEPTIC_SCHEMA = {
    "concerns": {
        "type": list, "maxitems": 3,
        "items": {
            "claim": {"type": str, "maxlen": 300},
            "evidence": {"type": str, "maxlen": 300},
        },
    },
}

_REBUTTAL_SYSTEM = (
    "You are the Analyst defending your thesis against the Skeptic. "
    + _PREDICTIVE_FRAME + "\n"
    "Address each concern honestly: rebut with evidence where you can, CONCEDE "
    "where the skeptic is right. Update your score accordingly — meaningful "
    "concessions must move the number.\n"
    'Return ONLY JSON: {"rebuttal": "<4 sentences max>", '
    '"revised_score": <0-100>, "concede": <true|false>}'
)

_REBUTTAL_SCHEMA = {
    "rebuttal": {"type": str, "maxlen": 1000},
    "revised_score": {"type": (int, float), "min": 0, "max": 100},
    "concede": {"type": bool},
}

_ARBITER_SYSTEM = (
    "You are the Arbiter — the final judgment on the desk. " + _PREDICTIVE_FRAME + "\n"
    "Read the full transcript (thesis, concerns, rebuttal, any fact-check flags). "
    "Decide whether this prediction goes on the book. Weigh argument QUALITY: "
    "did the skeptic land real hits? did the analyst answer them or dodge? "
    "verdict: CONFIRM (thesis stands), WEAKEN (stands but softer), REJECT.\n"
    "COHERENCE RULE: adjusted_score must agree with the direction — for a LONG "
    "it must be ABOVE 50, for a SHORT BELOW 50. If your honest view puts the "
    "score on the wrong side of 50, the trade does not belong on the book: set "
    "approved=false and REJECT. Weak-but-real conviction in a LONG is 52-58, "
    "not 43.\n"
    "adjusted_horizon_days: you own the horizon too — if the surviving edge is "
    "shorter or longer than the analyst's proposal (e.g. 'only a 1-2 day "
    "momentum edge survives'), SAY SO in this field; the book records YOUR "
    "horizon.\n"
    'Return ONLY JSON: {"approved": <true|false>, "adjusted_score": <0-100>, '
    '"adjusted_confidence": <0-100>, "adjusted_horizon_days": <1-10>, '
    '"verdict": "CONFIRM|WEAKEN|REJECT", "summary": "<3 sentences max>"}'
)

_ARBITER_SCHEMA = {
    "approved": {"type": bool},
    "adjusted_score": {"type": (int, float), "min": 0, "max": 100},
    "adjusted_confidence": {"type": (int, float), "min": 0, "max": 100},
    "adjusted_horizon_days": {"type": int, "min": 1, "max": 10, "optional": True},
    "verdict": {"type": str, "enum": ["CONFIRM", "WEAKEN", "REJECT"]},
    "summary": {"type": str, "maxlen": 800},
}


def _briefs_block(briefs: list[dict]) -> str:
    return wrap_data("briefs", json.dumps(briefs, default=str))


def _memory_block(history: list[dict]) -> str:
    if not history:
        return "No prior track record on this symbol."
    lines = [
        f"- {h['ts'][:10]}: {h['direction']} {h['horizon_days']}d conf={h['confidence']:.0f}"
        f" → alpha_net={h['alpha_net']}%"
        for h in history
    ]
    return "Your desk's past graded calls on this symbol:\n" + "\n".join(lines)


# Minimum graded picks in a bucket before its hit-rate is shown as a prior —
# below this it's noise, not signal, so we withhold it rather than mislead.
CALIB_MIN_SAMPLES = 8


def calibration_block(stats: dict, min_samples: int = CALIB_MIN_SAMPLES) -> str:
    """Grounded numeric priors: the desk's own GRADED hit-rate and net alpha by
    edge, horizon, and confidence bucket. Facts about our track record, not
    verbal 'lessons' — buckets under min_samples are withheld (too few to trust,
    and superstition is the failure mode we're avoiding). Fixed-size and
    falsifiable: it's a scorecard, so no unbounded growth and no injection
    surface. Weigh it, don't obey it."""
    total = stats.get("total") or {}
    graded = total.get("graded") or 0
    if graded < min_samples:
        return (f"Desk calibration: only {graded} graded calls so far — too few "
                "for reliable priors. Judge on the briefs alone.")
    by = stats.get("by") or {}
    lines: list[str] = []
    for dim in ("edge", "horizon", "confidence"):
        rows = [r for r in by.get(dim, []) if (r.get("graded") or 0) >= min_samples]
        parts = []
        for r in rows:
            n = r["graded"]
            hit = round(100 * (r.get("wins") or 0) / n)
            parts.append(f"{r['bucket']}: {hit}% hit, {r.get('avg_alpha_net')}% net α (n={n})")
        if parts:
            lines.append(f"  by {dim} — " + " · ".join(parts))
    if not lines:
        return (f"Desk calibration: {graded} graded overall, but no single bucket "
                f"has ≥{min_samples} yet — priors not reliable. Judge on the briefs.")
    return (
        "Your desk's GRADED calibration to date (net of friction, vs SPY) — "
        "facts about your own track record, weigh them but don't obey them:\n"
        + "\n".join(lines)
    )


def analyst_thesis(symbol: str, triage_reason: str, briefs: list[dict],
                   history: list[dict], decision_id: str | None,
                   calibration: str = "") -> dict:
    calib = f"{calibration}\n\n" if calibration else ""
    user = (
        f"Symbol: {symbol}\nTriage rationale: {triage_reason}\n\n"
        f"{calib}{_memory_block(history)}\n\nSpecialist briefs:\n{_briefs_block(briefs)}"
    )
    return call_role("researcher", _ANALYST_SYSTEM, user, schema=_THESIS_SCHEMA,
                     decision_id=decision_id)


def skeptic_challenge(symbol: str, thesis: dict, briefs: list[dict],
                      decision_id: str | None) -> dict:
    user = (
        f"Symbol: {symbol}\nAnalyst thesis: {json.dumps(thesis)}\n\n"
        f"Specialist briefs:\n{_briefs_block(briefs)}"
    )
    return call_role("critic", _SKEPTIC_SYSTEM, user, schema=_SKEPTIC_SCHEMA,
                     decision_id=decision_id)


def analyst_rebuttal(symbol: str, thesis: dict, concerns: list[dict],
                     decision_id: str | None) -> dict:
    user = (
        f"Symbol: {symbol}\nYour thesis: {json.dumps(thesis)}\n"
        f"Skeptic's concerns: {json.dumps(concerns)}"
    )
    return call_role("researcher", _REBUTTAL_SYSTEM, user, schema=_REBUTTAL_SCHEMA,
                     decision_id=decision_id)


_CHIEF_SYSTEM = (
    "You are the Chief Strategist of a trading research desk. Your analysts have "
    "each INDEPENDENTLY debated one opportunity and produced a call. Do NOT "
    "re-analyze them — COMPARE them head-to-head and decide which are genuinely "
    "the best to commit capital to right now.\n"
    "Rules for comparison:\n"
    "  • Conviction scores came from separate debates and are NOT directly "
    "comparable — re-judge on one common standard.\n"
    "  • Evidence QUALITY beats the raw number: a hard catalyst (confirmed "
    "filing, earnings, signed policy) outranks a single-source rumor even at a "
    "similar score.\n"
    "  • REDUNDANCY/CORRELATION: if several ideas are effectively the same bet "
    "(same sector, same driver, same direction), do NOT stack them — keep only "
    "the best expression.\n"
    "  • A weak slate means take fewer or none; a strong slate can support more.\n"
    "Rank ALL ideas best-to-worst. Mark take=true ONLY for the ones you'd "
    "actually put on. Give each a one-line COMPARATIVE reason (why it ranks "
    "here versus the others).\n"
    'Return ONLY JSON: {"ranked": [{"symbol": "<TICKER>", "take": true|false, '
    '"reason": "..."}], "summary": "<your read of the whole slate, 2-3 sentences>"}'
)

_CHIEF_SCHEMA = {
    "ranked": {
        "type": list, "maxitems": 12,
        "items": {
            "symbol": {"type": str, "maxlen": 10},
            "take": {"type": bool},
            "reason": {"type": str, "maxlen": 300},
        },
    },
    "summary": {"type": str, "maxlen": 800},
}


def chief_synthesis(opportunities: list[dict], decision_id: str | None) -> dict:
    """Head-to-head comparison across all debated ideas → ranked selection.

    `opportunities`: board rows with symbol, direction, horizon_days, edge,
    conviction, confidence, verdict, summary (the arbiter's take).
    """
    lines = []
    for o in opportunities:
        lines.append(json.dumps({
            "symbol": o["symbol"], "direction": o["direction"],
            "horizon_days": o["horizon_days"], "edge": o.get("edge"),
            "conviction": o.get("conviction"), "confidence": o.get("confidence"),
            "verdict": o.get("verdict"), "committee_take": o.get("approved"),
            "note": (o.get("summary") or "")[:300],
        }))
    user = "Debated opportunities to compare:\n" + wrap_data("ideas", "\n".join(lines))
    return call_role("head", _CHIEF_SYSTEM, user, schema=_CHIEF_SCHEMA,
                     decision_id=decision_id)


def arbiter_verdict(symbol: str, thesis: dict, concerns: list[dict], rebuttal: dict,
                    fact_flags: list[str], decision_id: str | None) -> dict:
    fn = false_negative_block()
    user = (
        (f"{fn}\n\n" if fn else "")
        + f"Symbol: {symbol}\n"
        f"THESIS: {json.dumps(thesis)}\n"
        f"SKEPTIC CONCERNS: {json.dumps(concerns)}\n"
        f"ANALYST REBUTTAL: {json.dumps(rebuttal)}\n"
        f"FACT-CHECK FLAGS: {json.dumps(fact_flags) if fact_flags else 'none'}"
    )
    return call_role("judge", _ARBITER_SYSTEM, user, schema=_ARBITER_SCHEMA,
                     decision_id=decision_id)


# ---------------------------------------------------------------------------
# Fact-check helper — numeric % claims vs actual price context (pure code)
# ---------------------------------------------------------------------------

# Only flag %s the skeptic explicitly frames as a PRICE MOVE — a move-verb must
# sit next to the number. Avoids false positives on valuation/guidance/support %s
# (e.g. "revenue +1%", "1.1% above the 90-day low") that aren't price-move claims.
_MOVE_PCT_RE = re.compile(
    r"(?:ran|rose|fell|dropped|gained|lost|surged|plunged|jumped|rallied|slid|"
    r"soared|sank|climbed|declined|crashed|tumbled|spiked|up|down)\s+(?:by\s+)?"
    r"([+-]?\d{1,3}(?:\.\d+)?)\s?%"
    r"|([+-]?\d{1,3}(?:\.\d+)?)\s?%\s+(?:move|rally|selloff|sell-off|drop|gain|"
    r"decline|surge|plunge|pop|run|slide|jump)",
    re.IGNORECASE,
)


def fact_check_concerns(concerns: list[dict], price_ctx: dict | None) -> list[str]:
    """Flag skeptic PRICE-MOVE claims wildly inconsistent with real price data."""
    if not price_ctx:
        return []
    known = {
        abs(price_ctx.get("change_today_pct") or 0.0),
        abs(price_ctx.get("change_5d_pct") or 0.0),
        abs(price_ctx.get("change_20d_pct") or 0.0),
    }
    flags = []
    for c in concerns:
        text = f"{c.get('claim','')} {c.get('evidence','')}"
        for m in _MOVE_PCT_RE.finditer(text):
            raw = m.group(1) or m.group(2)
            val = abs(float(raw))
            if val > 0.5 and known and min(abs(val - k) for k in known) > max(5.0, val):
                flags.append(
                    f"Skeptic cited a {raw}% price move — no matching move in data "
                    f"(today={price_ctx.get('change_today_pct')}%, "
                    f"5d={price_ctx.get('change_5d_pct')}%, "
                    f"20d={price_ctx.get('change_20d_pct')}%)"
                )
    return flags
