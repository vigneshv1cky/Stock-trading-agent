"""Dashboard — FastAPI serving the shadcn/ui SPA + JSON API.

Auth: HTTP Basic — enforced ONLY when the server is bound to a non-loopback host
(the public GCP VM sets DASHBOARD_HOST=0.0.0.0). A local 127.0.0.1 run needs no
password (we're not live); a public bind still fail-closes if ADMIN_USERNAME/
ADMIN_PASSWORD are unset.
Frontend: built by `pnpm build` in alphadesk/ui → alphadesk/app/static/.
"""

import base64
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from alphadesk.ledger import store

_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="AlphaDesk")


def _auth_required() -> bool:
    """Auth is enforced ONLY when bound to a non-loopback host — i.e. reachable
    off-box (the GCP VM sets DASHBOARD_HOST=0.0.0.0). A local 127.0.0.1 run is not
    exposed, so it needs no password: no auth wall while we're not live."""
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip().lower()
    return host not in ("", "127.0.0.1", "localhost", "::1")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    # /healthz is the ONLY unauthenticated path — the external watchdog's probe.
    # It exposes liveness only, never data.
    if request.url.path == "/healthz":
        return await call_next(request)
    # Local (loopback) run — open, no password.
    if not _auth_required():
        return await call_next(request)
    user = os.environ.get("ADMIN_USERNAME", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not user or not password:
        return Response("auth not configured", status_code=503)
    header = request.headers.get("Authorization", "")
    ok = False
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            u, _, p = decoded.partition(":")
            ok = secrets.compare_digest(u.encode(), user.encode()) and secrets.compare_digest(
                p.encode(), password.encode()
            )
        except Exception:
            ok = False
    if not ok:
        return Response(
            "unauthorized", status_code=401, headers={"WWW-Authenticate": "Basic realm=alphadesk"}
        )
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness for the GCP uptime check: 200 while the ingest loop is
    cycling, 503 if it has been silent >30 min (hung loop / dead scheduler).
    First 30 min after boot count as healthy (startup grace)."""
    from alphadesk.app import scheduler
    age = scheduler.heartbeat_age_s()
    if age < 1800 or age == float("inf") and _process_age_s() < 1800:
        return {"ok": True}
    if age == float("inf"):
        return Response("scheduler never ticked", status_code=503)
    return Response(f"ingest silent {int(age)}s", status_code=503)


_BOOT_MONO = __import__("time").monotonic()


def _process_age_s() -> float:
    import time
    return time.monotonic() - _BOOT_MONO


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.get("/api/picks/{pick_id}")
def api_pick(pick_id: int):
    pick = store.get_pick(pick_id)
    if not pick:
        raise HTTPException(404, "no such pick")
    return pick


@app.get("/api/stats")
def api_stats():
    return store.stats()


@app.get("/api/tokens")
def api_tokens(days: int = 1):
    return {"days": days, "usage": store.token_summary(days)}


@app.get("/api/sources")
def api_sources(days: int = 30):
    """Per ingestion source: articles in, tokens spent (ingest + debate), and
    value (picks / graded / avg alpha) — which channel earns its tokens."""
    return {"days": days, "sources": store.source_scorecard(days)}


@app.get("/api/earnings")
def api_earnings():
    """Be-ready view: who reports next (with the time to RUN the desk to catch the
    drift) and who just reported."""
    from alphadesk.config import now_et
    from alphadesk.ingest import prices
    from alphadesk.ingest.earnings import reported_public, run_at

    # Time-aware split: a report is "just reported" once it's PUBLIC (BMO/DAY at the
    # 9:30 open, AMC at the 16:00 close of its report day) — not when Nasdaq happens
    # to backfill the actual EPS. So a name reporting today flips after 9:30 today.
    now = now_et()
    upcoming, reported = [], []
    for e in store.earnings_window(days_back=4, days_fwd=14):
        pub = reported_public(e["report_date"], e.get("session"))
        if pub is not None and now >= pub:
            reported.append(e)
        else:
            e["run_at"] = run_at(e["report_date"], e.get("session"))
            upcoming.append(e)
    # Sort so the UI can group by run-day (earliest to run first) with the biggest
    # names surfaced first inside each day — never truncated by earlier small-caps.
    upcoming.sort(key=lambda e: (e["run_at"] or "9999", -(e.get("market_cap") or 0.0)))
    # newest report first, then group by report-day in the UI (biggest names first)
    # reverse=True → newest report day first AND biggest market cap first within a
    # day (a plain cap here, NOT -cap: reverse already flips it to descending).
    reported.sort(key=lambda e: (e["report_date"], e.get("market_cap") or 0.0), reverse=True)

    # Collapse dual-class listings of the same company (identical report date +
    # market cap to the dollar, e.g. GOOG/GOOGL) to one row. Two different firms
    # never share a 13-digit cap exactly, so this only merges share classes.
    def _dedupe_dual(rows: list[dict]) -> list[dict]:
        seen: set = set()
        out = []
        for e in rows:
            mc = e.get("market_cap")
            if mc:
                key = (e["report_date"], mc)
                if key in seen:
                    continue
                seen.add(key)
            out.append(e)
        return out

    # Sort so the UI can group by run-day (earliest to run first) with the biggest
    # names surfaced first inside each day — never truncated by earlier small-caps.
    upcoming.sort(key=lambda e: (e["run_at"] or "9999", -(e.get("market_cap") or 0.0)))
    upcoming = _dedupe_dual(upcoming)
    reported = _dedupe_dual(reported)
    # Show the real, verifiable signal: how much the stock has moved SINCE the
    # report went public (the drift itself) — not a maybe-misleading EPS surprise%.
    moves = prices.moves_since_report(reported)
    # Coverage self-assessment: did the desk act on each reporter? Match the desk's
    # engagement (pick/skip) that happened ON OR AFTER the report — the post-earnings
    # decision — so a pre-report pick doesn't count as catching the drift.
    eng = store.earnings_engagement([e["symbol"] for e in reported])
    for e in reported:
        e["move_since_report_pct"] = moves.get(e["symbol"])
        m = eng.get(e["symbol"].upper())
        if m and (m.get("ts") or "")[:10] >= e["report_date"][:10]:
            e["engagement"] = m["state"]
            e["engagement_pick_id"] = m.get("pick_id")
            e["engagement_dir"] = m.get("direction")
            e["engagement_verdict"] = m.get("verdict")
            e["engagement_why"] = m.get("why")
        else:
            e["engagement"] = "UNSEEN"     # never surfaced (or only picked pre-report)
    return {"upcoming": upcoming, "reported": reported}


def _alpha_so_far(direction: str, stock_then, cur, spy_then, spy_now):
    """Interim (unofficial) alpha: your return so far minus SPY over the SAME
    elapsed window, net of round-trip friction. None if a baseline is missing.
    This is a live mark, NOT the ledger grade (which settles only at the horizon).
    Same math the exit stamp freezes (plan.realized_exit) — one definition."""
    from alphadesk.desk.plan import realized_exit
    return realized_exit(direction, stock_then, cur, spy_then, spy_now)["exit_alpha"]


@app.get("/api/live")
def api_live():
    """Live tracking of open picks that carry a trade plan: current price vs
    entry/target/stop, P&L, alpha-so-far vs SPY, and a status. All pure arithmetic
    (code owns physics + scoring); the levels came from the desk. Alpha-so-far is a
    live mark, NOT the official grade — that still settles only at the horizon."""
    from alphadesk.config import session as market_session
    from alphadesk.desk import plan
    from alphadesk.ingest import prices
    picks = store.live_picks()
    quotes = prices.latest_prices([p["symbol"] for p in picks] + ["SPY"])
    spy_now = quotes.get("SPY")
    out = []
    for p in picks:
        cur = quotes.get(p["symbol"].upper())
        entry, target, stop = p["plan_entry"], p["plan_target"], p["plan_stop"]
        row = dict(p, current=cur, pnl_pct=None, progress=None, status="no quote",
                   alpha_so_far=_alpha_so_far(p["direction"], entry, cur,
                                              p.get("spy_price"), spy_now))
        if cur and entry and target and stop and target != stop:
            up = p["direction"] == "LONG"
            row["pnl_pct"] = round((1.0 if up else -1.0) * (cur - entry) / entry * 100, 2)
            prog = (cur - stop) / (target - stop) if up else (stop - cur) / (stop - target)
            row["progress"] = round(max(0.0, min(1.0, prog)), 3)  # 0 = at stop, 1 = at target
            hit = plan.level_crossed(p["direction"], cur, target, stop)
            if hit == "target":
                row["status"] = "target hit"
            elif hit == "stop":
                row["status"] = "stopped out"
            elif abs(cur - target) <= 0.15 * abs(target - entry):
                row["status"] = "near target"
            elif abs(cur - stop) <= 0.15 * abs(stop - entry):
                row["status"] = "near stop"
            else:
                row["status"] = "working"
        out.append(row)
    return {"live": out, "market": market_session()}


@app.get("/api/timelines")
def api_timelines(days: int = 30):
    """Track record grouped BY STOCK: each symbol's ordered calls with outcomes
    (open → live P&L; graded → vs S&P; exited), the desk's current stance, and
    whether that stance changed over time (buy→sell / an exit)."""
    from alphadesk.config import session as market_session
    from alphadesk.ingest import prices
    rows = store.recent_team_picks(days)
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    open_syms = [s for s, evs in by_sym.items()
                 if any(e["graded_at"] is None and e["exit_ts"] is None for e in evs)]
    quotes = prices.latest_prices(open_syms + ["SPY"])
    spy_now = quotes.get("SPY")

    symbols = []
    for sym, evs in by_sym.items():
        evs.sort(key=lambda e: e["id"])
        events = []
        for e in evs:
            state = "exited" if e["exit_ts"] else ("graded" if e["graded_at"] else "open")
            ev = dict(e, state=state, current=None, pnl_pct=None, status=None, alpha_so_far=None)
            if state == "open":
                cur = quotes.get(sym.upper())
                entry, target, stop = e["plan_entry"], e["plan_target"], e["plan_stop"]
                ev["current"] = cur
                ev["alpha_so_far"] = _alpha_so_far(e["direction"], entry, cur,
                                                   e.get("spy_price"), spy_now)
                if cur and entry and target and stop and target != stop:
                    up = e["direction"] == "LONG"
                    ev["pnl_pct"] = round((1.0 if up else -1.0) * (cur - entry) / entry * 100, 2)
                    hit_t = cur >= target if up else cur <= target
                    hit_s = cur <= stop if up else cur >= stop
                    ev["status"] = ("target hit" if hit_t else "stopped out" if hit_s
                                    else "working")
            events.append(ev)
        latest = evs[-1]
        current = ("EXITED" if latest["exit_ts"]
                   else latest["direction"] if latest["graded_at"] is None else "CLOSED")
        changed = len({e["direction"] for e in evs}) > 1 or any(e["exit_ts"] for e in evs)
        symbols.append({"symbol": sym, "current": current, "changed": changed,
                        "last_ts": latest["ts"], "events": events})
    symbols.sort(key=lambda s: s["last_ts"], reverse=True)           # recent first…
    symbols.sort(key=lambda s: 0 if s["current"] in ("LONG", "SHORT") else 1)  # …open on top
    return {"symbols": symbols, "market": market_session()}


_run_day = ""
_run_count = 0


def _within_daily_cap() -> bool:
    """Runaway guard: cap Find Trades runs per calendar day."""
    global _run_day, _run_count
    from datetime import date

    from alphadesk.config import MAX_RUNS_PER_DAY
    today = date.today().isoformat()
    if today != _run_day:
        _run_day, _run_count = today, 0
    if _run_count >= MAX_RUNS_PER_DAY:
        return False
    _run_count += 1
    return True


_run_log = logging.getLogger("alphadesk.run")


def _clip(s, n: int = 110) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _log_run_event(ev: dict) -> None:
    """Mirror a live Find Trades run to the terminal in real time — the FULL
    transcript, streamed as each event is produced (the terminal can afford more
    detail than the browser cards: the critic's actual pushback, the researcher's
    thesis and reply, fact-check flags, briefs, skips, holds, head reasoning)."""
    t = ev.get("type")
    sym = ev.get("symbol", "")
    if t == "status":
        _run_log.info("· %s", _clip(ev.get("msg", ""), 140))
    elif t == "skips":
        skips = ev.get("skips") or []
        if skips:
            names = ", ".join(s.get("symbol", "?") for s in skips[:12])
            _run_log.info("  passed on %d: %s%s", len(skips), names, " …" if len(skips) > 12 else "")
    elif t == "exposure_shock":
        _run_log.info("  shock   %-6s mapping supply-chain ripples", sym)
    elif t == "exposure_candidate":
        _run_log.info("  ripple  %s → %-6s %s  %s", ev.get("shock", ""), sym,
                      ev.get("direction", ""), _clip(ev.get("chain", ""), 90))
    elif t == "position_hold":
        _run_log.info("HOLD    %-6s %s", sym, _clip(ev.get("reason", ""), 100))
    elif t == "position_exit":
        _run_log.info("EXIT    %-6s %s", sym, _clip(ev.get("reason", ""), 100))
    elif t == "triage_pick":
        _run_log.info("SCOUT ▸ %-6s [%s] %s", sym, ev.get("edge") or "?", _clip(ev.get("reason", ""), 90))
    elif t == "gate":
        _run_log.info("  ✗ gated %-6s %s", sym, _clip(ev.get("reason", ""), 100))
    elif t == "brief":
        _run_log.info("  note   %-6s [%s] %s", sym, ev.get("kind", ""), _clip(ev.get("summary", ""), 100))
    elif t == "thesis":
        _run_log.info("  CASE   %-6s %s ~%sd · score %s — %s", sym, ev.get("direction", ""),
                      ev.get("horizon_days", ""), ev.get("score", ""), _clip(ev.get("thesis", ""), 120))
    elif t == "concern":
        _run_log.info("  vs     %-6s %s — %s", sym, _clip(ev.get("claim", ""), 90),
                      _clip(ev.get("evidence", ""), 80))
    elif t == "fact_flag":
        _run_log.info("  ⚑ flag %-6s %s", sym, _clip(ev.get("text", ""), 110))
    elif t == "counter":
        if ev.get("stance") == "FLIP":
            _run_log.info("  ⟲ CRITIC reverses %-6s %s → %s — %s", sym, ev.get("proposed_from", ""),
                          ev.get("counter_direction", ""), _clip(ev.get("counter", ""), 90))
        else:
            _run_log.info("  ⟲ CRITIC stand-aside %-6s %s", sym, _clip(ev.get("counter", ""), 90))
    elif t == "rebuttal":
        _run_log.info("  reply  %-6s revised %s · concede=%s", sym, ev.get("revised_score", ""),
                      "yes" if ev.get("concede") else "no")
    elif t == "decision":
        _run_log.info("DECIDE ▸ %-6s %s  %s  conf %s%s", sym, ev.get("direction", ""),
                      ev.get("verdict", ""), ev.get("conviction", ""),
                      "  ⟲ REVERSED" if ev.get("flipped") else "")
        if ev.get("summary"):
            _run_log.info("         %s", _clip(ev.get("summary", ""), 160))
    elif t == "chief":
        board = ev.get("board") or []
        takes = sum(1 for r in board if r.get("take"))
        _run_log.info("HEAD ▸  ranked %d, %d worth acting on", len(board), takes)
        if ev.get("summary"):
            _run_log.info("         %s", _clip(ev.get("summary", ""), 200))
        for r in board:
            if r.get("take"):
                _run_log.info("         ✓ %-6s %s — %s", r.get("symbol", ""), r.get("direction", ""),
                              _clip(r.get("chief_reason", ""), 110))
    elif t == "done":
        board = ev.get("board") or []
        _run_log.info("── run complete — %d ideas, %d worth acting on ──", len(board),
                      sum(1 for r in board if r.get("take")))


@app.get("/api/find-trades")
async def api_find_trades(request: Request, hours: float = 24.0,
                          max_debates: int = 6, expose: bool = False):
    """Server-Sent Events stream of a live 'Find Trades' run — the team
    scanning news and debating opportunities in real time. expose=true runs the
    (heavier, web-grounded) Connections desk to surface supply-chain ripples —
    off by default to conserve quota."""
    import json as _json

    from fastapi.responses import StreamingResponse

    from alphadesk.desk.stream import stream_find_trades

    async def gen():
        if not _within_daily_cap():
            from alphadesk.config import MAX_RUNS_PER_DAY
            yield f"data: {_json.dumps({'type': 'status', 'msg': f'daily run cap reached ({MAX_RUNS_PER_DAY}/day) — try again tomorrow'})}\n\n"
            yield f"data: {_json.dumps({'type': 'done', 'board': []})}\n\n"
            return
        _run_log.info("── Find Trades: scanning %.0fh%s ──", hours, " (deep)" if expose else "")
        try:
            async for event in stream_find_trades(
                hours=hours, max_debates=max_debates, expose=expose,
                is_disconnected=request.is_disconnected,
            ):
                _log_run_event(event)
                yield f"data: {_json.dumps(event)}\n\n"
        except Exception as exc:  # never leave the client hanging
            _run_log.error("run error: %s", exc)
            yield f"data: {_json.dumps({'type': 'status', 'msg': f'run error: {exc}'})}\n\n"
            yield f"data: {_json.dumps({'type': 'done', 'board': []})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# SPA — static bundle with index fallback (client handles the rest)
# ---------------------------------------------------------------------------

@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path:
        candidate = (_STATIC / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(_STATIC.resolve()):
            return FileResponse(candidate)
    index = _STATIC / "index.html"
    if not index.is_file():
        return Response(
            "UI bundle missing — run `pnpm build` in alphadesk/ui", status_code=503
        )
    return FileResponse(index)
