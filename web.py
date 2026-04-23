from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from stock_sentiment.screener_app import ScreenerApp
from stock_sentiment.cloud_output import generate_html_report
from stock_sentiment.history import History
from stock_sentiment.market.broker import PaperBroker
from stock_sentiment.scheduler import Scheduler
import threading

app = FastAPI(title="Stock Screener Web App")

# Mount static files and initialize templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Background thread to run the trading bot scheduler
def run_bot_in_background():
    print("[Web] Starting background trading bot...")
    history = History()
    try:
        # These settings match your default 'run.py --schedule' behavior
        scheduler = Scheduler(min_return=10.0, top_n=30, interval_hours=0.5)
        scheduler.run()
    except Exception as e:
        print(f"[Web] CRITICAL: Background bot failed: {e}")
        try:
            history.save_heartbeat("Error", f"Bot thread crashed: {str(e)}")
        except:
            pass
    finally:
        history.close()

@app.on_event("startup")
async def startup_event():
    # Start the bot in its own thread so it doesn't block FastAPI
    bot_thread = threading.Thread(target=run_bot_in_background, daemon=True)
    bot_thread.start()

# Create a thread pool to run the screener
executor = ThreadPoolExecutor(max_workers=2)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/health")
def health_check():
    return {"status": "ok"}

class LogRequest(BaseModel):
    level: str = "INFO"
    message: str

@app.post("/api/log")
async def client_log(req: LogRequest):
    print(f"[Client {req.level}] {req.message}")
    return {"status": "ok"}

class ScreenRequest(BaseModel):
    min_return: float = 10.0
    top_n: int = 30

@app.post("/api/screen", response_class=HTMLResponse)
async def screen_stocks(req: ScreenRequest):
    def _run_screener():
        screener_app = ScreenerApp(min_return=req.min_return, top_n=req.top_n)
        try:
            predictions, count, alerts = screener_app.run(cloud_mode=False, trigger="MANUAL")
            return generate_html_report(predictions, count, fragment=True)
        except Exception as e:
            print(f"[Web] Manual screen error: {e}")
            return f"<p style='color: red'>Error: {str(e)}</p>"

    loop = asyncio.get_running_loop()
    html_report = await loop.run_in_executor(executor, _run_screener)
    return html_report

@app.post("/api/force-trade", response_class=HTMLResponse)
async def force_trade(req: ScreenRequest):
    def _run_force_trade():
        # Instantiate a new Scheduler with the requested parameters
        scheduler = Scheduler(min_return=req.min_return, top_n=req.top_n)
        try:
            # Execute one cycle
            predictions, count, alerts = scheduler.execute_cycle(trigger="FORCE_TRADE")
            # Return a fragment of the HTML report
            return generate_html_report(predictions, count, fragment=True)
        except Exception as e:
            print(f"[Web] Force trade error: {e}")
            return f"<p style='color: red'>Error: {str(e)}</p>"
        finally:
            scheduler.history.close()

    loop = asyncio.get_running_loop()
    html_fragment = await loop.run_in_executor(executor, _run_force_trade)
    return html_fragment

@app.get("/api/performance", response_class=JSONResponse)
def get_performance():
    print("[Web] Fetching performance data...")
    history = History()
    
    try:
        # 1. Fetch Backtest Stats & Latest Run from Local History
        print("[Web] Reading history...")
        try:
            backtest_stats = history.get_backtest_stats()
            if backtest_stats and "accuracy" in backtest_stats and backtest_stats["accuracy"] is not None:
                accuracy_pct = backtest_stats["accuracy"] * 100
            else:
                accuracy_pct = None
            total_return = backtest_stats.get("avg_return_10d")
        except Exception as e:
            print(f"[Web] Error fetching backtest stats: {e}")
            accuracy_pct = None
            total_return = None

        try:
            latest_run = history.get_latest_run()
            if latest_run:
                picks = history.get_predictions_for_run(latest_run["id"])
                picks = picks[:5]
                last_run_at = latest_run["run_at"]
                trigger_type = latest_run.get("trigger_type", "UNKNOWN")
            else:
                picks = []
                last_run_at = None
                trigger_type = None
        except Exception as e:
            print(f"[Web] Error fetching latest run: {e}")
            latest_run = None
            picks = []
            last_run_at = None
            trigger_type = None

        heartbeat = None
        try:
            heartbeat = history.get_heartbeat()
        except Exception as e:
            print(f"[Web] Error fetching heartbeat: {e}")

        # 2. Fetch Alpaca data
        print("[Web] Checking Alpaca...")
        broker = PaperBroker()
        alpaca_data = {"equity": None, "buying_power": None, "positions": [], "error": None}
        
        if broker.client:
            def fetch_alpaca():
                return broker.client.get_account(), broker.client.get_all_positions()
                
            try:
                future = executor.submit(fetch_alpaca)
                account, positions = future.result(timeout=5.0)
                
                alpaca_data["equity"] = float(account.equity)
                alpaca_data["buying_power"] = float(account.buying_power)
                
                alpaca_data["positions"] = [
                    {
                        "symbol": p.symbol,
                        "qty": float(p.qty),
                        "avg_entry_price": float(p.avg_entry_price),
                        "unrealized_plpc": float(p.unrealized_plpc)
                    } for p in positions
                ]
            except TimeoutError:
                print("[Web] Alpaca request timed out.")
                alpaca_data["error"] = "Alpaca request timed out."
            except Exception as e:
                print(f"[Web] Alpaca error: {e}")
                alpaca_data["error"] = f"Alpaca error: {str(e)}"
        else:
            alpaca_data["error"] = "Alpaca integration disabled or keys missing."

        print("[Web] Performance data ready.")
        return {
            "backtest": {
                "accuracy": accuracy_pct,
                "total_return": total_return
            },
            "latest_run": {
                "picks": picks,
                "last_run_at": last_run_at,
                "trigger": trigger_type
            },
            "bot_status": heartbeat,
            "alpaca": alpaca_data
        }
    finally:
        history.close()
