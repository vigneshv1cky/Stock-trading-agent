"""AlphaDesk entrypoint.

  python -m alphadesk.main run        # scheduler + dashboard (the live system)
  python -m alphadesk.main backfill --hours 168
  python -m alphadesk.main grade      # one grading pass
  python -m alphadesk.main status     # ledger summary
"""

import argparse
import asyncio
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # yfinance logs BRK.A/.B-style "possibly delisted" at ERROR for tickers it
    # can't price; the app handles missing prices, so silence the spam.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _web_server():
    import os

    import uvicorn

    from alphadesk.app.dashboard import app as dashboard_app

    return uvicorn.Server(uvicorn.Config(
        dashboard_app,
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),  # VM sets 0.0.0.0
        port=int(os.environ.get("DASHBOARD_PORT", "8000")),
        log_level="warning",
    ))


async def _run() -> None:
    """Legacy autonomous mode: 24/7 scheduler + dashboard."""
    from alphadesk.app import scheduler
    await asyncio.gather(scheduler.run_forever(), _web_server().serve())


async def _serve() -> None:
    """v2 on-demand mode: dashboard + hourly portfolio grader (pure code, no
    LLM). Trades run only when you click Find Trades; the grader keeps the
    paper portfolio marking even while nothing else runs."""

    async def _grader_loop():
        from alphadesk.app import scheduler
        from alphadesk.ledger.grader import grade_due
        loop = asyncio.get_running_loop()
        log = logging.getLogger("alphadesk.grader")
        while True:
            try:
                n = await loop.run_in_executor(None, grade_due)
                if n:
                    log.info("Graded %d positions", n)
            except Exception as exc:
                log.error("grader error: %s", exc)
            scheduler.beat()   # liveness for /healthz in dashboard mode (no ingest loop here)
            await asyncio.sleep(3600)

    async def _earnings_loop():
        from alphadesk.ingest import earnings
        loop = asyncio.get_running_loop()
        log = logging.getLogger("alphadesk.earnings")
        while True:
            try:
                await loop.run_in_executor(None, earnings.refresh_calendar)
            except Exception as exc:
                log.error("earnings refresh error: %s", exc)
            await asyncio.sleep(6 * 3600)   # 4×/day keeps upcoming + recent fresh

    async def _position_watch_loop():
        """Watch open picks between runs. Two layers, both research/paper (a ledger
        exit stamp, never an order):
          • level cross (target/stop) → close immediately. Pure code, no token cost.
          • cheap exit SCREEN (near-target / MFE give-back) → escalate that ONE
            position to the opus reviewer, which decides HOLD/EXIT. So a spent move
            (a beat that popped past its implied move) gets closed before the gain
            decays, instead of waiting for the next Find Trades run. Code owns the
            cheap watching; the reviewer owns the judgment. Escalation is throttled
            per position (EXIT_REVIEW_COOLDOWN_S) so it can't spam the model."""
        import time as _time

        from alphadesk.config import (
            EXIT_REVIEW_COOLDOWN_S,
            LIMIT_FILL_BUFFER_PCT,
            LIMIT_FILL_MIN_CUSHION_FRAC,
            entry_fill_time,
            now_et,
        )
        from alphadesk.config import session as market_session
        from alphadesk.desk import review
        from alphadesk.desk.plan import (
            exit_signal,
            first_touch_exit,
            level_crossed,
            limit_fill,
            realized_exit,
        )
        from alphadesk.ingest import prices
        from alphadesk.ledger import store
        loop = asyncio.get_running_loop()
        log = logging.getLogger("alphadesk.watch")
        peak_fav: dict[int, float] = {}      # pick_id → best favorable move % seen
        reviewed_at: dict[int, float] = {}   # pick_id → monotonic ts of last review
        last_check: dict[int, object] = {}   # pick_id → ET ts we last walked bars up to
        while True:
            try:
                if market_session() == "OPEN":   # Model A: only fill/exit in regular hours (skip thin pre/after-market)
                    open_pos = await loop.run_in_executor(None, store.live_picks)
                    # Model A fill: once a closed-market pick's 9:30 open has passed,
                    # resolve its entry from the fill-day OHLC. Market → the open; limit
                    # → its level if price reached it (stamp entry_price so live P&L /
                    # exits measure from the real fill), else the limit never triggered
                    # → NOT TAKEN (no fill, no P&L).
                    now = now_et()
                    unfilled = [p for p in open_pos if p.get("entry_price") is None
                                and (ft := entry_fill_time(p["ts"], p.get("session"))) and ft <= now]
                    not_taken_ids: set[int] = set()
                    if unfilled:
                        items = [{"id": p["id"], "symbol": p["symbol"],
                                  "fill_date": entry_fill_time(p["ts"], p["session"]).strftime("%Y-%m-%d")}
                                 for p in unfilled]
                        ohlc = await loop.run_in_executor(None, prices.fill_ohlc, items)
                        for p in unfilled:
                            if p["id"] not in ohlc:
                                continue
                            o, h, low = ohlc[p["id"]]
                            px = limit_fill(p["direction"], p.get("order_type"),
                                            p.get("plan_entry"), o, h, low, LIMIT_FILL_BUFFER_PCT,
                                            stop=p.get("plan_stop"),
                                            min_cushion_frac=LIMIT_FILL_MIN_CUSHION_FRAC)
                            if px is not None:
                                await loop.run_in_executor(
                                    None, lambda i=p["id"], x=px: store.set_entry_price(i, x))
                                p["entry_price"] = px   # use it this pass too
                            else:
                                reason = (f"not taken: limit {p.get('plan_entry')} not filled — price never "
                                          "reached it, or gapped against the thesis too close to the stop")
                                await loop.run_in_executor(
                                    None, lambda i=p["id"], r=reason: store.record_exit(i, r))
                                not_taken_ids.add(p["id"])
                                log.info("Not taken #%d %s — limit %s not reached",
                                         p["id"], p["symbol"], p.get("plan_entry"))
                    # Only auto-exit positions that were actually TAKEN and FILLED —
                    # live_picks also carries counterfactuals the Head passed on and
                    # not-yet-filled limits (entry_price is NULL); exiting those would
                    # stamp realized P&L off plan_entry as a phantom fill. (The run-level
                    # review uses open_taken_picks, which already filters taken=1; the
                    # between-run watcher must match it.)
                    monitorable = [p for p in open_pos if p["id"] not in not_taken_ids
                                   and p.get("taken") and p.get("entry_price") is not None
                                   and p.get("plan_target") and p.get("plan_stop")]
                    live_ids = {p["id"] for p in open_pos}
                    for stale in [i for i in peak_fav if i not in live_ids]:
                        peak_fav.pop(stale, None)
                        reviewed_at.pop(stale, None)
                    if monitorable:
                        quotes = await loop.run_in_executor(
                            None, prices.latest_prices,
                            [p["symbol"] for p in monitorable] + ["SPY"])
                        spy_now = quotes.get("SPY")
                        for p in monitorable:
                            cur = quotes.get(p["symbol"].upper())
                            entry = p.get("entry_price") or p.get("plan_entry")
                            # Walk the true intraday PATH since we last looked (first sight:
                            # since the fill) to find the FIRST level touched — priced AT the
                            # level, gap-aware, and order-aware when a bar spans both. This
                            # replaces reading a single ~180s spot quote (which mis-booked the
                            # wrong level on a target-then-stop bar and froze P&L at whatever
                            # price the poll happened to catch). Falls back to the spot quote
                            # only when intraday bars are unavailable.
                            start = last_check.get(p["id"]) or entry_fill_time(
                                p["ts"], p.get("session"))
                            bars = (await loop.run_in_executor(
                                None, prices.intraday_bars, p["symbol"], start)
                                if start else [])
                            last_check[p["id"]] = now
                            ft = (first_touch_exit(p["direction"], p["plan_target"],
                                                   p["plan_stop"], bars) if bars else None)
                            if ft is None and not bars and cur:   # no bars → spot fallback
                                hit = level_crossed(p["direction"], cur,
                                                    p["plan_target"], p["plan_stop"])
                                if hit:
                                    ft = {"level": hit, "price": p["plan_target"]
                                          if hit == "target" else p["plan_stop"]}
                            if ft:
                                label = "target hit" if ft["level"] == "target" else "stopped out"
                                exit_px = ft["price"]
                                reason = f"{label} @ {exit_px} (first-touch {ft['level']})"
                                perf = realized_exit(p["direction"], entry, exit_px,
                                                     p.get("spy_price"), spy_now)
                                await loop.run_in_executor(
                                    None, lambda pid=p["id"], r=reason, pf=perf:
                                    store.record_exit(pid, r, **pf))
                                log.info("Auto-exit #%d %s %s — %s (%s%% vs SPY)",
                                         p["id"], p["symbol"], p["direction"], reason,
                                         perf.get("exit_alpha"))
                                continue
                            if not cur:
                                continue   # nothing crossed and no spot price → nothing to screen
                            # track the peak favorable move, then run the cheap screen.
                            # Fold in the PERSISTED MFE (grade_paths, daily High/Low):
                            # it survives restarts and catches intraday spikes between
                            # 180s polls, so a position that already ran up still fires
                            # give-back after a restart (e.g. PKE peaked +12% then faded).
                            if entry:
                                up = p["direction"] == "LONG"
                                fav = (cur - entry) / entry * 100 * (1 if up else -1)
                                peak_fav[p["id"]] = max(
                                    peak_fav.get(p["id"], 0.0), p.get("mfe_pct") or 0.0, fav)
                            flag = exit_signal(p["direction"], entry, cur,
                                               p["plan_target"], p["plan_stop"],
                                               peak_fav.get(p["id"], 0.0))
                            if not flag:
                                continue
                            last = reviewed_at.get(p["id"], 0.0)
                            if _time.monotonic() - last < EXIT_REVIEW_COOLDOWN_S:
                                continue   # throttled — don't spam the reviewer
                            reviewed_at[p["id"]] = _time.monotonic()
                            pctx = await loop.run_in_executor(
                                None, prices.get_context, p["symbol"])
                            verdict = await loop.run_in_executor(
                                None, review.review_position, p, pctx, [],
                                f"watch-{p['id']}")
                            log.info("Screen flagged #%d %s (%s) → reviewer: %s — %s",
                                     p["id"], p["symbol"], flag, verdict["decision"],
                                     verdict["reason"])
                            if verdict["decision"] == "EXIT":
                                exit_px = (pctx or {}).get("last_price") or cur
                                perf = realized_exit(p["direction"], entry, exit_px,
                                                     p.get("spy_price"), spy_now)
                                await loop.run_in_executor(
                                    None, lambda pid=p["id"], r=verdict["reason"], pf=perf:
                                    store.record_exit(pid, r, **pf))
                                log.info("Review-exit #%d %s — %s (%s%% vs SPY)",
                                         p["id"], p["symbol"], verdict["reason"],
                                         perf.get("exit_alpha"))
            except Exception as exc:
                log.error("position watch error: %s", exc)
            from alphadesk.app import scheduler
            scheduler.beat()   # 180s liveness for /healthz (grader's hourly beat is too coarse)
            await asyncio.sleep(180)   # ~3 min; a hit closes the paper position

    await asyncio.gather(_grader_loop(), _earnings_loop(),
                         _position_watch_loop(), _web_server().serve())


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="alphadesk")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="autonomous: 24/7 scheduler + dashboard (legacy)")
    sub.add_parser("dashboard", help="v2 on-demand: dashboard only — trades run on button click")
    p_back = sub.add_parser("backfill")
    p_back.add_argument("--hours", type=float, default=72)
    p_desk = sub.add_parser("desk", help="convene the team NOW on recent news")
    p_desk.add_argument("--hours", type=float, default=8,
                        help="news lookback for the candidate window")
    p_world = sub.add_parser("world", help="one GDELT world-news tick (optionally to the desk)")
    p_world.add_argument("--categories", type=int, default=3)
    p_world.add_argument("--to-desk", action="store_true",
                         help="send exposure candidates to the team")
    sub.add_parser("grade")
    sub.add_parser("status")
    sub.add_parser("abtest", help="reaction-gate A/B: forward alpha bucketed by reaction size")
    sub.add_parser("alpha", help="honest alpha: SPY-relative alpha_net vs beta-adjusted, borrow-aware alpha_adj")
    sub.add_parser("earnings", help="refresh the earnings calendar and show upcoming / recent")
    args = parser.parse_args()

    if args.cmd == "run":
        from alphadesk.ledger import store
        store.install_token_sink()
        asyncio.run(_run())
    elif args.cmd == "dashboard":
        from alphadesk.ledger import store
        store.install_token_sink()
        log = logging.getLogger("alphadesk")
        log.info("Dashboard on http://%s:%s — click Find Trades to run",
                 __import__("os").environ.get("DASHBOARD_HOST", "127.0.0.1"),
                 __import__("os").environ.get("DASHBOARD_PORT", "8000"))
        asyncio.run(_serve())
    elif args.cmd == "backfill":
        from alphadesk.ingest.news import catch_up
        from alphadesk.ledger import store
        store.install_token_sink()
        n = catch_up(args.hours)
        print(f"backfilled {n} articles")
    elif args.cmd == "desk":
        from datetime import datetime, timedelta, timezone

        from alphadesk.desk.workflow import research_run
        from alphadesk.ingest import news
        from alphadesk.ledger import store
        store.install_token_sink()

        async def _adhoc() -> None:
            n, candidates = await asyncio.get_running_loop().run_in_executor(
                None, news.poll,
                datetime.now(timezone.utc) - timedelta(hours=args.hours),
            )
            print(f"{n} fresh articles, {len(candidates)} candidate symbols")
            if candidates:
                ids = await research_run(candidates, trigger_src="DEEP_RUN")
                print(f"team produced {len(ids)} decisions — see the dashboard")
            else:
                print("no fresh candidates in that window")

        asyncio.run(_adhoc())
    elif args.cmd == "world":
        from alphadesk.ingest import world
        from alphadesk.ledger import store
        store.install_token_sink()
        n, candidates = world.poll(categories_per_tick=args.categories)
        print(f"{n} relevant world events → {len(candidates)} exposure candidates")
        for sym, arts in candidates.items():
            for a in arts:
                print(f"  {sym}: {a['title'][:90]}")
                print(f"     {a['summary'][:160]}")
        if args.to_desk and candidates:
            from alphadesk.desk.workflow import research_run
            ids = asyncio.run(research_run(candidates, trigger_src="STREAM"))
            print(f"team produced {len(ids)} decisions")
    elif args.cmd == "grade":
        from alphadesk.ledger.grader import grade_due
        print(f"graded {grade_due()} picks")
    elif args.cmd == "status":
        from alphadesk.ledger import store
        print("ledger:", store.stats()["total"])
        print("tokens:", store.token_summary(days=1))
    elif args.cmd == "abtest":
        from alphadesk.config import MATERIAL_REACTION_PCT
        from alphadesk.ledger import store
        rows = store.reaction_ab_rows()
        # bucket by |reaction|; the gate keeps everything at/above MATERIAL_REACTION_PCT
        edges = [0.0, 1.0, MATERIAL_REACTION_PCT, 3.0, 6.0, float("inf")]
        labels = [f"<{edges[1]:g}%", f"{edges[1]:g}-{edges[2]:g}%",
                  f"{edges[2]:g}-3%", "3-6%", ">6%"]
        buckets: list[list[float]] = [[] for _ in labels]
        for r in rows:
            mag = abs(r["reaction_total"])
            for i in range(len(labels)):
                if edges[i] <= mag < edges[i + 1]:
                    buckets[i].append(r["alpha_net"])
                    break
        print(f"\n=== reaction-gate A/B — forward alpha vs SPY by reaction size "
              f"(gate keeps ≥ {MATERIAL_REACTION_PCT:g}%) ===")
        print(f"  {'bucket':10} {'gate':5} {'n':>4} {'mean α':>9} {'median α':>9} {'win%':>6}")
        for lab, vals in zip(labels, buckets):
            kept = "keep" if edges[labels.index(lab)] >= MATERIAL_REACTION_PCT else "drop"
            if vals:
                vals_sorted = sorted(vals)
                mean = sum(vals) / len(vals)
                median = vals_sorted[len(vals) // 2]
                win = 100.0 * sum(1 for v in vals if v > 0) / len(vals)
                print(f"  {lab:10} {kept:5} {len(vals):>4} {mean:>8.2f}% {median:>8.2f}% {win:>5.0f}%")
            else:
                print(f"  {lab:10} {kept:5} {0:>4} {'—':>9} {'—':>9} {'—':>6}")
        n = len(rows)
        if n < 20:
            print(f"\n  ({n} graded — too few to read yet; let it accumulate).")
        else:
            drop = [r["alpha_net"] for r in rows if not r["gate_passed"]]
            keep = [r["alpha_net"] for r in rows if r["gate_passed"]]
            dm = sum(drop) / len(drop) if drop else 0.0
            km = sum(keep) / len(keep) if keep else 0.0
            print(f"\n  dropped arm: n={len(drop)} mean α={dm:+.2f}%   "
                  f"kept arm: n={len(keep)} mean α={km:+.2f}%")
            print("  → dropped arm α ≥ kept arm α means the gate is cutting winners.")
    elif args.cmd == "alpha":
        from alphadesk.ledger import store
        rows = store.alpha_comparison()

        def _agg(rs):
            if not rs:
                return (0, None, None, None)
            net = sum(r["alpha_net"] for r in rs) / len(rs)
            adj = sum(r["alpha_adj"] for r in rs) / len(rs)
            beta = sum(r["beta"] for r in rs if r["beta"] is not None)
            nb = sum(1 for r in rs if r["beta"] is not None)
            return (len(rs), net, adj, (beta / nb) if nb else None)

        longs = [r for r in rows if r["direction"] == "LONG"]
        shorts = [r for r in rows if r["direction"] == "SHORT"]
        print("\n=== honest alpha — SPY-relative (alpha_net) vs beta-adjusted + borrow-aware (alpha_adj) ===")
        print(f"  {'cohort':7} {'n':>4} {'mean net':>10} {'mean adj':>10} {'β drag':>8} {'mean β':>7}")
        for name, rs in (("all", rows), ("longs", longs), ("shorts", shorts)):
            n, net, adj, beta = _agg(rs)
            if n:
                drag = net - adj
                bstr = f"{beta:.2f}" if beta is not None else "—"
                print(f"  {name:7} {n:>4} {net:>9.2f}% {adj:>9.2f}% {drag:>7.2f}% {bstr:>7}")
            else:
                print(f"  {name:7} {0:>4} {'—':>10} {'—':>10} {'—':>8} {'—':>7}")
        if not rows:
            print("\n  (no picks graded with both metrics yet — grade forward, then re-check).")
        else:
            print("\n  β drag = how much alpha_net OVERSTATED vs the beta-adjusted number "
                  "(positive = booked beta/borrow as alpha).")
    elif args.cmd == "earnings":
        from alphadesk.ingest import earnings
        from alphadesk.ledger import store
        print(f"calendar refreshed: {earnings.refresh_calendar()} rows")
        up = store.upcoming_earnings(days=7)
        print(f"\n=== reporting in the next 7 days ({len(up)}) ===")
        for e in up[:30]:
            print(f"  {e['report_date'][:16]}  {e['session'] or '?':3}  {e['symbol']:6}  est={e['eps_estimate']}")
        rec = store.recently_reported(days=3)
        print(f"\n=== reported in the last 3 days ({len(rec)}) ===")
        for e in rec:
            print(f"  {e['report_date'][:16]}  {e['symbol']:6}  est={e['eps_estimate']} act={e['eps_actual']} surprise={e['surprise_pct']}%")
    sys.exit(0)


if __name__ == "__main__":
    main()
