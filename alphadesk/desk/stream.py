"""On-demand 'Find Trades' — the v2 flow. Scans a broad news window, triages
opportunities, and debates each committee-style, STREAMING every step so the
dashboard can show the agents thinking in real time.

Emits SSE-style event dicts via an async generator:
    status        — human-readable progress line
    triage_pick   — a symbol triage chose, with reason + edge hint
    skips         — the symbols triage passed on (with reasons)
    debate_start  — beginning deliberation on one symbol
    brief         — a specialist subagent's output
    thesis        — the analyst's opening call
    concern       — one skeptic attack (streamed individually)
    fact_flag     — a code-side fact-check flag
    rebuttal      — the analyst's defense/concession
    decision      — the arbiter's verdict + the final booked pick
    done          — the ranked board of all opportunities found

Reuses the exact committee the autonomous engine used; only the orchestration
(sequential + streamed, broad news window) is new. No graph, no daemon.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from alphadesk.config import MODEL_MAP, session
from alphadesk.desk import briefs as briefs_mod
from alphadesk.desk import committee, triage
from alphadesk.ingest import news, prices
from alphadesk.ledger import store
from alphadesk.llm import LLMError

log = logging.getLogger("alphadesk.stream")


def _ev(_type: str, **data):
    return {"type": _type, **data}


def _headlines(articles: list[dict]) -> list[str]:
    return [
        f"[{a.get('category', '?')}] {a.get('title', '')[:120]}"
        + (f" (sent={a['mentions'][0]['sentiment']})" if a.get("mentions") else "")
        for a in articles[:4]
    ]


def _avg_sentiment(articles: list[dict]) -> float:
    vals = [m["sentiment"] for a in articles for m in a.get("mentions", [])]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


async def stream_find_trades(hours: float = 48.0, max_debates: int = 6):
    """Async generator of deliberation events. Broad news window (default 48h —
    a batch run can afford to look far wider than the old live-tick engine)."""
    loop = asyncio.get_running_loop()

    yield _ev("status", msg=f"Scanning the last {int(hours)}h of world + financial news…")
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    from datetime import datetime as _dt
    since_dt = _dt.fromtimestamp(since, tz=timezone.utc)
    try:
        n, candidates = await loop.run_in_executor(None, news.poll, since_dt)
    except Exception as exc:
        yield _ev("status", msg=f"News scan failed: {exc}")
        yield _ev("done", board=[])
        return

    if not candidates:
        yield _ev("status", msg="No fresh catalysts found in that window.")
        yield _ev("done", board=[])
        return

    yield _ev("status", msg=f"{n} articles → {len(candidates)} companies with catalysts. Triaging…")

    # Build the triage window (price context per symbol)
    window: dict[str, dict] = {}
    for sym, arts in list(candidates.items())[:60]:
        ctx = await loop.run_in_executor(None, prices.get_context, sym)
        window[sym] = {
            "headlines": _headlines(arts),
            "avg_sentiment": _avg_sentiment(arts),
            "price": ctx,
        }
    movers = await loop.run_in_executor(None, prices.movers)

    try:
        result = await loop.run_in_executor(None, triage.run_triage, window, movers)
    except LLMError as exc:
        yield _ev("status", msg=f"Triage failed: {exc}")
        yield _ev("done", board=[])
        return

    picks = (result.get("picks") or [])[:max_debates]
    skips = result.get("skips") or []
    yield _ev("skips", skips=skips)
    for p in picks:
        yield _ev("triage_pick", symbol=p["symbol"], edge=p.get("edge_hint"),
                  reason=p.get("reason", ""))

    if not picks:
        yield _ev("status", msg="Triage found no opportunities worth full analysis right now.")
        yield _ev("done", board=[])
        return

    yield _ev("status", msg=f"Committee debating {len(picks)} opportunities…")

    board: list[dict] = []
    for pick in picks:
        sym = pick["symbol"]
        decision_id = f"{sym}-{uuid.uuid4().hex[:8]}"
        price_ctx = window.get(sym, {}).get("price")
        arts = candidates.get(sym, [])
        yield _ev("debate_start", symbol=sym, edge=pick.get("edge_hint"))

        try:
            # briefs (parallel, but emit as each returns)
            tech = await loop.run_in_executor(
                None, briefs_mod.technical_brief, sym, price_ctx, decision_id)
            yield _ev("brief", symbol=sym, **tech)
            nb = await loop.run_in_executor(
                None, briefs_mod.news_brief, sym, arts, decision_id)
            yield _ev("brief", symbol=sym, **nb)
            briefs = [tech, nb]

            history = await loop.run_in_executor(None, store.symbol_history, sym)
            thesis = await loop.run_in_executor(
                None, lambda: committee.analyst_thesis(sym, pick["reason"], briefs, history, decision_id))
            model_tags = {"analyst": thesis.pop("_downgraded_model", MODEL_MAP["analyst"])}
            yield _ev("thesis", symbol=sym, **thesis)

            concerns_out = await loop.run_in_executor(
                None, lambda: committee.skeptic_challenge(sym, thesis, briefs, decision_id))
            model_tags["skeptic"] = concerns_out.pop("_downgraded_model", MODEL_MAP["skeptic"])
            concerns = concerns_out.get("concerns", [])
            for c in concerns:
                yield _ev("concern", symbol=sym, **c)

            flags = committee.fact_check_concerns(concerns, price_ctx)
            for f in flags:
                yield _ev("fact_flag", symbol=sym, text=f)

            rebuttal = await loop.run_in_executor(
                None, lambda: committee.analyst_rebuttal(sym, thesis, concerns, decision_id))
            rebuttal.pop("_downgraded_model", None)
            yield _ev("rebuttal", symbol=sym, **rebuttal)

            verdict = await loop.run_in_executor(
                None, lambda: committee.arbiter_verdict(sym, thesis, concerns, rebuttal, flags, decision_id))
            model_tags["arbiter"] = verdict.pop("_downgraded_model", MODEL_MAP["arbiter"])
        except LLMError as exc:
            yield _ev("status", msg=f"{sym}: dropped ({exc})")
            continue

        sess = session()
        horizon = int(verdict.get("adjusted_horizon_days") or thesis["horizon_days"])
        pick_id = store.record_pick({
            "symbol": sym, "arm": "COMMITTEE", "edge": pick.get("edge_hint"),
            "trigger_src": "FIND_TRADES", "session": sess,
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
        board.append(row)
        yield _ev("decision", **row)

    # Chief — genuine head-to-head comparison across every debated idea
    # (not just sorting isolated conviction numbers). One Opus call.
    if len(board) >= 1:
        yield _ev("status", msg="Chief comparing all opportunities head-to-head…")
        try:
            chief = await loop.run_in_executor(
                None, lambda: committee.chief_synthesis(board, "chief"))
            ranking = {r["symbol"].upper(): r for r in chief.get("ranked", [])}
            order = {r["symbol"].upper(): i for i, r in enumerate(chief.get("ranked", []))}
            for row in board:
                cr = ranking.get(row["symbol"].upper())
                row["take"] = bool(cr["take"]) if cr else row["approved"]
                row["chief_reason"] = cr["reason"] if cr else ""
            board.sort(key=lambda r: order.get(r["symbol"].upper(), 999))
            store.add_run("FIND_TRADES", board)
            yield _ev("chief", board=board, summary=chief.get("summary", ""))
            yield _ev("done", board=board)
            return
        except LLMError as exc:
            log.warning("Chief synthesis failed (%s) — falling back to score sort", exc)

    # fallback: no Chief → sort isolated scores
    for row in board:
        row["take"] = row["approved"]
        row["chief_reason"] = ""
    board.sort(key=lambda r: (not r["approved"], -abs(r["conviction"] - 50)))
    yield _ev("done", board=board)
