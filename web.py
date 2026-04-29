"""Trading Terminal — FastAPI web dashboard.

Run:  uvicorn web:app --reload --port 8000
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

load_dotenv()

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

app = FastAPI(title="Trading Terminal")

_HELD_CACHE = os.path.expanduser("~/.stock_screener/held_cache.json")
_EXEC_LOG = os.path.expanduser("~/.stock_screener/last_execution.json")
_TRADE_HISTORY = os.path.expanduser("~/.stock_screener/trade_history.json")
_MEMORY_FILE = os.path.expanduser("~/.stock_screener/agent_memory.json")

_broker = None
_macro_cache: dict = {}
_macro_cache_ts: float = 0.0
_MACRO_TTL = 30


def _get_broker():
    global _broker
    if _broker is None:
        from stock_sentiment.market.broker import PaperBroker
        _broker = PaperBroker()
    return _broker


def _load_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


@app.get("/api/portfolio")
def api_portfolio():
    try:
        b = _get_broker()
        if not b.client:
            return {"error": "broker unavailable"}
        acct = b.client.get_account()
        equity = float(acct.equity)
        last = float(acct.last_equity)
        return {
            "equity": equity,
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "day_pnl": equity - last,
            "day_pnl_pct": (equity - last) / last * 100 if last else 0.0,
            "last_equity": last,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/positions")
def api_positions():
    try:
        b = _get_broker()
        if not b.client:
            return []
        held = _load_json(_HELD_CACHE)
        result = []
        for p in b.client.get_all_positions():
            sym = p.symbol
            cache = held.get(sym, {})
            result.append({
                "symbol": sym,
                "qty": float(p.qty),
                "side": p.side.value,
                "market_value": float(p.market_value),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
                "score": cache.get("score", 50.0),
                "entered_at": cache.get("entered_at", ""),
            })
        result.sort(key=lambda x: x["unrealized_pl"], reverse=True)
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/trades")
def api_trades():
    try:
        if os.path.exists(_TRADE_HISTORY):
            with open(_TRADE_HISTORY) as f:
                history = json.load(f)
            return list(reversed(history))
    except Exception:
        pass
    return []


@app.get("/api/macro")
def api_macro():
    global _macro_cache, _macro_cache_ts
    if time.time() - _macro_cache_ts < _MACRO_TTL and _macro_cache:
        return _macro_cache
    try:
        import yfinance as yf
        spy_h = yf.Ticker("SPY").history(period="2d")
        vix_h = yf.Ticker("^VIX").history(period="1d")
        vix = float(vix_h["Close"].iloc[-1]) if len(vix_h) else 18.0
        spy_chg = (
            (float(spy_h["Close"].iloc[-1]) - float(spy_h["Close"].iloc[-2]))
            / float(spy_h["Close"].iloc[-2]) * 100
            if len(spy_h) >= 2 else 0.0
        )
        regime = (
            "PANIC" if vix > 30 else
            "RISK_OFF" if vix > 22 else
            "RISK_ON" if vix < 15 else
            "NEUTRAL"
        )
        now = datetime.now(_ET)
        mins = now.hour * 60 + now.minute
        market_open = now.weekday() < 5 and 9 * 60 + 30 <= mins < 16 * 60
        _macro_cache = {
            "vix": round(vix, 1),
            "spy_change_pct": round(spy_chg, 2),
            "regime": regime,
            "market_open": market_open,
        }
        _macro_cache_ts = time.time()
        return _macro_cache
    except Exception:
        return {"vix": 0.0, "spy_change_pct": 0.0, "regime": "UNKNOWN", "market_open": False}


@app.get("/api/memory")
def api_memory():
    data = _load_json(_MEMORY_FILE)
    global_lessons = data.get("global_lessons", data.get("lessons", []))
    return {
        "global_lessons": global_lessons[:6],
        "total": len(global_lessons),
        "trades_reviewed": data.get("trades_reviewed", 0),
        "updated_at": data.get("updated_at", ""),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open("templates/index.html") as f:
        return f.read()
