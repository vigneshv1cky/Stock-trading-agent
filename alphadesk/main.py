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

        from alphadesk.config import EXIT_REVIEW_COOLDOWN_S
        from alphadesk.config import session as market_session
        from alphadesk.desk import review
        from alphadesk.desk.plan import exit_signal, level_crossed, realized_exit
        from alphadesk.ingest import prices
        from alphadesk.ledger import store
        loop = asyncio.get_running_loop()
        log = logging.getLogger("alphadesk.watch")
        peak_fav: dict[int, float] = {}      # pick_id → best favorable move % seen
        reviewed_at: dict[int, float] = {}   # pick_id → monotonic ts of last review
        while True:
            try:
                if market_session() != "CLOSED":   # prices only move in-session
                    open_pos = await loop.run_in_executor(None, store.live_picks)
                    monitorable = [p for p in open_pos
                                   if p.get("plan_target") and p.get("plan_stop")]
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
                            if not cur:
                                continue
                            entry = p.get("entry_price") or p.get("plan_entry")
                            hit = level_crossed(p["direction"], cur,
                                                p["plan_target"], p["plan_stop"])
                            if hit:
                                level = p["plan_target"] if hit == "target" else p["plan_stop"]
                                label = "target hit" if hit == "target" else "stopped out"
                                reason = f"{label} @ {cur} ({hit} {level})"
                                # freeze realized performance at the exit price
                                perf = realized_exit(p["direction"], entry, cur,
                                                     p.get("spy_price"), spy_now)
                                await loop.run_in_executor(
                                    None, lambda pid=p["id"], r=reason, pf=perf:
                                    store.record_exit(pid, r, **pf))
                                log.info("Auto-exit #%d %s %s — %s (%s%% vs SPY)",
                                         p["id"], p["symbol"], p["direction"], reason,
                                         perf.get("exit_alpha"))
                                continue
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
