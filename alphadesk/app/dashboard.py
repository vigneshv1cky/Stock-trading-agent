"""Dashboard — FastAPI serving the shadcn/ui SPA + JSON API.

Auth: HTTP Basic enforced by middleware on EVERY route (API, SPA, assets) —
fail-closed if ADMIN_USERNAME/ADMIN_PASSWORD are unset.
Frontend: built by `pnpm build` in alphadesk/ui → alphadesk/app/static/.
"""

import base64
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from alphadesk.ledger import store

_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="AlphaDesk")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    # /healthz is the ONLY unauthenticated path — the external watchdog's probe.
    # It exposes liveness only, never data.
    if request.url.path == "/healthz":
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

@app.get("/api/picks")
def api_picks(limit: int = 30):
    return {"picks": store.recent(limit)}


@app.get("/api/picks/{pick_id}")
def api_pick(pick_id: int):
    pick = store.get_pick(pick_id)
    if not pick:
        raise HTTPException(404, "no such pick")
    return pick


@app.get("/api/stats")
def api_stats():
    return store.stats()


@app.get("/api/funnel")
def api_funnel(limit: int = 30):
    from alphadesk.app import scheduler
    return {"paused": scheduler.paused(), "windows": store.funnel_recent(limit)}


@app.get("/api/tokens")
def api_tokens(days: int = 1):
    return {"days": days, "usage": store.token_summary(days)}


@app.get("/api/earnings")
def api_earnings():
    """Be-ready view: who reports next (with the time to RUN the desk to catch the
    drift) and who just reported."""
    from alphadesk.ingest.earnings import run_at
    upcoming = store.upcoming_earnings(days=7)
    for e in upcoming:
        e["run_at"] = run_at(e["report_date"], e.get("session"))
    return {"upcoming": upcoming, "reported": store.recently_reported(days=3)}


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


@app.get("/api/find-trades")
async def api_find_trades(request: Request, hours: float = 24.0,
                          max_debates: int = 6, expose: bool = False):
    """Server-Sent Events stream of a live 'Find Trades' run — the committee
    scanning news and debating opportunities in real time. expose=true runs the
    (heavier, web-grounded) Exposure Desk to surface supply-chain ripples —
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
        try:
            async for event in stream_find_trades(
                hours=hours, max_debates=max_debates, expose=expose,
                is_disconnected=request.is_disconnected,
            ):
                yield f"data: {_json.dumps(event)}\n\n"
        except Exception as exc:  # never leave the client hanging
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
