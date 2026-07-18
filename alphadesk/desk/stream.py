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

from alphadesk.config import (
    EARNINGS_DRIFT_DAYS,
    EXPOSURE_MAX_SHOCKS,
    MODEL_MAP,
    REPICK_COOLDOWN_HOURS,
    SOLO_ARM_EVERY_N,
    session,
)
from alphadesk.desk import (
    connections,
    debate,
    earnings_reader,
    loner,
    news_check,
    notes,
    review,
    scout,
    team,
)
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


def _intensity(articles: list[dict]) -> float:
    """Shock materiality proxy: |avg sentiment| × coverage."""
    return abs(_avg_sentiment(articles)) * len(articles)


async def stream_find_trades(hours: float = 48.0, max_debates: int = 6,
                             expose: bool = False, is_disconnected=None):
    """Async generator of deliberation events. Broad news window (default 48h —
    a batch run can afford to look far wider than the old live-tick engine).
    Stops early if the client disconnects (no more wasted LLM spend)."""
    loop = asyncio.get_running_loop()

    async def _gone() -> bool:
        return bool(is_disconnected and await is_disconnected())

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

    # Earnings drift — names that reported in the last few days are first-class
    # candidates (the desk's cleanest DRIFT edge). Injected as synthetic EARNINGS
    # "articles" so they flow through the same triage → committee pipeline; the
    # committee judges whether the post-earnings move continues.
    reported = await loop.run_in_executor(None, store.recently_reported, EARNINGS_DRIFT_DAYS)
    for e in reported:
        if await _gone():
            return
        esym, surp = e["symbol"], (e.get("surprise_pct") or 0.0)
        beat = "beat" if surp > 0 else ("miss" if surp < 0 else "in-line")
        arts = candidates.setdefault(esym, [])
        arts.insert(0, {
            "id": f"earnings-{esym}-{e['report_date'][:10]}",
            "title": f"[EARNINGS] {esym} reported {e['report_date'][:10]} {e.get('session') or ''}: "
                     f"EPS {e.get('eps_actual')} vs est {e.get('eps_estimate')} — {beat} {surp}%",
            "summary": f"Post-earnings-drift setup: {esym} {beat} consensus by {surp}%.",
            "source": "EarningsCalendar", "url": "", "published_at": e["report_date"],
            "category": "EARNINGS", "tickers": [esym],
            "mentions": [{"symbol": esym, "sentiment": round(max(-1.0, min(1.0, surp / 10.0)), 3),
                          "label": ("positive" if surp > 0 else "negative" if surp < 0 else "neutral"),
                          "category": "EARNINGS"}],
            "relations": [],
        })
        # Anti-double-dip: the name may also have surfaced in the news scan on the
        # SAME earnings story — merge (one candidate per symbol) and dedup articles
        # by id so the same story is never counted twice.
        seen: set = set()
        deduped = []
        for a in arts:
            if a.get("id") not in seen:
                seen.add(a.get("id"))
                deduped.append(a)
        arts[:] = deduped
    if reported:
        yield _ev("status", msg=f"{len(reported)} name(s) reported in the last "
                                f"{EARNINGS_DRIFT_DAYS}d — added as post-earnings-drift candidates.")

    # Position review — BEFORE hunting new trades (and even in a quiet window),
    # re-check every still-open TAKE from earlier runs against current price +
    # fresh news, and issue HOLD/EXIT with a reason. You may have traded the
    # original call, so exits are surfaced first and stamped in the ledger.
    open_positions = await loop.run_in_executor(None, store.open_taken_picks)
    if open_positions:
        yield _ev("status", msg=f"Reviewing {len(open_positions)} open position(s) from earlier runs…")
        for pos in open_positions:
            if await _gone():
                return
            psym = pos["symbol"]
            pctx = await loop.run_in_executor(None, prices.get_context, psym)
            fresh = candidates.get(psym, [])
            verdict = await loop.run_in_executor(
                None, review.review_position, pos, pctx, fresh, f"reeval-{pos['id']}")
            if verdict["decision"] == "EXIT":
                await loop.run_in_executor(None, store.record_exit, pos["id"], verdict["reason"])
                yield _ev("position_exit", id=pos["id"], symbol=psym, direction=pos["direction"],
                          horizon_days=pos["horizon_days"], entry=pos.get("entry_price"),
                          now=(pctx or {}).get("last_price"), reason=verdict["reason"])
            else:
                yield _ev("position_hold", id=pos["id"], symbol=psym, direction=pos["direction"],
                          horizon_days=pos["horizon_days"], reason=verdict["reason"])

    if not candidates:
        yield _ev("status", msg="No fresh catalysts found in that window.")
        yield _ev("done", board=[])
        return

    yield _ev("status", msg=f"{n} articles → {len(candidates)} companies with catalysts.")

    ripple_syms: set[str] = set()   # names the Exposure Desk surfaced (prioritized into triage)

    # Exposure Desk — expand the most material shocks into ripple candidates
    # (the connected, tradable names that haven't moved). Gated to the top-N
    # most intense shocks for cost. Set expose=False for a light run.
    if expose and candidates and not await _gone():
        shocks = sorted(candidates.items(), key=lambda kv: -_intensity(kv[1]))
        # Dedupe shocks that are the SAME underlying event: Polygon tags one story
        # with several ticker variants (GOOG/GOOGM/GOOGN), which would otherwise
        # burn the top-N slots web-mapping the same company. Skip a shock whose
        # headlines overlap one already chosen.
        shock_inputs: list[tuple[str, str]] = []
        seen_events: list[set] = []
        for sym, arts in shocks:
            if _intensity(arts) <= 0.1:
                continue
            key = {a.get("id") or a.get("title", "") for a in arts[:3]}
            if any(key & prev for prev in seen_events):
                continue
            seen_events.append(key)
            shock_inputs.append((sym, " | ".join(a.get("title", "")[:120] for a in arts[:3])))
            if len(shock_inputs) >= EXPOSURE_MAX_SHOCKS:
                break
        if shock_inputs:
            yield _ev("status",
                      msg=f"Exposure Desk mapping supply-chain ripples from "
                          f"{len(shock_inputs)} material shocks (web-verified)…")
            for sym, _ in shock_inputs:
                yield _ev("exposure_shock", symbol=sym)
            exp_results = await connections.run_connections(shock_inputs, "exposure")
            added = 0
            for res in exp_results:
                for c in res["candidates"]:
                    csym = c["symbol"]
                    if csym in candidates:
                        continue  # already surfaced directly by the news
                    sentiment = 0.5 if c["direction"] == "LONG" else -0.5
                    candidates.setdefault(csym, []).append({
                        "id": f"ripple-{res['shock']}-{csym}",
                        "title": f"[RIPPLE from {res['shock']}] {c['chain'][:110]}",
                        "summary": f"HYPOTHESIS ({c['strength']}): {c['chain']}",
                        "source": "ExposureDesk", "url": "",
                        "published_at": since_dt.isoformat(), "category": "RIPPLE",
                        "tickers": [csym],
                        "mentions": [{"symbol": csym, "sentiment": sentiment,
                                      "label": c["direction"].lower(), "category": "RIPPLE"}],
                        "relations": [],
                    })
                    ripple_syms.add(csym)
                    yield _ev("exposure_candidate", shock=res["shock"], symbol=csym,
                              direction=c["direction"], chain=c["chain"], strength=c["strength"])
                    added += 1
            yield _ev("status", msg=f"Exposure Desk surfaced {added} ripple candidates.")

    # Anti-double-dip across runs — but not blind to NEW catalysts:
    #  • names we already HOLD → skip (the position review re-evaluated them; new
    #    adverse news there triggers an EXIT, so they're covered).
    #  • names debated within the cooldown → skip UNLESS a materiality check says a
    #    genuinely NEW catalyst arrived since that debate (same story != new event).
    held = {p["symbol"].upper() for p in open_positions}
    cooling = await loop.run_in_executor(None, store.symbols_debated_since, REPICK_COOLDOWN_HOURS)
    dropped: list[str] = []
    for s in list(candidates):
        su = s.upper()
        if su in held:
            candidates.pop(s, None)
            dropped.append(s)
            continue
        if su in cooling:
            last = await loop.run_in_executor(None, store.last_debate, su)
            ts = (last or {}).get("ts") or ""
            new_arts = [a for a in candidates[s] if str(a.get("published_at", "")) > ts]
            if new_arts:
                v = await loop.run_in_executor(
                    None, news_check.fresh_catalyst, s, last, new_arts, f"mat-{su}")
                if v.get("fresh_catalyst"):
                    yield _ev("status", msg=f"{s}: new development since last look — re-examining "
                                            f"({(v.get('reason') or '')[:90]}).")
                    continue  # a genuinely new catalyst — keep it in the pool
            candidates.pop(s, None)
            dropped.append(s)
    if dropped:
        yield _ev("status", msg=f"Skipped {len(dropped)} name(s): already held, or same story as a "
                                f"debate in the last {REPICK_COOLDOWN_HOURS}h (no re-dip).")
    if not candidates:
        yield _ev("status", msg="Nothing fresh to debate after de-duping held/recent names.")
        yield _ev("done", board=[])
        return

    yield _ev("status", msg="Triaging…")

    # Build the triage window (price context per symbol). Ripple candidates are
    # prioritized so the Exposure Desk's web-grounded work is never truncated out.
    ordered = (
        [kv for kv in candidates.items() if kv[0] in ripple_syms]
        + [kv for kv in candidates.items() if kv[0] not in ripple_syms]
    )
    window: dict[str, dict] = {}
    for sym, arts in ordered[:80]:
        ctx = await loop.run_in_executor(None, prices.get_context, sym)
        window[sym] = {
            "headlines": _headlines(arts),
            "avg_sentiment": _avg_sentiment(arts),
            "price": ctx,
        }
    movers = await loop.run_in_executor(None, prices.movers)

    try:
        result = await loop.run_in_executor(None, scout.run_scout, window, movers)
    except LLMError as exc:
        yield _ev("status", msg=f"Triage failed: {exc}")
        yield _ev("done", board=[])
        return

    picks = (result.get("picks") or [])[:max_debates]
    skips = result.get("skips") or []
    await loop.run_in_executor(None, store.record_skips, skips)  # grade forward: did we skip a mover?
    yield _ev("skips", skips=skips)
    for p in picks:
        yield _ev("triage_pick", symbol=p["symbol"], edge=p.get("edge_hint"),
                  reason=p.get("reason", ""))

    if not picks:
        yield _ev("status", msg="Triage found no opportunities worth full analysis right now.")
        yield _ev("done", board=[])
        return

    yield _ev("status", msg=f"Committee debating {len(picks)} opportunities…")

    # Grounded calibration prior — the desk's own graded scorecard, computed
    # once per run and handed to every analyst/solo call as facts (not lessons).
    calibration = team.calibration_block(
        await loop.run_in_executor(None, store.stats))

    board: list[dict] = []
    for pick_idx, pick in enumerate(picks):
        if await _gone():   # client closed the tab — stop burning quota
            log.info("Find Trades client disconnected — stopping after %d debates", pick_idx)
            return
        sym = pick["symbol"]
        decision_id = f"{sym}-{uuid.uuid4().hex[:8]}"
        price_ctx = window.get(sym, {}).get("price")
        arts = candidates.get(sym, [])
        yield _ev("debate_start", symbol=sym, edge=pick.get("edge_hint"))

        try:
            # brief subagents fan out in PARALLEL (technical, news, fundamentals,
            # freshness) — each a bounded Haiku research task feeding the analyst
            fundamentals = await loop.run_in_executor(None, prices.get_fundamentals, sym)
            briefs = list(await asyncio.gather(
                loop.run_in_executor(None, notes.market_brief, sym, price_ctx, fundamentals, arts, decision_id),
                loop.run_in_executor(None, notes.news_brief, sym, arts, decision_id),
            ))
            # If this pick just reported, read the ACTUAL report (web-grounded,
            # cached per event) — guidance/tone drive the drift, so it gets its own
            # evidence block. Only fires for names triage actually picked (lean).
            erow = await loop.run_in_executor(None, store.earnings_row, sym, EARNINGS_DRIFT_DAYS)
            if erow:
                read = await loop.run_in_executor(None, earnings_reader.get_or_read, erow, f"eread-{sym}")
                if read:
                    briefs.append({"kind": "earnings", "summary": read, "key_facts": []})
            for b in briefs:
                yield _ev("brief", symbol=sym, **b)

            history = await loop.run_in_executor(None, store.symbol_history, sym)
            # shared committee core — yields thesis/concern/fact_flag/rebuttal to
            # stream live, writes the ledger row, and returns it via "_result"
            row = None
            async for ev in debate.deliberate(sym, pick, briefs, price_ctx, history,
                                              calibration, "FIND_TRADES", decision_id):
                if ev["type"] == "_result":
                    row = ev["row"]
                else:
                    yield ev
        except LLMError as exc:
            yield _ev("status", msg=f"{sym}: dropped ({exc})")
            continue

        if row is None:   # core yielded no result (shouldn't happen) — book nothing
            continue
        sess = session()   # for the solo arm's entry-price stamp below
        board.append(row)
        yield _ev("decision", **row)

        # Solo control arm — every Nth pick, one strong agent works the SAME
        # briefs blind to the team. The ledger later answers: does the
        # committee actually beat one agent? (kill-criterion #2)
        if SOLO_ARM_EVERY_N and (pick_idx + 1) % SOLO_ARM_EVERY_N == 0:
            try:
                s = await loop.run_in_executor(
                    None, lambda: loner.loner_analysis(
                        sym, pick["reason"], briefs, history, decision_id + "-solo", calibration))
                s_model = s.pop("_downgraded_model", MODEL_MAP["loner"])
                store.record_pick({
                    "symbol": sym, "arm": "SOLO", "edge": pick.get("edge_hint"),
                    "trigger_src": "FIND_TRADES", "session": sess,
                    "direction": s["direction"], "horizon_days": s["horizon_days"],
                    "score": s["score"], "confidence": s["confidence"],
                    "approved": int(bool(s["approved"])), "triage_reason": pick["reason"],
                    "thesis": s["thesis"], "briefs": briefs, "model_tags": {"loner": s_model},
                    "low_liquidity": int(bool(price_ctx and price_ctx.get("low_liquidity"))),
                    "entry_price": (price_ctx or {}).get("last_price") if sess == "OPEN" else None,
                    "spy_price": (prices.get_context("SPY") or {}).get("last_price"),
                })
                yield _ev("loner", symbol=sym, direction=s["direction"],
                          horizon_days=s["horizon_days"], score=s["score"])
            except LLMError as exc:
                log.warning("Solo arm dropped %s: %s", sym, exc)

    # Chief — genuine head-to-head comparison across every debated idea
    # (not just sorting isolated conviction numbers). One Opus call.
    if len(board) >= 1:
        yield _ev("status", msg="Chief comparing all opportunities head-to-head…")
        try:
            chief = await loop.run_in_executor(
                None, lambda: team.head_ranking(board, "chief"))
            ranking = {r["symbol"].upper(): r for r in chief.get("ranked", [])}
            order = {r["symbol"].upper(): i for i, r in enumerate(chief.get("ranked", []))}
            for row in board:
                cr = ranking.get(row["symbol"].upper())
                row["take"] = bool(cr["take"]) if cr else row["approved"]
                row["chief_reason"] = cr["reason"] if cr else ""
            board.sort(key=lambda r: order.get(r["symbol"].upper(), 999))
            store.add_run("FIND_TRADES", board)
            store.mark_taken([r["id"] for r in board if r.get("take")])  # open positions to review next run
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
    store.mark_taken([r["id"] for r in board if r.get("take")])
    yield _ev("done", board=board)
