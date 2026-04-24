import os
from datetime import datetime, timezone
from typing import List, Dict
from collections import defaultdict

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from datetime import timezone as ZoneInfo # Fallback

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, OrderStatus
except ImportError:
    TradingClient = None

class PerformanceTracker:
    """Tracks and calculates portfolio performance using Alpaca data."""

    def __init__(self):
        self.api_key = os.environ.get("ALPACA_API_KEY")
        self.secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if self.api_key and self.secret_key and TradingClient:
            self.client = TradingClient(self.api_key, self.secret_key, paper=True)
        else:
            self.client = None

    def get_performance_summary(self) -> Dict:
        """Fetch real-time P/L and equity summary."""
        print("[Analytics] Fetching Portfolio Summary from Alpaca...")
        if not self.client:
            return {"error": "Alpaca client not connected"}
        try:
            account = self.client.get_account()
            positions = self.client.get_all_positions()
            unrealized_pl = sum(float(p.unrealized_pl) for p in positions)
            total_cost = sum(float(p.qty) * float(p.avg_entry_price) for p in positions)
            unrealized_pl_pct = (unrealized_pl / total_cost) * 100 if total_cost > 0 else 0
            
            print(f"[Analytics] Equity: ${float(account.equity):,.2f} | Unrealized: ${unrealized_pl:,.2f}")
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "daily_pl": float(account.equity) - float(account.last_equity),
                "daily_pl_pct": ((float(account.equity) / float(account.last_equity)) - 1) * 100 if float(account.last_equity) > 0 else 0,
                "unrealized_pl": unrealized_pl,
                "unrealized_pl_pct": unrealized_pl_pct,
                "position_count": len(positions)
            }
        except Exception as e:
            print(f"[Analytics] ERROR fetching summary: {e}")
            return {"error": str(e)}

    def get_closed_trades(self, limit: int = 100) -> List[Dict]:
        """Fetch matched Buy/Sell pairs to calculate P/L per trade."""
        print("[Analytics] Fetching Trade History to match pairs...")
        if not self.client:
            return []

        try:
            # Market Timezone
            ny_tz = ZoneInfo("America/New_York")
            
            # Use CLOSED status and filter for FILLED orders manually
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
            all_orders = self.client.get_orders(filter=req)
            
            # Ensure we only process actually filled orders with a valid fill time
            filled_orders = [o for o in all_orders if o.status == OrderStatus.FILLED and o.filled_at is not None]
            
            buys = defaultdict(list)
            history = []
            sorted_orders = sorted(filled_orders, key=lambda x: x.filled_at)

            for order in sorted_orders:
                symbol = order.symbol
                if order.side == OrderSide.BUY:
                    buys[symbol].append(order)
                elif order.side == OrderSide.SELL:
                    if buys[symbol]:
                        entry_order = buys[symbol].pop(0)
                        entry_p = float(entry_order.filled_avg_price)
                        exit_p = float(order.filled_avg_price)
                        qty = float(order.filled_qty)
                        pl_dollars = (exit_p - entry_p) * qty
                        pl_pct = ((exit_p / entry_p) - 1) * 100 if entry_p > 0 else 0
                        
                        # Localize UTC time to NY Market Time
                        entry_ny = entry_order.filled_at.astimezone(ny_tz)
                        exit_ny = order.filled_at.astimezone(ny_tz)
                        
                        history.append({
                            "symbol": symbol,
                            "entry_price": entry_p,
                            "exit_price": exit_p,
                            "qty": qty,
                            "pl_dollars": pl_dollars,
                            "pl_pct": pl_pct,
                            "entry_time": entry_ny.strftime("%Y-%m-%d %H:%M") + " NY",
                            "exit_time": exit_ny.strftime("%Y-%m-%d %H:%M") + " NY",
                            "status": "WIN" if pl_dollars >= 0 else "LOSS"
                        })
            
            print(f"[Analytics] Successfully matched {len(history)} trade pairs.")
            return sorted(history, key=lambda x: x['exit_time'], reverse=True)
        except Exception as e:
            import traceback
            print(f"[Analytics] ERROR matching trades: {e}")
            traceback.print_exc()
            return []

    def get_equity_curve(self) -> List[Dict]:
        if not self.client: return []
        try:
            print("[Analytics] Fetching 30-day Equity Curve...")
            history = self.client.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
            return [{"timestamp": datetime.fromtimestamp(history.timestamp[i], tz=timezone.utc).strftime("%Y-%m-%d"), "equity": history.equity[i]} for i in range(len(history.equity))]
        except Exception as e:
            print(f"[Analytics] ERROR fetching curve: {e}")
            return []
