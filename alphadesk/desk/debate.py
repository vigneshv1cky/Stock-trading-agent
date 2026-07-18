"""The shared committee debate — the ONE place the analyst⇄skeptic→arbiter
sequence and the ledger write live, so the streaming (Find Trades) and batch
(research_run) pipelines can never drift on the core logic.

`deliberate()` is an async generator: it yields the debate's intermediate events
(thesis, concern, fact_flag, rebuttal) for live streaming, writes the committee
ledger row, and yields a terminal private {"type": "_result", ...} event carrying
the row + pick_id + raw thesis/verdict the callers need. It raises LLMError if a
stage fails (the caller drops the candidate).

Deliberately NOT here (they legitimately differ per entry point): brief-gathering
(stream uses fundamentals/freshness, workflow uses the graph brief), the solo arm
cadence, and the caller-specific pre/post steps (exposure, chief, cooldowns,
funnel). Those stay in stream.py / workflow.py.
"""

import asyncio
import logging

from alphadesk.config import MODEL_MAP, session
from alphadesk.desk import team
from alphadesk.ingest import prices
from alphadesk.ledger import store

log = logging.getLogger("alphadesk.debate")


async def deliberate(sym: str, pick: dict, briefs: list[dict], price_ctx: dict | None,
                     history: list[dict], calibration: str, trigger_src: str,
                     decision_id: str):
    """Run the full committee debate on one pick and write its ledger row.

    Yields event dicts (thesis, concern, fact_flag, rebuttal) for streaming, then
    a terminal {"type": "_result", "row": <board row>, "pick_id": int,
    "thesis": <raw>, "verdict": <raw>}. Raises LLMError on any stage failure.
    """
    loop = asyncio.get_running_loop()

    thesis = await loop.run_in_executor(
        None, lambda: team.researcher_case(
            sym, pick["reason"], briefs, history, decision_id, calibration))
    model_tags = {"researcher": thesis.pop("_downgraded_model", MODEL_MAP["researcher"])}
    yield {"type": "thesis", "symbol": sym, **thesis}

    concerns_out = await loop.run_in_executor(
        None, lambda: team.critic_challenge(sym, thesis, briefs, decision_id))
    model_tags["critic"] = concerns_out.pop("_downgraded_model", MODEL_MAP["critic"])
    concerns = concerns_out.get("concerns", [])
    for c in concerns:
        yield {"type": "concern", "symbol": sym, **c}

    flags = team.fact_check_concerns(concerns, price_ctx)
    for f in flags:
        yield {"type": "fact_flag", "symbol": sym, "text": f}

    rebuttal = await loop.run_in_executor(
        None, lambda: team.researcher_reply(sym, thesis, concerns, decision_id))
    rebuttal.pop("_downgraded_model", None)
    yield {"type": "rebuttal", "symbol": sym, **rebuttal}

    verdict = await loop.run_in_executor(
        None, lambda: team.judge_verdict(sym, thesis, concerns, rebuttal, flags, decision_id))
    model_tags["judge"] = verdict.pop("_downgraded_model", MODEL_MAP["judge"])

    sess = session()
    horizon = int(verdict.get("adjusted_horizon_days") or thesis["horizon_days"])
    pick_id = store.record_pick({
        "symbol": sym, "arm": "TEAM", "edge": pick.get("edge_hint"),
        "trigger_src": trigger_src, "session": sess,
        "direction": thesis["direction"], "horizon_days": horizon,
        "score": thesis["score"], "adjusted_score": verdict["adjusted_score"],
        "confidence": verdict["adjusted_confidence"], "verdict": verdict["verdict"],
        "approved": int(bool(verdict["approved"])),
        "triage_reason": pick["reason"], "thesis": thesis["thesis"],
        "debate": {"concerns": concerns, "rebuttal": rebuttal,
                   "fact_flags": flags, "arbiter_summary": verdict["summary"]},
        "briefs": briefs, "model_tags": model_tags,
        "low_liquidity": int(bool(price_ctx and price_ctx.get("low_liquidity"))),
        "skeptic_moved_score": round(float(rebuttal["revised_score"]) - float(thesis["score"]), 2),
        "arbiter_overrode": int(bool(verdict["approved"]) != (float(rebuttal["revised_score"]) > 50)),
        "entry_price": (price_ctx or {}).get("last_price") if sess == "OPEN" else None,
        "spy_price": (prices.get_context("SPY") or {}).get("last_price"),
    })
    row = {
        "id": pick_id, "symbol": sym, "direction": thesis["direction"],
        "horizon_days": horizon, "edge": pick.get("edge_hint"),
        "conviction": verdict["adjusted_score"], "confidence": verdict["adjusted_confidence"],
        "verdict": verdict["verdict"], "approved": bool(verdict["approved"]),
        "summary": verdict["summary"],
    }
    yield {"type": "_result", "row": row, "pick_id": pick_id,
           "thesis": thesis, "verdict": verdict}
