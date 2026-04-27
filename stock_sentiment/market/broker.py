import json
import math
import os
import re
from datetime import datetime, timezone

from rich.console import Console

console = Console()

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, TrailingStopOrderRequest, StopOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
except ImportError:
    TradingClient = None  # type: ignore[assignment,misc]
    StockHistoricalDataClient = None  # type: ignore[assignment,misc]

_COOLDOWN_FILE = os.path.expanduser("~/.stock_screener/cooldowns.json")
_HELD_CACHE_FILE = os.path.expanduser("~/.stock_screener/held_cache.json")
_EXECUTION_LOG_FILE = os.path.expanduser("~/.stock_screener/last_execution.json")
_COOLDOWN_HOURS = 4


class PaperBroker:
    """Automated paper trading executor using Alpaca with Smart Conviction Swapping."""

    def __init__(self):
        self.trail_percent = 3.0
        self.max_positions = 10
        self.buy_threshold = 60.0
        self._data_client = None

        from stock_sentiment.config import load_settings
        settings = load_settings()

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
    # Sizing
    # ------------------------------------------------------------------

    def _slot_size_for_score(self, score: float, portfolio_value: float, size_multiplier: float = 1.0) -> float:
        """Scale position size as % of portfolio equity, adjusted by regime size multiplier.
        Tiers capped so 10 max positions × 9.5% peak = 95% max deployment (no implicit leverage).
        Base tiers: 90+=9.5%, 80-89=7.5%, 70-79=6%, 60-69=5%. Floor of $50 for tiny accounts."""
        if score >= 90:
            pct = 0.095
        elif score >= 80:
            pct = 0.075
        elif score >= 70:
            pct = 0.060
        else:
            pct = 0.050
        return max(50.0, round(portfolio_value * pct * size_multiplier, 2))

    # ------------------------------------------------------------------
    # Profit-tier stop tightening
    # ------------------------------------------------------------------

    def _desired_trail_pct(self, position) -> float:
        """Return a tighter trailing stop % for positions with large unrealized gains.
        Alpaca's unrealized_plpc is a decimal (e.g. 0.20 = 20%)."""
        try:
            gain_pct = float(position.unrealized_plpc) * 100
        except (AttributeError, ValueError, TypeError):
            return self.trail_percent
        if gain_pct >= 30:
            return 0.8   # lock in most of the gain
        if gain_pct >= 15:
            return 1.5   # meaningful profit secured
        return self.trail_percent

    # ------------------------------------------------------------------
    # Cooldown persistence
    # ------------------------------------------------------------------

    def _load_cooldowns(self) -> dict:
        try:
            if os.path.exists(_COOLDOWN_FILE):
                with open(_COOLDOWN_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_cooldowns(self, cooldowns: dict) -> None:
        os.makedirs(os.path.dirname(_COOLDOWN_FILE), exist_ok=True)
        now = datetime.now(timezone.utc)
        active = {
            sym: ts for sym, ts in cooldowns.items()
            if (now - datetime.fromisoformat(ts)).total_seconds() < _COOLDOWN_HOURS * 3600
        }
        with open(_COOLDOWN_FILE, "w") as f:
            json.dump(active, f, indent=2)

    def _in_cooldown(self, symbol: str, cooldowns: dict) -> bool:
        ts = cooldowns.get(symbol)
        if not ts:
            return False
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return elapsed < _COOLDOWN_HOURS * 3600

    def _load_held_cache(self) -> set:
        try:
            if os.path.exists(_HELD_CACHE_FILE):
                with open(_HELD_CACHE_FILE) as f:
                    return set(json.load(f))
        except Exception:
            pass
        return set()

    def _save_held_cache(self, symbols: set) -> None:
        os.makedirs(os.path.dirname(_HELD_CACHE_FILE), exist_ok=True)
        with open(_HELD_CACHE_FILE, "w") as f:
            json.dump(list(symbols), f)

    # ------------------------------------------------------------------
    # Trade cycle
    # ------------------------------------------------------------------

    def execute_trades(self, predictions, regime=None, trigger="SCHEDULED"):
        """Execute trades with conviction-scaled sizing and smart portfolio swapping."""
        if not self.client:
            console.print("[yellow]⚠  Alpaca client not initialized — trade execution skipped.[/yellow]")
            return {}

        buy_threshold = regime.buy_threshold if regime else self.buy_threshold
        size_multiplier = regime.size_multiplier if regime else 1.0

        exec_log: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "regime": str(regime) if regime else "UNKNOWN",
            "bought": [],
            "sold": [],
            "swapped": [],
        }

        console.rule("[bold cyan]💼  Trade Execution[/bold cyan]")

        try:
            # Load persistent state from previous cycle
            cooldowns = self._load_cooldowns()
            prev_held = self._load_held_cache()

            # 0. Cancel stale pending BUY orders from previous cycles
            open_buy_orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY))
            if open_buy_orders:
                stale = [o.symbol for o in open_buy_orders]
                console.print(f"  [dim]Canceling {len(stale)} stale BUY order(s): {', '.join(stale)}[/dim]")
                for order in open_buy_orders:
                    self.client.cancel_order_by_id(order_id=order.id)

            # 1. Fetch live account state
            account = self.client.get_account()
            cash = float(account.cash)
            portfolio_value = float(account.equity)
            positions = self.client.get_all_positions()
            held_symbols = {p.symbol for p in positions}

            console.print(
                f"  [dim]Portfolio:[/dim] [bold]${portfolio_value:,.2f}[/bold]"
                f"  [dim]Cash:[/dim] [bold]${cash:,.2f}[/bold]"
                f"  [dim]Positions:[/dim] [bold]{len(held_symbols)}/{self.max_positions}[/bold]"
                f"  [dim]Threshold:[/dim] [bold]{buy_threshold:.0f}[/bold]"
                f"  [dim]Sizing:[/dim] [bold]{size_multiplier*100:.0f}%[/bold]"
            )

            # 2. Safety Audit: ensure every position has a stop, tighten on large gains
            self._ensure_trailing_stops(positions)

            # 3. Handle Mandatory Sells (AI Downgrades to BEARISH)
            pred_map = {p.symbol: p for p in predictions}
            bearish_sold: set = set()
            for p in positions:
                symbol = p.symbol
                if symbol in pred_map and pred_map[symbol].prediction == "BEARISH":
                    console.print(f"  [red]✖  SELL {symbol}[/red]  [dim]AI downgraded to BEARISH[/dim]")
                    self._close_position_safely(symbol)
                    held_symbols.discard(symbol)
                    bearish_sold.add(symbol)
                    exec_log["sold"].append({
                        "symbol": symbol,
                        "reason": "BEARISH downgrade",
                        "detail": "; ".join(pred_map[symbol].reasoning[:2]) if pred_map[symbol].reasoning else "",
                    })

            if bearish_sold:
                cash = float(self.client.get_account().cash)
                console.print(f"  [dim]Cash after BEARISH exits: ${cash:,.2f}[/dim]")

            # 3b. Earnings proximity exits
            earnings_closed: set = set()
            for p in positions:
                symbol = p.symbol
                if symbol in bearish_sold:
                    continue
                pred = pred_map.get(symbol)
                if pred and pred.days_to_earnings is not None and pred.days_to_earnings <= 3:
                    console.print(f"  [yellow]⚡ CLOSE {symbol}[/yellow]  [dim]earnings in {pred.days_to_earnings}d — gap risk[/dim]")
                    self._close_position_safely(symbol)
                    held_symbols.discard(symbol)
                    earnings_closed.add(symbol)
                    exec_log["sold"].append({
                        "symbol": symbol,
                        "reason": f"Earnings in {pred.days_to_earnings}d",
                        "detail": "Pre-emptive close to avoid gap risk",
                    })

            if earnings_closed:
                cash = float(self.client.get_account().cash)
                console.print(f"  [dim]Cash after earnings exits: ${cash:,.2f}[/dim]")

            # Detect stop-triggered exits
            stopped_out = prev_held - held_symbols - bearish_sold - earnings_closed
            if stopped_out:
                now_iso = datetime.now(timezone.utc).isoformat()
                for sym in stopped_out:
                    cooldowns[sym] = now_iso
                    exec_log["sold"].append({"symbol": sym, "reason": "Trailing stop triggered", "detail": f"{_COOLDOWN_HOURS}h re-entry cooldown applied"})
                console.print(
                    f"  [dim]Stop-out detected: [/dim][yellow]{', '.join(stopped_out)}[/yellow]"
                    f"  [dim]→ {_COOLDOWN_HOURS}h re-entry cooldown applied[/dim]"
                )
                self._save_cooldowns(cooldowns)

            # 4. Process New Buy Opportunities
            all_bullish = [p for p in predictions if p.prediction == "BULLISH"]
            above_threshold = [p for p in all_bullish if p.overall_score >= buy_threshold]
            on_cooldown = [p for p in above_threshold if self._in_cooldown(p.symbol, cooldowns)]
            buy_candidates = [
                p for p in above_threshold
                if p.symbol not in held_symbols
                and not self._in_cooldown(p.symbol, cooldowns)
            ]
            buy_candidates.sort(key=lambda x: x.overall_score, reverse=True)

            console.print(
                f"\n  [dim]Buy funnel:[/dim]  "
                f"[green]{len(all_bullish)} BULLISH[/green]  →  "
                f"[bold]{len(above_threshold)} above threshold[/bold]  →  "
                f"[cyan]{len(buy_candidates)} actionable[/cyan]"
                + (f"  [dim]({len(on_cooldown)} on cooldown)[/dim]" if on_cooldown else "")
            )

            buys_executed = 0
            swaps_executed = 0

            for new_pick in buy_candidates:
                slot = self._slot_size_for_score(new_pick.overall_score, portfolio_value, size_multiplier)
                has_cash = cash >= slot
                has_slot = len(held_symbols) < self.max_positions

                # CASE 1: Standard Entry
                if has_cash and has_slot:
                    price, qty = self._place_market_buy(new_pick.symbol, new_pick.overall_score, new_pick.current_price, portfolio_value, size_multiplier)
                    if qty:
                        held_symbols.add(new_pick.symbol)
                        cash -= slot
                        buys_executed += 1
                        exec_log["bought"].append({
                            "symbol": new_pick.symbol,
                            "score": round(new_pick.overall_score, 1),
                            "archetype": new_pick.archetype,
                            "price": price,
                            "qty": qty,
                            "cost": round(price * qty, 2),
                            "reasons": new_pick.reasoning,
                        })

                # CASE 2: Swap
                elif len(held_symbols) > 0:
                    held_scores = [
                        (s, pred_map[s].overall_score)
                        for s in held_symbols
                        if s in pred_map
                    ]
                    if not held_scores:
                        continue

                    held_scores.sort(key=lambda x: x[1])
                    weakest_symbol, weakest_score = held_scores[0]

                    if new_pick.overall_score > (weakest_score + 5):
                        swap_reason = "FULL SLOTS" if not has_slot else "LOW CASH"
                        console.print(
                            f"  [yellow]⇄  SWAP ({swap_reason}):[/yellow]"
                            f"  [red]{weakest_symbol} {weakest_score:.1f}[/red]"
                            f"  →  [green]{new_pick.symbol} {new_pick.overall_score:.1f}[/green]"
                        )
                        try:
                            self._close_position_safely(weakest_symbol)
                            held_symbols.discard(weakest_symbol)
                            price, qty = self._place_market_buy(new_pick.symbol, new_pick.overall_score, new_pick.current_price, portfolio_value, size_multiplier)
                            if qty:
                                held_symbols.add(new_pick.symbol)
                                swaps_executed += 1
                                exec_log["swapped"].append({
                                    "out": weakest_symbol,
                                    "out_score": round(weakest_score, 1),
                                    "in": new_pick.symbol,
                                    "in_score": round(new_pick.overall_score, 1),
                                    "reason": swap_reason,
                                    "price": price,
                                    "qty": qty,
                                    "in_reasons": new_pick.reasoning,
                                })
                        except Exception as e:
                            console.print(f"  [red]✖  Swap failed for {new_pick.symbol}: {e}[/red]")

            # Persist held symbols so the next cycle can detect stop-outs
            self._save_held_cache(held_symbols)

            exec_log["summary"] = {
                "bought": buys_executed,
                "sold": len(exec_log["sold"]),
                "swapped": swaps_executed,
                "held": len(held_symbols),
            }

            # Write execution log for the dashboard
            try:
                os.makedirs(os.path.dirname(_EXECUTION_LOG_FILE), exist_ok=True)
                with open(_EXECUTION_LOG_FILE, "w") as f:
                    json.dump(exec_log, f, indent=2)
            except Exception:
                pass

            # Cycle summary
            console.print(
                f"\n  [dim]Cycle result:[/dim]  "
                f"[green]+{buys_executed} bought[/green]  ·  "
                f"[yellow]{swaps_executed} swapped[/yellow]  ·  "
                f"[red]{len(exec_log['sold'])} exits[/red]  ·  "
                f"[dim]{len(held_symbols)} positions held[/dim]"
            )

        except Exception as e:
            console.print(f"  [bold red]CRITICAL: Trade cycle failed: {e}[/bold red]")

        console.rule()
        return exec_log

    # ------------------------------------------------------------------
    # Stop management
    # ------------------------------------------------------------------

    def _ensure_trailing_stops(self, positions):
        """Ensure every position has stop-loss protection; tighten stops on large gains.

        Profit tiers (based on unrealized gain %):
          ≥ 30% gain → 0.8% trailing stop  (lock in most of the move)
          ≥ 15% gain → 1.5% trailing stop  (meaningful protection)
          default    → 3.0% trailing stop

        Fractional shares get a stop-market order instead (Alpaca limitation).
        """
        try:
            orders_req = GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.SELL)
            open_orders = self.client.get_orders(filter=orders_req)
            stop_map = {
                o.symbol: o for o in open_orders
                if o.order_type in ("trailing_stop", "stop")
            }

            stops_set = 0
            stops_tightened = 0
            stops_skipped = []

            for p in positions:
                try:
                    desired_trail = self._desired_trail_pct(p)
                    qty = float(p.qty)

                    if p.symbol in stop_map:
                        existing = stop_map[p.symbol]
                        existing_trail = getattr(existing, "trail_percent", None)
                        if existing_trail is not None:
                            existing_trail = float(existing_trail)
                            if existing_trail <= desired_trail:
                                continue
                            gain_pct = float(getattr(p, "unrealized_plpc", 0)) * 100
                            console.print(
                                f"  [dim]Stop tightened[/dim] {p.symbol}: "
                                f"[yellow]{existing_trail:.1f}% → {desired_trail:.1f}%[/yellow]"
                                f"  [dim](gain: +{gain_pct:.1f}%)[/dim]"
                            )
                            self.client.cancel_order_by_id(existing.id)
                            stops_tightened += 1
                        else:
                            continue

                    if qty.is_integer():
                        self.client.submit_order(TrailingStopOrderRequest(
                            symbol=p.symbol,
                            qty=p.qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC,
                            trail_percent=desired_trail,
                        ))
                        stops_set += 1
                    else:
                        market_price = float(p.current_price)
                        stop_price = round(market_price * (1 - desired_trail / 100), 2)
                        if stop_price >= market_price:
                            continue
                        try:
                            self.client.submit_order(StopOrderRequest(
                                symbol=p.symbol,
                                qty=str(qty),
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY,
                                stop_price=stop_price,
                            ))
                            stops_set += 1
                        except Exception as stop_err:
                            err_str = str(stop_err)
                            live_match = re.search(r'"market_price"\s*:\s*"([\d.]+)"', err_str)
                            if live_match:
                                live_price = float(live_match.group(1))
                                adj_stop = round(live_price * (1 - desired_trail / 100), 2)
                                if adj_stop < live_price:
                                    console.print(f"  [dim]Stop retry {p.symbol}: live=${live_price:.2f} → stop=${adj_stop:.2f}[/dim]")
                                    self.client.submit_order(StopOrderRequest(
                                        symbol=p.symbol,
                                        qty=str(qty),
                                        side=OrderSide.SELL,
                                        time_in_force=TimeInForce.DAY,
                                        stop_price=adj_stop,
                                    ))
                                    stops_set += 1
                                else:
                                    console.print(f"  [yellow]⚠  Cannot stop {p.symbol}: live ${live_price:.2f} too low[/yellow]")
                            else:
                                console.print(f"  [red]✖  Stop failed {p.symbol}: {stop_err}[/red]")

                except Exception:
                    # Shares locked by an existing sell order (e.g. pending BEARISH liquidation)
                    stops_skipped.append(p.symbol)

            if stops_set or stops_tightened or stops_skipped:
                skipped_str = f"  [dim]·[/dim]  [yellow]{len(stops_skipped)} skipped (shares locked): {', '.join(stops_skipped)}[/yellow]" if stops_skipped else ""
                console.print(f"  [dim]Stop audit:[/dim] {stops_set} set · {stops_tightened} tightened{skipped_str}")
        except Exception as e:
            console.print(f"  [red]✖  Safety audit setup failed: {e}[/red]")

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _close_position_safely(self, symbol: str):
        """Cancel open orders for the symbol to unlock shares, then close at market."""
        try:
            open_orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
            for order in open_orders:
                self.client.cancel_order_by_id(order_id=order.id)
            self.client.close_position(symbol_or_asset_id=symbol)
        except Exception as e:
            console.print(f"  [red]✖  Error closing {symbol}: {e}[/red]")
            raise

    def _get_live_price(self, symbol: str, fallback: float) -> float:
        """Fetch the latest trade price from Alpaca; fall back to screener price on error."""
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

    def _place_market_buy(self, symbol: str, score: float, current_price: float, portfolio_value: float, size_multiplier: float = 1.0) -> tuple[float, int]:
        """Submit a DAY market buy using whole shares (required for trailing stops).
        Returns (price, qty) on success, (0.0, 0) on skip or error."""
        live_price = self._get_live_price(symbol, current_price)
        price_source = "live" if live_price != current_price else "screener"

        if live_price <= 0:
            console.print(f"  [dim]Skipping {symbol}: invalid price[/dim]")
            return 0.0, 0

        slot = self._slot_size_for_score(score, portfolio_value, size_multiplier)
        shares_to_buy = math.floor(slot / live_price)

        if shares_to_buy <= 0:
            console.print(f"  [dim]Skipping {symbol}: ${live_price:.2f} exceeds slot ${slot:.0f}[/dim]")
            return 0.0, 0

        actual_cost = shares_to_buy * live_price
        console.print(
            f"  [green]✔  BUY {symbol}[/green]"
            f"  [dim]score=[/dim][bold]{score:.1f}[/bold]"
            f"  [dim]qty=[/dim][bold]{shares_to_buy}[/bold]"
            f"  [dim]@[/dim] ${live_price:.2f} [dim]({price_source})[/dim]"
            f"  [dim]≈[/dim] [bold]${actual_cost:.2f}[/bold]"
        )

        try:
            self.client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=shares_to_buy,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            return live_price, shares_to_buy
        except Exception as e:
            console.print(f"  [red]✖  Market buy failed for {symbol}: {e}[/red]")
            return 0.0, 0
