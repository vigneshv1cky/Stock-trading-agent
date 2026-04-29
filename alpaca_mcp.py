"""Alpaca MCP server — exposes Alpaca account/positions/orders to Claude as tools.

Run directly:
    python alpaca_mcp.py

Registered in .mcp.json so Claude Code picks it up automatically.
Uses paper or live keys based on ALPACA_PAPER env var (default: paper).
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
_API_KEY = (
    os.environ.get("ALPACA_API_KEY", "")
    if _PAPER
    else os.environ.get("ALPACA_LIVE_API_KEY", "")
)
_SECRET_KEY = (
    os.environ.get("ALPACA_SECRET_KEY", "")
    if _PAPER
    else os.environ.get("ALPACA_LIVE_SECRET_KEY", "")
)

mcp = FastMCP("Alpaca")


def _trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(_API_KEY, _SECRET_KEY, paper=_PAPER)


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(_API_KEY, _SECRET_KEY)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@mcp.tool()
def get_account() -> dict:
    """Get Alpaca account summary: portfolio value, buying power, cash, P&L, and mode."""
    client = _trading_client()
    acct = client.get_account()
    return {
        "mode": "paper" if _PAPER else "live",
        "portfolio_value": float(acct.portfolio_value),
        "buying_power": float(acct.buying_power),
        "cash": float(acct.cash),
        "equity": float(acct.equity),
        "last_equity": float(acct.last_equity),
        "day_pnl": round(float(acct.equity) - float(acct.last_equity), 2),
        "day_pnl_pct": round(
            (float(acct.equity) - float(acct.last_equity)) / float(acct.last_equity) * 100, 3
        ) if float(acct.last_equity) else 0.0,
        "pattern_day_trader": acct.pattern_day_trader,
        "trading_blocked": acct.trading_blocked,
        "account_blocked": acct.account_blocked,
    }


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@mcp.tool()
def get_positions() -> list[dict]:
    """Get all open positions with entry price, current price, and unrealized P&L."""
    client = _trading_client()
    positions = client.get_all_positions()
    result = []
    for p in positions:
        side = str(getattr(p, "side", "")).lower()
        result.append({
            "symbol": p.symbol,
            "side": "SHORT" if "short" in side else "LONG",
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": round(float(p.unrealized_pl), 2),
            "unrealized_pl_pct": round(float(p.unrealized_plpc) * 100, 2),
            "intraday_pl": round(float(p.unrealized_intraday_pl), 2),
        })
    result.sort(key=lambda x: abs(x["unrealized_pl"]), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@mcp.tool()
def get_orders(status: str = "all", limit: int = 20) -> list[dict]:
    """Get recent Alpaca orders.

    Args:
        status: "open", "closed", or "all" (default: "all")
        limit: max number of orders to return (default: 20)
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    status_map = {
        "open": QueryOrderStatus.OPEN,
        "closed": QueryOrderStatus.CLOSED,
        "all": QueryOrderStatus.ALL,
    }
    client = _trading_client()
    orders = client.get_orders(
        filter=GetOrdersRequest(
            status=status_map.get(status, QueryOrderStatus.ALL),
            limit=limit,
        )
    )
    return [
        {
            "id": str(o.id),
            "symbol": o.symbol,
            "side": str(o.side).split(".")[-1],
            "type": str(o.type).split(".")[-1],
            "qty": float(o.qty or 0),
            "filled_qty": float(o.filled_qty or 0),
            "status": str(o.status).split(".")[-1],
            "limit_price": float(o.limit_price) if o.limit_price else None,
            "stop_price": float(o.stop_price) if o.stop_price else None,
            "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
            "submitted_at": str(o.submitted_at),
            "filled_at": str(o.filled_at) if o.filled_at else None,
        }
        for o in orders
    ]


@mcp.tool()
def cancel_order(order_id: str) -> dict:
    """Cancel an open Alpaca order by ID.

    Args:
        order_id: the order UUID string (get it from get_orders)
    """
    from uuid import UUID
    client = _trading_client()
    client.cancel_order_by_id(UUID(order_id))
    return {"cancelled": order_id}


# ---------------------------------------------------------------------------
# Market clock
# ---------------------------------------------------------------------------

@mcp.tool()
def get_market_clock() -> dict:
    """Check if the US stock market is currently open and when it next opens/closes."""
    client = _trading_client()
    clock = client.get_clock()
    return {
        "is_open": clock.is_open,
        "timestamp": str(clock.timestamp),
        "next_open": str(clock.next_open),
        "next_close": str(clock.next_close),
    }


# ---------------------------------------------------------------------------
# Portfolio history
# ---------------------------------------------------------------------------

@mcp.tool()
def get_portfolio_history(period: str = "1W") -> dict:
    """Get portfolio equity curve.

    Args:
        period: "1D", "1W", "1M", "3M", or "1A" (default: "1W")
    """
    client = _trading_client()
    history = client.get_portfolio_history(period_length=period)  # type: ignore[call-arg]
    timestamps = history.timestamp or []
    equity = history.equity or []
    profit_loss = history.profit_loss or []
    return {
        "period": period,
        "base_value": float(history.base_value) if history.base_value else None,
        "points": [
            {
                "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "equity": round(float(eq), 2),
                "pnl": round(float(pl), 2),
            }
            for ts, eq, pl in zip(timestamps, equity, profit_loss)
        ],
    }


# ---------------------------------------------------------------------------
# Asset info
# ---------------------------------------------------------------------------

@mcp.tool()
def get_asset(symbol: str) -> dict:
    """Check if a symbol is tradable and shortable on Alpaca.

    Args:
        symbol: stock ticker e.g. "AAPL"
    """
    client = _trading_client()
    asset = client.get_asset(symbol.upper())
    return {
        "symbol": asset.symbol,
        "name": asset.name,
        "tradable": asset.tradable,
        "shortable": asset.shortable,
        "marginable": asset.marginable,
        "easy_to_borrow": asset.easy_to_borrow,
        "fractionable": asset.fractionable,
        "status": str(asset.status).split(".")[-1],
    }


if __name__ == "__main__":
    mcp.run()
