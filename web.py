from fastapi import FastAPI, BackgroundTasks, Request, Depends, HTTPException, status, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import os
import threading
import traceback

# Load environment variables from .env file
load_dotenv()

# Admin credentials
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

from stock_sentiment.screener_app import ScreenerApp
from stock_sentiment.cloud_output import generate_html_report
from stock_sentiment.history import History
from stock_sentiment.market.broker import PaperBroker
from stock_sentiment.market.performance import PerformanceTracker
from stock_sentiment.scheduler import Scheduler

app = FastAPI(title="Bot Command Center")

# Mount static files and initialize templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Background thread to run the trading bot scheduler
def run_bot_in_background():
    print("[Web] Starting background trading bot...")
    history = History()
    try:
        import time; time.sleep(5)
        print(f"[Web] Initializing Autonomous Scheduler (Triple-Door Logic)")
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

# --- Authentication Logic ---
def check_auth(request: Request):
    # Allow bypass if ADMIN_PASSWORD is not configured and defaults to 'changeme'
    if ADMIN_PASSWORD == "changeme":
        return
    if request.cookies.get("auth_token") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(key="auth_token", value=password, httponly=True)
        return response
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid credentials"})

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("auth_token")
    return response

# --- Protected Routes ---

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if ADMIN_PASSWORD != "changeme" and request.cookies.get("auth_token") != ADMIN_PASSWORD:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/health")
def health_check():
    return {"status": "ok"}

class LogRequest(BaseModel):
    level: str = "INFO"
    message: str

@app.post("/api/log", dependencies=[Depends(check_auth)])
async def client_log(req: LogRequest):
    print(f"[Client {req.level}] {req.message}")
    return {"status": "ok"}

@app.post("/api/screen", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def screen_stocks():
    def _run_screener():
        screener_app = ScreenerApp(top_n=40)
        try:
            predictions, count, alerts = screener_app.run(cloud_mode=False, trigger="MANUAL")
            return generate_html_report(predictions, count, fragment=True)
        except Exception as e:
            print(f"[Web] Manual screen error: {e}")
            traceback.print_exc()
            return f"<p style='color: red'>Error: {str(e)}</p>"
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _run_screener)

@app.post("/api/force-trade", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def force_trade():
    def _run_force_trade():
        scheduler = Scheduler(top_n=40)
        try:
            predictions, count, alerts = scheduler.execute_cycle(trigger="FORCE_EXEC")
            return generate_html_report(predictions, count, fragment=True)
        except Exception as e:
            print(f"[Web] Force trade error: {e}")
            traceback.print_exc()
            return f"<p style='color: red'>Error: {str(e)}</p>"
        finally:
            scheduler.history.close()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _run_force_trade)

@app.get("/api/history", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_trade_history():
    tracker = PerformanceTracker()
    history = tracker.get_closed_trades(limit=500)
    return {"history": history}

@app.get("/api/performance", response_class=JSONResponse, dependencies=[Depends(check_auth)])
def get_performance():
    history = History()
    tracker = PerformanceTracker()
    try:
        backtest_stats = history.get_backtest_stats()
        latest_run = history.get_latest_run()
        heartbeat = history.get_heartbeat()
        perf = tracker.get_performance_summary()
        positions = []
        if tracker.client:
            p_list = tracker.client.get_all_positions()
            positions = [
                {
                    "symbol": p.symbol, "qty": float(p.qty), "avg_entry_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price), "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc) * 100
                } for p in p_list
            ]
        return {
            "summary": perf, "positions": positions, "backtest": backtest_stats,
            "latest_run": { 
                "id": latest_run["id"] if latest_run else None, 
                "at": latest_run["run_at"] if latest_run else None, 
                "trigger": latest_run.get("trigger_type", "AUTO") if latest_run else "AUTO" 
            },
            "bot_status": {
                "status": heartbeat.get("status") if heartbeat else "Idle",
                "message": heartbeat.get("message") if heartbeat else "No recent activity",
                "last_ping": heartbeat.get("last_updated") if heartbeat else None
            }
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
