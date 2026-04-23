import os

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
except ImportError:
    TradingClient = None

class PaperBroker:
    """Automated paper trading executor using Alpaca."""

    def __init__(self):
        self.api_key = os.environ.get("ALPACA_API_KEY")
        self.secret_key = os.environ.get("ALPACA_SECRET_KEY")
        
        if not self.api_key or not self.secret_key:
            print("WARNING: ALPACA_API_KEY and/or ALPACA_SECRET_KEY missing from environment. Paper trading disabled.")
            self.client = None
        elif TradingClient is None:
            print("WARNING: alpaca-py is not installed. Paper trading disabled.")
            self.client = None
        else:
            try:
                self.client = TradingClient(self.api_key, self.secret_key, paper=True)
            except Exception as e:
                print(f"WARNING: Failed to initialize Alpaca TradingClient: {e}. Paper trading disabled.")
                self.client = None

    def execute_trades(self, predictions):
        """Execute trades based on predictions."""
        if not self.client:
            print("[PaperBroker] Alpaca client not initialized. Skipping trades.")
            return

        print("\n[cyan]Executing paper trades via Alpaca...[/cyan]")
        print("[PaperBroker] Fetching current positions from Alpaca...")
        
        try:
            positions = self.client.get_all_positions()
            held_symbols = {p.symbol for p in positions}
            print(f"[PaperBroker] Currently holding {len(held_symbols)} symbols: {', '.join(held_symbols)}")
        except Exception as e:
            print(f"[PaperBroker] ERROR fetching positions: {e}")
            return

        print(f"[PaperBroker] Processing {len(predictions)} predictions for potential trades...")
        for pred in predictions:
            symbol = pred.symbol
            try:
                if pred.prediction == "BULLISH" and pred.overall_score >= 75:
                    if symbol not in held_symbols:
                        if len(held_symbols) >= 10:
                            print(f"[PaperBroker] Skipping BUY for {symbol} (Portfolio full: 10 positions limit)")
                            print(f"  Skipping BUY for {symbol} (Portfolio full: max 10 positions / $10,000 limit)")
                            continue
                        
                        print(f"[PaperBroker] Submitting BUY order for {symbol} (Score: {pred.overall_score:.1f})")
                        print(f"  Submitting MARKET BUY order for {symbol} (Bullish, Score: {pred.overall_score:.1f}, $1000)")
                        from alpaca.trading.requests import MarketOrderRequest
                        order_data = MarketOrderRequest(
                            symbol=symbol,
                            notional=1000.00,
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY
                        )
                        # We use DAY because Alpaca requires DAY for fractional market orders
                        self.client.submit_order(order_data=order_data)
                        held_symbols.add(symbol)
                elif pred.prediction == "BEARISH":
                    if symbol in held_symbols:
                        print(f"[PaperBroker] Submitting SELL order for {symbol} (Bearish rating)")
                        print(f"  Submitting SELL order for {symbol} (Bearish)")
                        self.client.close_position(symbol_or_asset_id=symbol)
                        held_symbols.remove(symbol)
            except Exception as e:
                print(f"[PaperBroker] ERROR executing trade for {symbol}: {e}")
                print(f"  Error executing trade for {symbol}: {e}")
        
        print("[PaperBroker] Trade execution cycle complete.")
