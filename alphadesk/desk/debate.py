"""The shared team debate — the ONE place the researcher⇄critic→judge
sequence and the ledger write live, so the streaming (Find Trades) and batch
(research_run) pipelines can never drift on the core logic.

`deliberate()` is an async generator: it yields the debate's intermediate events
(thesis, concern, fact_flag, rebuttal) for live streaming, writes the team
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

from alphadesk.config import MODEL_MAP, pinned_horizon, session
from alphadesk.desk import plan, team
from alphadesk.ingest import prices
from alphadesk.ledger import store

log = logging.getLogger("alphadesk.debate")


async def deliberate(sym: str, pick: dict, briefs: list[dict], price_ctx: dict | None,
                     history: list[dict], calibration: str, trigger_src: str,
                     decision_id: str):
    """Run the full team debate on one pick and write its ledger row.

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
    # The critic may now REVERSE the call (FLIP → opposite side) or STAND_ASIDE.
    counter = {
        "stance": concerns_out.get("stance", "SUPPORT"),
        "counter_direction": concerns_out.get("counter_direction", "NONE"),
        "counter": concerns_out.get("counter", ""),
    }
    if counter["stance"] != "SUPPORT":
        yield {"type": "counter", "symbol": sym, **counter,
               "proposed_from": thesis["direction"]}

    flags = team.fact_check_concerns(concerns, price_ctx)
    for f in flags:
        yield {"type": "fact_flag", "symbol": sym, "text": f}

    rebuttal = await loop.run_in_executor(
        None, lambda: team.researcher_reply(sym, thesis, concerns, counter, decision_id))
    # The researcher speaks twice (opening thesis + this rebuttal); model_tags["researcher"]
    # was set from the thesis only and the rebuttal's downgrade was discarded, so a debate
    # whose rebuttal ran on a downgraded tier was ledgered as full-tier — blinding the
    # kill-criterion "were weak-model calls worse?" analysis. Reflect a downgraded rebuttal.
    rb_model = rebuttal.pop("_downgraded_model", None)
    if rb_model and rb_model != MODEL_MAP["researcher"]:
        model_tags["researcher"] = rb_model
    yield {"type": "rebuttal", "symbol": sym, **rebuttal}

    verdict = await loop.run_in_executor(
        None, lambda: team.judge_verdict(sym, thesis, concerns, counter, rebuttal, flags, decision_id))
    model_tags["judge"] = verdict.pop("_downgraded_model", MODEL_MAP["judge"])

    # The judge always commits to a direction (LONG/SHORT) — it may adopt the
    # critic's flip. There is no stand-aside: every debated name is a graded
    # directional call; `approved` marks conviction (size up) vs a thin lean.
    final_dir = verdict.get("final_direction") or thesis["direction"]
    booked_dir = final_dir if final_dir in ("LONG", "SHORT") else thesis["direction"]
    flipped = booked_dir != thesis["direction"]

    sess = session()
    # Horizon is PRE-COMMITTED per edge — the grade settles at a horizon fixed in advance,
    # NOT one the judge picked after seeing the setup (that was a garden-of-forking-paths: the
    # same catalyst bookable as a 1d or 10d call, only the chosen spec logged). The plan (and
    # thus the trade) sizes to this pinned horizon too, so entry and grade stay consistent.
    horizon = pinned_horizon(pick.get("edge_hint"))

    # Execution desk: turn the committed call into an actionable trade plan
    # (entry/target/stop/note). Fail-open — a missing plan never blocks the pick.
    trade = await loop.run_in_executor(
        None, lambda: plan.trade_plan(sym, booked_dir, horizon, price_ctx,
                                      thesis["thesis"], decision_id))
    if trade:
        yield {"type": "plan", "symbol": sym, "direction": booked_dir, **trade}

    pick_id = store.record_pick({
        "symbol": sym, "arm": "TEAM", "edge": pick.get("edge_hint"),
        "source": pick.get("source"), "decision_id": decision_id,
        "trigger_src": trigger_src, "session": sess,
        "direction": booked_dir, "horizon_days": horizon,
        "score": thesis["score"], "adjusted_score": verdict["adjusted_score"],
        "confidence": verdict["adjusted_confidence"], "verdict": verdict["verdict"],
        "approved": int(bool(verdict["approved"])),
        "triage_reason": pick["reason"], "thesis": thesis["thesis"],
        "debate": {"concerns": concerns, "rebuttal": rebuttal,
                   "fact_flags": flags, "arbiter_summary": verdict["summary"],
                   "critic_stance": counter["stance"],
                   "counter_direction": counter["counter_direction"],
                   "counter": counter["counter"],
                   "proposed_direction": thesis["direction"],
                   "final_direction": final_dir, "flipped": flipped},
        "briefs": briefs, "model_tags": model_tags,
        "low_liquidity": int(bool(price_ctx and price_ctx.get("low_liquidity"))),
        "skeptic_moved_score": round(float(rebuttal["revised_score"]) - float(thesis["score"]), 2),
        # Overrode = judge APPROVED a pick whose committed direction opposes the
        # researcher's post-rebuttal lean (revised_score >50 favors LONG). The old XOR
        # compared approval against that lean directly, so every approved SHORT
        # (approved=True, revised_score<50) was logged as an override that never happened.
        "arbiter_overrode": int(bool(verdict["approved"]) and final_dir != (
            "LONG" if float(rebuttal["revised_score"]) > 50 else "SHORT")),
        "entry_price": (price_ctx or {}).get("last_price") if sess == "OPEN" else None,
        "spy_price": (prices.get_context("SPY") or {}).get("last_price"),
        "plan_entry": (trade or {}).get("entry"),
        "plan_target": (trade or {}).get("target"),
        "plan_stop": (trade or {}).get("stop"),
        "plan_note": (trade or {}).get("note"),
        "order_type": (trade or {}).get("order"),
    })
    row = {
        "id": pick_id, "symbol": sym, "direction": booked_dir,
        "horizon_days": horizon, "edge": pick.get("edge_hint"),
        "conviction": verdict["adjusted_score"], "confidence": verdict["adjusted_confidence"],
        "verdict": verdict["verdict"], "approved": bool(verdict["approved"]),
        "flipped": flipped, "summary": verdict["summary"], "plan": trade,
    }
    yield {"type": "_result", "row": row, "pick_id": pick_id,
           "thesis": thesis, "verdict": verdict}
