import logging
import os
import time

from rich.console import Console

console = Console()
_log = logging.getLogger("PaperBroker")

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, StopOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
except ImportError:
    TradingClient = None  # type: ignore[assignment,misc]
    StockHistoricalDataClient = None  # type: ignore[assignment,misc]


def _is_long_position(p) -> bool:
    return str(getattr(p, "side", "")).lower() in ("long", "positionside.long")


class PaperBroker:
    """Automated paper trading executor using Alpaca with Smart Conviction Swapping."""

    def __init__(self):
        self._data_client = None
        self._crypto_client = None

        _settings_file = os.path.expanduser("~/.stock_screener/settings.json")
        try:
            import json as _json
            settings = {"alpaca_paper": True, "alpaca_live_api_key": "", "alpaca_live_secret_key": ""}
            if os.path.exists(_settings_file):
                settings.update(_json.load(open(_settings_file)))
        except Exception:
            settings = {"alpaca_paper": True, "alpaca_live_api_key": "", "alpaca_live_secret_key": ""}

        env_paper = os.environ.get("ALPACA_PAPER")
        paper_mode = env_paper.lower() != "false" if env_paper is not None else settings.get("alpaca_paper", True)

        if paper_mode:
            self.api_key = os.environ.get("ALPACA_API_KEY", "")
            self.secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        else:
            self.api_key = os.environ.get("ALPACA_LIVE_API_KEY") or settings.get("alpaca_live_api_key", "")
            self.secret_key = os.environ.get("ALPACA_LIVE_SECRET_KEY") or settings.get("alpaca_live_secret_key", "")

        if not self.api_key or not self.secret_key:
            mode_label = "paper" if paper_mode else "live"
            print(f"WARNING: Alpaca {mode_label} keys missing. Trade execution disabled.")
            self.client = None
        elif TradingClient is None:
            print("WARNING: alpaca-py is not installed. Trade execution disabled.")
            self.client = None
        else:
            try:
                if not paper_mode:
                    print("WARNING: Live trading mode active. Real money at risk.")
                self.client = TradingClient(self.api_key, self.secret_key, paper=paper_mode)
            except Exception as e:
                print(f"WARNING: Failed to initialize Alpaca TradingClient: {e}. Trade execution disabled.")
                self.client = None

    # ------------------------------------------------------------------
    # VIX regime
    # ------------------------------------------------------------------

    def _get_vix(self) -> float:
        """Fetch latest VIX close via yfinance. Returns 20.0 on failure (normal regime)."""
        try:
            import yfinance as yf
            hist = yf.Ticker("^VIX").history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return 20.0

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _slot_size_for_score(
        self,
        portfolio_value: float,
        avg_sentiment: float | None = None,
        article_count: int | None = None,
    ) -> float:
        """Return dollar allocation scaled by news urgency.

        When avg_sentiment + article_count are provided: 7–12% of portfolio based on
        recency-weighted sentiment amplitude × article coverage breadth.
        Fallback (no news data): flat 9%.
        """
        if avg_sentiment is not None and article_count is not None:
            news_urgency = max(0.0, avg_sentiment) * min(1.0, article_count / 3.0)
            pct = max(0.07, min(0.12, 0.07 + news_urgency * 0.05))
        else:
            pct = 0.09
        return max(50.0, round(portfolio_value * pct, 2))

    def _can_short(self, symbol: str) -> bool:
        """Check if Alpaca allows shorting this asset (shortable AND easy_to_borrow)."""
        try:
            asset = self.client.get_asset(symbol)
            return bool(asset.shortable) and bool(asset.easy_to_borrow)
        except Exception:
            return False


    # ------------------------------------------------------------------
    # Stop management
    # ------------------------------------------------------------------

    def _place_stop_for_new_position(self, symbol: str, qty: float, is_long: bool, entry_price: float) -> bool:
        """Place a hard stop-loss immediately after entry: long –1.5%, short +0.8%.
        Retries up to 3 times (0.5s apart) to handle market-order fill race condition."""
        pct = 0.03 if is_long else 0.02
        stop_price = round(entry_price * (1 - pct) if is_long else entry_price * (1 + pct), 2)
        stop_side = OrderSide.SELL if is_long else OrderSide.BUY
        if not float(qty).is_integer():
            console.print(f"  [dim]Stop skipped {symbol}: fractional qty ({qty}) — no stop for fractional positions[/dim]")
            return True
        for attempt in range(3):
            try:
                self.client.submit_order(StopOrderRequest(
                    symbol=symbol,
                    qty=int(qty),
                    side=stop_side,
                    time_in_force=TimeInForce.GTC,
                    stop_price=stop_price,
                ))
                console.print(f"  [dim]Stop placed {symbol}: ${stop_price:.2f} ({pct * 100:.1f}% from ${entry_price:.2f})[/dim]")
                return True
            except Exception as e:
                err_str = str(e)
                # Alpaca wash-trade block: cancel the conflicting order and retry immediately
                if "40310000" in err_str or "wash trade" in err_str.lower():
                    import re as _re
                    m = _re.search(r'"existing_order_id"\s*:\s*"([^"]+)"', err_str)
                    if m:
                        try:
                            self.client.cancel_order_by_id(m.group(1))
                            time.sleep(0.3)
                        except Exception:
                            pass
                    continue
                if attempt < 2:
                    time.sleep(0.5)
                else:
                    console.print(f"  [red]✖  Stop failed {symbol} after 3 attempts: {e}[/red]")
        return False

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _close_position_safely(self, symbol: str):
        """Cancel open orders for the symbol to unlock shares, then close at market.
        Works for both long (market sell) and short (market buy to cover) positions."""
        try:
            open_orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
            if open_orders:
                for order in open_orders:
                    try:
                        self.client.cancel_order_by_id(order_id=order.id)
                    except Exception:
                        pass
                time.sleep(1)  # let Alpaca process cancellations before closing
            self.client.close_position(symbol_or_asset_id=symbol)
        except Exception as e:
            console.print(f"  [red]✖  Error closing {symbol}: {e}[/red]")
            raise

    def _get_live_price(self, symbol: str, fallback: float) -> float:
        """Fetch the latest trade price from Alpaca; fall back to screener price on error."""
        if "/" in symbol:
            return self._get_crypto_live_price(symbol, fallback)
        try:
            if StockHistoricalDataClient is None:
                return fallback
            if self._data_client is None:
                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
            trade = self._data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
            return float(trade[symbol].price)
        except Exception:
            return fallback

    def _get_crypto_live_price(self, symbol: str, fallback: float) -> float:
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import CryptoLatestTradeRequest  # type: ignore[import-untyped]
            if self._crypto_client is None:
                self._crypto_client = CryptoHistoricalDataClient()
            trade = self._crypto_client.get_crypto_latest_trade(
                CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            )
            return float(trade[symbol].price)
        except Exception:
            return fallback

    def _place_market_buy(
        self,
        symbol: str,
        current_price: float,
        portfolio_value: float,
        avg_sentiment: float | None = None,
        article_count: int | None = None,
    ) -> tuple[float, float]:
        """Submit a market buy. Returns (price, qty) on success.
        Crypto uses notional ordering (fractional); stocks use whole shares."""
        is_crypto = "/" in symbol
        live_price = self._get_live_price(symbol, current_price)
        price_source = "live" if live_price != current_price else "screener"

        if live_price <= 0:
            console.print(f"  [dim]Skipping {symbol}: invalid price[/dim]")
            return 0.0, 0.0

        slot = self._slot_size_for_score(portfolio_value, avg_sentiment, article_count)

        if is_crypto:
            approx_qty = slot / live_price
            console.print(
                f"  [green]✔  BUY {symbol}[/green]"
                f"  [dim]notional=[/dim][bold]${slot:.0f}[/bold]"
                f"  [dim]≈[/dim][bold]{approx_qty:.6f}[/bold]"
                f"  [dim]@[/dim] ${live_price:.2f} [dim]({price_source})[/dim]"
            )
            try:
                self.client.submit_order(MarketOrderRequest(
                    symbol=symbol,
                    notional=round(slot, 2),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                ))
                # Stop placed by the 5-min stop audit; notional fills don't give a fixed qty upfront
                return live_price, approx_qty
            except Exception as e:
                console.print(f"  [red]✖  Crypto buy failed for {symbol}: {e}[/red]")
                return 0.0, 0.0

        qty = int(slot / live_price)  # whole shares only

        if qty < 1:
            console.print(f"  [dim]Skipping {symbol}: slot ${slot:.0f} < price ${live_price:.2f}[/dim]")
            return 0.0, 0.0

        _log.info("Submitting BUY %s  qty=%d  @$%.2f  slot=$%.0f", symbol, qty, live_price, slot)
        try:
            self.client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            console.print(
                f"  [green]✔  BUY {symbol}[/green]"
                f"  [dim]slot=[/dim][bold]${slot:.0f}[/bold]"
                f"  [dim]qty=[/dim][bold]{qty}[/bold]"
                f"  [dim]@[/dim] ${live_price:.2f} [dim]({price_source})[/dim]"
            )
            placed = self._place_stop_for_new_position(symbol, qty, is_long=True, entry_price=live_price)
            if not placed:
                console.print(f"  [yellow]⚠  {symbol} entered WITHOUT stop — audit will retry next cycle[/yellow]")
            return live_price, qty
        except Exception as e:
            _log.error("BUY order REJECTED for %s: %s", symbol, e)
            console.print(f"  [red]✖  Market buy failed for {symbol}: {e}[/red]")
            return 0.0, 0.0

    def _place_market_short(
        self,
        symbol: str,
        current_price: float,
        portfolio_value: float,
        avg_sentiment: float | None = None,
        article_count: int | None = None,
    ) -> tuple[float, float]:
        """Submit a market sell to open a short position (fractional shares).
        Returns (price, qty) on success, (0.0, 0.0) on skip or error."""
        if "/" in symbol:
            console.print(f"  [dim]Short skipped {symbol}: crypto assets are LONG only[/dim]")
            return 0.0, 0.0

        live_price = self._get_live_price(symbol, current_price)
        price_source = "live" if live_price != current_price else "screener"

        if live_price <= 0:
            console.print(f"  [dim]Skipping short {symbol}: invalid price[/dim]")
            return 0.0, 0.0

        slot = self._slot_size_for_score(portfolio_value, avg_sentiment, article_count)
        qty = int(slot / live_price)  # whole shares only — Alpaca rejects fractional shorts

        if qty < 1:
            console.print(f"  [dim]Skipping short {symbol}: slot ${slot:.0f} < price ${live_price:.2f}[/dim]")
            return 0.0, 0.0

        _log.info("Submitting SHORT %s  qty=%d  @$%.2f  slot=$%.0f", symbol, qty, live_price, slot)
        try:
            self.client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            console.print(
                f"  [red]✔  SHORT {symbol}[/red]"
                f"  [dim]slot=[/dim][bold]${slot:.0f}[/bold]"
                f"  [dim]qty=[/dim][bold]{qty}[/bold]"
                f"  [dim]@[/dim] ${live_price:.2f} [dim]({price_source})[/dim]"
            )
            time.sleep(2)  # wait for short fill before placing BUY stop (prevents wash-trade rejection)
            placed = self._place_stop_for_new_position(symbol, qty, is_long=False, entry_price=live_price)
            if not placed:
                console.print(f"  [yellow]⚠  {symbol} shorted WITHOUT stop — audit will retry next cycle[/yellow]")
            return live_price, qty
        except Exception as e:
            _log.error("SHORT order REJECTED for %s: %s", symbol, e)
            console.print(f"  [red]✖  Market short failed for {symbol}: {e}[/red]")
            return 0.0, 0.0
