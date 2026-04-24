import os
import math

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest, GetOrdersRequest, CancelOrderResponse
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
except ImportError:
    TradingClient = None

class PaperBroker:
    """Automated paper trading executor using Alpaca with Smart Conviction Swapping (Aggressive)."""

    def __init__(self):
        self.api_key = os.environ.get("ALPACA_API_KEY")
        self.secret_key = os.environ.get("ALPACA_SECRET_KEY")
        self.trail_percent = 3.0 # Set to 3% to reduce whipsaws
        self.max_positions = 10
        self.slot_size = 1000.00
        self.buy_threshold = 60.0 # Aggressive threshold
        
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
        """Execute trades with aggressive threshold and smart portfolio swapping."""
        if not self.client:
            print("[PaperBroker] Alpaca client not initialized. Skipping trades.")
            return

        print("\n[cyan]Executing managed trades via Alpaca (Aggressive Mode)...[/cyan]")
        
        try:
            # 0. Cancel any stale pending BUY orders from previous cycles
            print("[PaperBroker] Clearing stale pending BUY orders...")
            open_buy_orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY))
            for order in open_buy_orders:
                print(f"[PaperBroker] Canceling outdated BUY order for {order.symbol}")
                self.client.cancel_order_by_id(order_id=order.id)

            # 1. Fetch live account state
            account = self.client.get_account()
            cash = float(account.cash)
            positions = self.client.get_all_positions()
            held_symbols = {p.symbol for p in positions}
            
            print(f"[PaperBroker] Account Cash: ${cash:,.2f} | Positions: {len(held_symbols)}/{self.max_positions}")

            # 2. Safety Audit: Ensure Trailing Stops are active for all holdings
            self._ensure_trailing_stops(positions)

            # 3. Handle Mandatory Sells (AI Downgrades to BEARISH)
            pred_map = {p.symbol: p for p in predictions}
            for p in positions:
                symbol = p.symbol
                if symbol in pred_map and pred_map[symbol].prediction == "BEARISH":
                    print(f"[PaperBroker] SELLING {symbol}: AI downgraded rating to BEARISH.")
                    self._close_position_safely(symbol)
                    held_symbols.remove(symbol)
                    cash += self.slot_size # Approximate increase for next steps

            # 4. Process New Buy Opportunities
            # Only consider high-conviction picks (Score >= 60) not already owned
            buy_candidates = [p for p in predictions if p.prediction == "BULLISH" and p.overall_score >= self.buy_threshold and p.symbol not in held_symbols]
            buy_candidates.sort(key=lambda x: x.overall_score, reverse=True)

            for new_pick in buy_candidates:
                has_cash = cash >= self.slot_size
                has_slot = len(held_symbols) < self.max_positions

                # CASE 1: Standard Entry (Free slot AND enough cash)
                if has_cash and has_slot:
                    self._place_market_buy(new_pick.symbol, new_pick.overall_score, new_pick.current_price)
                    held_symbols.add(new_pick.symbol)
                    cash -= self.slot_size

                # CASE 2: Upgrade Entry (No cash OR no slot - Swap required)
                elif len(held_symbols) > 0:
                    # Find our weakest current holding based on the new scan
                    held_scores = []
                    for s in held_symbols:
                        # Use current scan score if available, otherwise assume 0 (unknown/stale)
                        score = pred_map[s].overall_score if s in pred_map else 0
                        held_scores.append((s, score))
                    
                    held_scores.sort(key=lambda x: x[1]) # Lowest score first (the weakest link)
                    weakest_symbol, weakest_score = held_scores[0]

                    # Swap if new pick is significantly better (+5 points) than our worst holding
                    if new_pick.overall_score > (weakest_score + 5):
                        reason = "FULL SLOTS" if not has_slot else "LOW CASH"
                        print(f"[PaperBroker] SMART SWAP ({reason}): Replacing {weakest_symbol} ({weakest_score:.1f}) with {new_pick.symbol} ({new_pick.overall_score:.1f})")
                        try:
                            # Sell the weakest link safely to free up both a slot and cash
                            self._close_position_safely(weakest_symbol)
                            held_symbols.remove(weakest_symbol)
                            # Buy the high-conviction pick
                            self._place_market_buy(new_pick.symbol, new_pick.overall_score, new_pick.current_price)
                            held_symbols.add(new_pick.symbol)
                            # Sale funded the buy, so no need to adjust 'cash' for the next candidate
                        except Exception as e:
                            print(f"[PaperBroker] Swap failed for {new_pick.symbol}: {e}")
                
                else:
                    print(f"[PaperBroker] Skipping {new_pick.symbol}: No cash and no positions to swap.")

        except Exception as e:
            print(f"[PaperBroker] CRITICAL: Trade cycle failed: {e}")
        
        print("[PaperBroker] Trade execution cycle complete.")

    def _ensure_trailing_stops(self, positions):
        """Audit positions to ensure every one has an active trailing stop order."""
        try:
            orders_req = GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.SELL)
            open_orders = self.client.get_orders(filter=orders_req)
            protected_symbols = {o.symbol for o in open_orders if o.order_type == "trailing_stop"}
            
            for p in positions:
                # Alpaca will reject trailing stops on fractional shares. We assume new trades are whole shares.
                # But to be safe, we check if the qty is a whole number.
                qty = float(p.qty)
                if not qty.is_integer():
                    print(f"[PaperBroker] Safety Audit skipped for {p.symbol}: Cannot place trailing stop on fractional shares ({qty}).")
                    continue

                if p.symbol not in protected_symbols:
                    print(f"[PaperBroker] Safety Audit: Adding {self.trail_percent}% Trailing Stop to {p.symbol}")
                    trail_req = TrailingStopOrderRequest(
                        symbol=p.symbol,
                        qty=p.qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC, 
                        trail_percent=self.trail_percent
                    )
                    self.client.submit_order(trail_req)
        except Exception as e:
            print(f"[PaperBroker] Safety Audit failed: {e}")

    def _close_position_safely(self, symbol: str):
        """Cancel open orders (like stops) to unlock shares, then close position."""
        try:
            print(f"[PaperBroker] Unlocking {symbol}... (Canceling open orders)")
            
            # Explicitly cancel open orders for this symbol first
            orders_req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            open_orders = self.client.get_orders(filter=orders_req)
            for order in open_orders:
                self.client.cancel_order_by_id(order_id=order.id)
                
            print(f"[PaperBroker] Liquidating {symbol} at market price.")
            self.client.close_position(symbol_or_asset_id=symbol)
        except Exception as e:
            print(f"[PaperBroker] Error closing {symbol}: {e}")
            raise e

    def _place_market_buy(self, symbol: str, score: float, current_price: float):
        """Submit a market buy order via Alpaca using WHOLE SHARES to allow Trailing Stops."""
        if current_price <= 0:
            print(f"[PaperBroker] Skipping {symbol}: Invalid current price.")
            return

        # Calculate whole shares
        shares_to_buy = math.floor(self.slot_size / current_price)
        
        if shares_to_buy <= 0:
            print(f"[PaperBroker] Skipping {symbol}: Price (${current_price:.2f}) is higher than slot size (${self.slot_size:.2f}).")
            return

        actual_cost = shares_to_buy * current_price
        print(f"[PaperBroker] Submitting BUY order for {symbol} (Score: {score:.1f}, Qty: {shares_to_buy}, Est. Cost: ${actual_cost:.2f})")
        
        try:
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=shares_to_buy, # Using whole shares instead of fractional notional
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC # Standard GTC for whole shares
            )
            self.client.submit_order(order_data=order_data)
        except Exception as e:
            print(f"[PaperBroker] Market Buy failed for {symbol}: {e}")
