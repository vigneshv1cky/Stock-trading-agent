import asyncio
import os
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

from stock_sentiment.cloud_output import generate_html_report  # noqa: E402
from stock_sentiment.config import load_settings, save_settings  # noqa: E402
from stock_sentiment.history import History  # noqa: E402
from stock_sentiment.market.performance import PerformanceTracker  # noqa: E402
from stock_sentiment.scheduler import Scheduler  # noqa: E402
from stock_sentiment.screener_app import ScreenerApp  # noqa: E402

app = FastAPI(title="Sentinel")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Background bot thread
# ---------------------------------------------------------------------------

def run_bot_in_background():
    print("[Web] Starting background trading bot...")
    history = History()
    try:
        time.sleep(5)
        print("[Web] Initializing Autonomous Scheduler (Triple-Door Logic)")
        scheduler = Scheduler(top_n=40, interval_hours=0.5)
        scheduler.run()
    except Exception as e:
        print(f"[Web] CRITICAL: Background bot failed: {e}")
        traceback.print_exc()
        try:
            history.save_heartbeat("Error", f"Bot thread crashed: {str(e)}")
        except Exception as inner_e:
            print(f"[Web] Failed to save error heartbeat: {inner_e}")
    finally:
        history.close()


@app.on_event("startup")
async def startup_event():
    bot_thread = threading.Thread(target=run_bot_in_background, daemon=True)
    bot_thread.start()


executor = ThreadPoolExecutor(max_workers=2)
_trade_lock = threading.Lock()

# Random token generated on login — never the password itself
_session_token: str | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_auth(request: Request):
    if ADMIN_PASSWORD == "changeme":
        return
    if _session_token is None or request.cookies.get("auth_token") != _session_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    global _session_token
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        _session_token = secrets.token_urlsafe(32)
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(key="auth_token", value=_session_token, httponly=True)
        return response
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": "Invalid credentials"}
    )


@app.get("/logout")
def logout():
    global _session_token
    _session_token = None
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("auth_token")
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if ADMIN_PASSWORD != "changeme":
        if _session_token is None or request.cookies.get("auth_token") != _session_token:
            return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/health")
def health_check():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API — misc
# ---------------------------------------------------------------------------

class LogRequest(BaseModel):
    level: str = "INFO"
    message: str


@app.post("/api/log", dependencies=[Depends(check_auth)])
async def client_log(req: LogRequest):
    print(f"[Client {req.level}] {req.message}")
    return {"status": "ok"}


@app.post("/api/screen", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def screen_stocks():
    def _run():
        screener_app = ScreenerApp(top_n=40)
        try:
            predictions, count, _ = screener_app.run(trigger="MANUAL", execute_trades=False)
            return generate_html_report(predictions, count, fragment=True)
        except Exception as e:
            print(f"[Web] Manual screen error: {e}")
            traceback.print_exc()
            return f"<p style='color: red'>Error: {str(e)}</p>"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _run)


@app.post("/api/force-trade", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def force_trade():
    def _run():
        if not _trade_lock.acquire(blocking=False):
            return "<p style='color: orange'>Trade cycle already running — try again in a moment.</p>"
        scheduler = Scheduler(top_n=40)
        try:
            predictions, count, _ = scheduler.execute_cycle(trigger="FORCE_EXEC")
            return generate_html_report(predictions, count, fragment=True)
        except Exception as e:
            print(f"[Web] Force trade error: {e}")
            traceback.print_exc()
            return f"<p style='color: red'>Error: {str(e)}</p>"
        finally:
            scheduler.history.close()
            _trade_lock.release()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _run)


@app.get("/api/last-execution", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_last_execution():
    import json as _json
    path = os.path.expanduser("~/.stock_screener/last_execution.json")
    try:
        with open(path) as f:
            return _json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/history", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_trade_history():
    tracker = PerformanceTracker()
    return {"history": tracker.get_closed_trades(limit=500)}


@app.get("/api/performance", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_performance():
    history = History()
    tracker = PerformanceTracker()
    try:
        backtest_stats = history.get_backtest_stats()
        latest_run = history.get_latest_run(exclude_triggers=["MANUAL"])
        heartbeat = history.get_heartbeat()
        perf = tracker.get_performance_summary()
        positions = []
        if tracker.client:
            positions = [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc) * 100,
                }
                for p in tracker.client.get_all_positions()
            ]
        return {
            "summary": perf,
            "positions": positions,
            "backtest": backtest_stats,
            "latest_run": {
                "id": latest_run["id"] if latest_run else None,
                "at": latest_run["run_at"] if latest_run else None,
                "trigger": latest_run.get("trigger_type", "AUTO") if latest_run else "AUTO",
            },
            "bot_status": {
                "status": heartbeat.get("status") if heartbeat else "Idle",
                "message": heartbeat.get("message") if heartbeat else "No recent activity",
                "last_ping": heartbeat.get("last_updated") if heartbeat else None,
            },
        }
    except Exception as e:
        print(f"[Web] Performance fetch error: {e}")
        traceback.print_exc()
        return {"error": str(e)}
    finally:
        history.close()


@app.get("/api/equity-curve", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_equity_curve():
    tracker = PerformanceTracker()
    return {"points": tracker.get_equity_curve()}


# ---------------------------------------------------------------------------
# API — settings
# ---------------------------------------------------------------------------

@app.get("/api/settings", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_settings():
    settings = load_settings()
    live_key = settings.get("alpaca_live_api_key", "")
    return {
        "news_provider": settings.get("news_provider", "rss"),
        "alpaca_news_enabled": settings.get("alpaca_news_enabled", False),
        "alpaca_paper": settings.get("alpaca_paper", True),
        "alpaca_live_key_set": bool(live_key),
        "alpaca_live_key_hint": f"····{live_key[-4:]}" if len(live_key) >= 4 else "",
    }


class SettingsUpdate(BaseModel):
    news_provider: str | None = None
    alpaca_news_enabled: bool | None = None
    alpaca_paper: bool | None = None
    alpaca_live_api_key: str | None = None
    alpaca_live_secret_key: str | None = None


@app.post("/api/settings", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def update_settings(body: SettingsUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"ok": False, "error": "No fields provided"}
    save_settings(updates)
    print(f"[Web] Settings updated: {list(updates.keys())}")
    return {"ok": True}
