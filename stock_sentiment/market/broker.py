import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone, tzinfo

from rich.console import Console

console = Console()

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

_HELD_CACHE_FILE = os.path.expanduser("~/.stock_screener/held_cache.json")
_EXECUTION_LOG_FILE = os.path.expanduser("~/.stock_screener/last_execution.json")
_STOP_SKIPPED_FILE = os.path.expanduser("~/.stock_screener/stop_skipped.json")
_SHORT_ENTRY_MAX_SCORE = 35

_ET: tzinfo
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # EDT fallback


def _is_long_position(p) -> bool:
    return str(getattr(p, "side", "")).lower() in ("long", "positionside.long")


class PaperBroker:
    """Automated paper trading executor using Alpaca with Smart Conviction Swapping."""

    def __init__(self):
        self.max_positions = 10        # total portfolio cap
        self.max_short_cap = 8         # never go more than 8 shorts (keeps at least 2 long slots)
        self.buy_threshold = 60.0
        self._data_client = None

        from stock_sentiment.config import load_settings
        settings = load_settings()
        self.fixed_position_dollars = float(settings.get("fixed_position_dollars", 0))

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

    def _threshold_from_vix(self, vix: float) -> float:
        """Map VIX level to buy threshold."""
        if vix < 15:
            return 55.0   # calm   — more aggressive
        if vix < 25:
            return 60.0   # normal — baseline
        if vix < 35:
            return 70.0   # volatile — defensive
        return 85.0        # panic   — near-cash

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _slot_size_for_score(self, portfolio_value: float) -> float:
        """Return dollar allocation for a single position.

        Uses fixed_position_dollars if set; otherwise 5% of portfolio.
        Floor of $50."""
        return max(50.0, round(portfolio_value * 0.09, 2))

    # ------------------------------------------------------------------
    # Hard stop price calculation (stepped tiers)
    # ------------------------------------------------------------------

    def _desired_stop_price(self, position) -> float:
        """Return the intraday hard stop price: long –1.5%, short +0.8%."""
        is_long = _is_long_position(position)
        try:
            entry = float(position.avg_entry_price)
        except (AttributeError, ValueError, TypeError):
            entry = float(getattr(position, "current_price", 0))
        return round(entry * 0.985 if is_long else entry * 1.008, 2)

    # ------------------------------------------------------------------
    # Cooldown persistence
    # ------------------------------------------------------------------

    def _can_short(self, symbol: str) -> bool:
        """Check if Alpaca allows shorting this asset (shortable AND easy_to_borrow)."""
        try:
            asset = self.client.get_asset(symbol)
            return bool(asset.shortable) and bool(asset.easy_to_borrow)
        except Exception:
            return False

    def _close_eod_positions(self, positions) -> set:
        """Close ALL positions at or after 3:45 PM ET (day-trading mode). Returns closed symbols."""
        closed: set = set()
        try:
            now_et = datetime.now(_ET)
            if not (now_et.hour > 15 or (now_et.hour == 15 and now_et.minute >= 30)):
                return closed
            eod_syms = {p.symbol for p in positions}
            if not eod_syms:
                return closed
            console.print(f"  [yellow]⏰ EOD close: {len(eod_syms)} position(s): {', '.join(sorted(eod_syms))}[/yellow]")
            for sym in eod_syms:
                try:
                    self._close_position_safely(sym)
                    closed.add(sym)
                except Exception as e:
                    console.print(f"  [red]✖ EOD close failed {sym}: {e}[/red]")
        except Exception as e:
            console.print(f"  [red]✖ EOD close error: {e}[/red]")
        return closed

    def _load_held_cache(self) -> dict:
        """Returns {symbol: {"type": "DAY"|"SWING", "direction": "LONG"|"SHORT", "entered_at": ISO}}."""
        try:
            if os.path.exists(_HELD_CACHE_FILE):
                with open(_HELD_CACHE_FILE) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return {s: {"type": "SWING", "direction": "LONG", "entered_at": ""} for s in data}
                # Back-fill "direction" for older cache entries written before short support
                for meta in data.values():
                    if isinstance(meta, dict) and "direction" not in meta:
                        meta["direction"] = "LONG"
                return data
        except Exception:
            pass
        return {}

    def _save_held_cache(self, cache: dict) -> None:
        os.makedirs(os.path.dirname(_HELD_CACHE_FILE), exist_ok=True)
        with open(_HELD_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

    # ------------------------------------------------------------------
    # Trade cycle
    # ------------------------------------------------------------------

    def execute_trades(self, predictions, trigger="SCHEDULED"):
        """Execute trades with equal sizing and fixed position limits."""
        if not self.client:
            console.print("[yellow]⚠  Alpaca client not initialized — trade execution skipped.[/yellow]")
            return {}

        vix = self._get_vix()
        buy_threshold = self._threshold_from_vix(vix)
        vix_regime = "calm" if vix < 15 else "normal" if vix < 25 else "volatile" if vix < 35 else "panic"

        exec_log: dict = {
            "timestamp": datetime.now(_ET).isoformat(),
            "trigger": trigger,
            "bought": [],
            "sold": [],
            "swapped": [],
            "shorted": [],
            "covered": [],
        }

        console.rule("[bold cyan]💼  Trade Execution[/bold cyan]")

        try:
            # Load persistent state from previous cycle
            held_cache = self._load_held_cache()

            # 0. Cancel stale pending market orders from previous cycles
            for side in [OrderSide.BUY, OrderSide.SELL]:
                pending = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, side=side))
                stale = [
                    o for o in pending
                    if str(getattr(o, "order_type", "")).lower() in ("market", "ordertype.market")
                ]
                if stale:
                    console.print(f"  [dim]Canceling {len(stale)} stale {side} market order(s)[/dim]")
                    for order in stale:
                        self.client.cancel_order_by_id(order_id=order.id)

            # 1. Fetch live account state
            account = self.client.get_account()
            cash = float(account.cash)
            portfolio_value = float(account.equity)
            positions = self.client.get_all_positions()

            long_positions = [p for p in positions if _is_long_position(p)]
            short_positions = [p for p in positions if not _is_long_position(p)]
            long_symbols: set = {p.symbol for p in long_positions}
            short_symbols: set = {p.symbol for p in short_positions}
            held_symbols = long_symbols | short_symbols

            over_cap = len(held_symbols) > self.max_positions
            console.print(
                f"  [dim]Portfolio:[/dim] [bold]${portfolio_value:,.2f}[/bold]"
                f"  [dim]Cash:[/dim] [bold]${cash:,.2f}[/bold]"
                f"  [dim]Positions:[/dim] [bold]{len(held_symbols)}/{self.max_positions}[/bold]"
                f"  [dim]({len(long_symbols)}L / {len(short_symbols)}S)[/dim]"
                + ("  [bold red]⚠ OVER CAP — trimming weakest[/bold red]" if over_cap else "")
                + f"  [dim]VIX:[/dim] [bold]{vix:.1f}[/bold] [dim]({vix_regime})[/dim]"
                + f"  [dim]Threshold:[/dim] [bold]{buy_threshold:.0f}[/bold]"
            )

            # Build prediction lookup early — needed for cap enforcement and BEARISH exits
            pred_map = {p.symbol: p for p in predictions}

            # 1b. Total cap enforcement: close worst positions until back at limit
            if over_cap:
                excess = len(held_symbols) - self.max_positions
                pos_map = {p.symbol: p for p in positions}

                def _trim_key(sym: str) -> tuple[float, float]:
                    pos = pos_map.get(sym)
                    # unrealized_plpc is a decimal fraction (e.g. -0.05 = -5%)
                    plpc = float(getattr(pos, "unrealized_plpc", 0)) * 100 if pos else 0.0
                    conviction = abs(pred_map[sym].overall_score - 50) if sym in pred_map else 0.0
                    return (plpc, conviction)

                ranked = sorted(held_symbols, key=_trim_key)
                to_trim = ranked[:excess]
                console.print(f"  [yellow]✂  Trimming {len(to_trim)} over-cap position(s): {', '.join(to_trim)}[/yellow]")
                for sym in to_trim:
                    try:
                        self._close_position_safely(sym)
                        long_symbols.discard(sym)
                        short_symbols.discard(sym)
                        held_symbols.discard(sym)
                        held_cache.pop(sym, None)
                        exec_log["sold"].append({"symbol": sym, "reason": "Position cap enforcement", "detail": f"Trimmed to {self.max_positions}-slot limit"})
                    except Exception as e:
                        console.print(f"  [red]✖ Trim failed {sym}: {e}[/red]")
                cash = float(self.client.get_account().cash)

            # 2. Safety Audit: ensure every position has a hard stop; step up on gains
            self._ensure_hard_stops(positions, held_cache)

            # 2b. EOD close: liquidate all positions at 3:45 PM ET
            eod_closed = self._close_eod_positions(positions)
            for sym in eod_closed:
                held_symbols.discard(sym)
                long_symbols.discard(sym)
                short_symbols.discard(sym)
                held_cache.pop(sym, None)
                exec_log["sold"].append({"symbol": sym, "reason": "EOD close", "detail": "Day trade — closed at 3:45 PM ET"})

            # 3. Mandatory exits for long positions downgraded to BEARISH
            bearish_sold: set = set()
            for p in long_positions:
                symbol = p.symbol
                if symbol in eod_closed or symbol not in long_symbols:
                    continue
                if symbol in pred_map and pred_map[symbol].prediction == "BEARISH":
                    console.print(f"  [red]✖  SELL {symbol}[/red]  [dim]AI downgraded to BEARISH[/dim]")
                    self._close_position_safely(symbol)
                    long_symbols.discard(symbol)
                    held_symbols.discard(symbol)
                    bearish_sold.add(symbol)
                    exec_log["sold"].append({
                        "symbol": symbol,
                        "reason": "BEARISH downgrade",
                        "detail": "; ".join(pred_map[symbol].reasoning[:2]) if pred_map[symbol].reasoning else "",
                    })

            # 3a. Cover short positions that flipped BULLISH
            bullish_covered: set = set()
            for p in short_positions:
                symbol = p.symbol
                if symbol in eod_closed or symbol not in short_symbols:
                    continue
                if symbol in pred_map and pred_map[symbol].prediction == "BULLISH":
                    console.print(f"  [green]✔  COVER {symbol}[/green]  [dim]AI upgraded to BULLISH — closing short[/dim]")
                    self._close_position_safely(symbol)
                    short_symbols.discard(symbol)
                    held_symbols.discard(symbol)
                    bullish_covered.add(symbol)
                    exec_log["covered"].append({
                        "symbol": symbol,
                        "reason": "BULLISH upgrade",
                        "detail": "; ".join(pred_map[symbol].reasoning[:2]) if pred_map[symbol].reasoning else "",
                    })

            if bearish_sold or bullish_covered:
                cash = float(self.client.get_account().cash)
                console.print(f"  [dim]Cash after exits: ${cash:,.2f}[/dim]")

            # 3b. Flip: immediately short any position just sold as BEARISH this cycle.
            # Requires same conviction gate as regular shorts (≤35) to avoid occupying a slot
            # with a weak signal that blocks stronger candidates in the normal short cycle.
            # All flips are DAY trades (closed at EOD).
            for symbol in bearish_sold:
                pred = pred_map.get(symbol)
                if pred is None:
                    continue
                if pred.overall_score > _SHORT_ENTRY_MAX_SCORE:
                    console.print(f"  [dim]Flip skip {symbol}: score {pred.overall_score:.1f} > {_SHORT_ENTRY_MAX_SCORE} — insufficient conviction to short[/dim]")
                    continue
                if not self._can_short(symbol):
                    console.print(f"  [dim]Flip skip {symbol}: not shortable[/dim]")
                    continue
                has_short_slot = len(short_symbols) < self.max_short_cap and len(held_symbols) < self.max_positions
                has_cash = cash >= self._slot_size_for_score(portfolio_value)
                if not (has_cash and has_short_slot):
                    console.print(f"  [dim]Flip skip {symbol}: no slot or cash[/dim]")
                    continue
                price, qty = self._place_market_short(symbol, pred.current_price, portfolio_value)
                if qty:
                    short_symbols.add(symbol)
                    held_symbols.add(symbol)
                    held_cache[symbol] = {"type": "DAY", "direction": "SHORT", "entered_at": datetime.now(_ET).isoformat()}
                    cash -= self._slot_size_for_score(portfolio_value)
                    console.print(f"  [yellow]⇄  FLIP {symbol}[/yellow]  [dim]long → short (BEARISH downgrade)[/dim]")
                    exec_log["shorted"].append({
                        "symbol": symbol,
                        "score": round(pred.overall_score, 1),
                        "archetype": pred.archetype,
                        "trade_type": "DAY",
                        "direction": "SHORT",
                        "price": price,
                        "qty": qty,
                        "proceeds": round(price * qty, 2),
                        "reasons": pred.reasoning,
                    })
            if any(symbol in short_symbols for symbol in bearish_sold):
                cash = float(self.client.get_account().cash)
                console.print(f"  [dim]Cash after flips: ${cash:,.2f}[/dim]")

            # 3c. Flip: immediately go long any position just covered as BULLISH this cycle.
            # No score gate — the BULLISH prediction itself is the signal.
            for symbol in bullish_covered:
                pred = pred_map.get(symbol)
                if pred is None:
                    continue
                has_slot = len(held_symbols) < self.max_positions
                has_cash = cash >= self._slot_size_for_score(portfolio_value)
                if not (has_cash and has_slot):
                    console.print(f"  [dim]Flip skip {symbol}: no slot or cash[/dim]")
                    continue
                trade_type = "DAY"
                price, qty = self._place_market_buy(symbol, pred.current_price, portfolio_value)
                if qty:
                    long_symbols.add(symbol)
                    held_symbols.add(symbol)
                    held_cache[symbol] = {"type": trade_type, "direction": "LONG", "entered_at": datetime.now(_ET).isoformat()}
                    cash -= self._slot_size_for_score(portfolio_value)
                    console.print(f"  [yellow]⇄  FLIP {symbol}[/yellow]  [dim]short → long (BULLISH upgrade)[/dim]")
                    exec_log["bought"].append({
                        "symbol": symbol,
                        "score": round(pred.overall_score, 1),
                        "archetype": pred.archetype,
                        "trade_type": trade_type,
                        "direction": "LONG",
                        "price": price,
                        "qty": qty,
                        "cost": round(price * qty, 2),
                        "reasons": pred.reasoning,
                    })
            if any(symbol in long_symbols for symbol in bullish_covered):
                cash = float(self.client.get_account().cash)
                console.print(f"  [dim]Cash after flips: ${cash:,.2f}[/dim]")

            # 3d. Earnings proximity exits (both directions)
            earnings_closed: set = set()
            for p in positions:
                symbol = p.symbol
                if symbol in bearish_sold or symbol in bullish_covered or symbol in eod_closed:
                    continue
                pred = pred_map.get(symbol)
                if pred and pred.days_to_earnings is not None and pred.days_to_earnings <= 3:
                    console.print(f"  [yellow]⚡ CLOSE {symbol}[/yellow]  [dim]earnings in {pred.days_to_earnings}d — gap risk[/dim]")
                    self._close_position_safely(symbol)
                    held_symbols.discard(symbol)
                    long_symbols.discard(symbol)
                    short_symbols.discard(symbol)
                    earnings_closed.add(symbol)
                    exec_log["sold"].append({
                        "symbol": symbol,
                        "reason": f"Earnings in {pred.days_to_earnings}d",
                        "detail": "Pre-emptive close to avoid gap risk",
                    })

            if earnings_closed:
                cash = float(self.client.get_account().cash)
                console.print(f"  [dim]Cash after earnings exits: ${cash:,.2f}[/dim]")

            # 4. Process New Long Buy Opportunities
            all_bullish = [p for p in predictions if p.prediction == "BULLISH"]
            above_threshold = [p for p in all_bullish if p.overall_score >= buy_threshold]
            buy_candidates = [
                p for p in above_threshold
                if p.symbol not in held_symbols
            ]
            buy_candidates.sort(key=lambda x: x.overall_score, reverse=True)

            console.print(
                f"\n  [dim]Long funnel:[/dim]  "
                f"[green]{len(all_bullish)} BULLISH[/green]  →  "
                f"[bold]{len(above_threshold)} above threshold[/bold]  →  "
                f"[cyan]{len(buy_candidates)} actionable[/cyan]"
            )

            buys_executed = 0
            swaps_executed = 0
            swapped_out: set = set()

            for new_pick in buy_candidates:
                if len(held_symbols) >= self.max_positions:
                    break
                trade_type = "DAY"
                slot = self._slot_size_for_score(portfolio_value)
                has_cash = cash >= slot
                has_slot = len(held_symbols) < self.max_positions

                # CASE 1: Standard Entry
                if has_cash and has_slot:
                    price, qty = self._place_market_buy(new_pick.symbol, new_pick.current_price, portfolio_value)
                    if qty:
                        long_symbols.add(new_pick.symbol)
                        held_symbols.add(new_pick.symbol)
                        held_cache[new_pick.symbol] = {"type": trade_type, "direction": "LONG", "entered_at": datetime.now(_ET).isoformat()}
                        cash -= slot
                        buys_executed += 1

                        exec_log["bought"].append({
                            "symbol": new_pick.symbol,
                            "score": round(new_pick.overall_score, 1),
                            "archetype": new_pick.archetype,
                            "trade_type": trade_type,
                            "direction": "LONG",
                            "price": price,
                            "qty": qty,
                            "cost": round(price * qty, 2),
                            "reasons": new_pick.reasoning,
                        })

                # CASE 2: Swap — portfolio full, replace weakest long with better pick
                elif long_symbols and len(held_symbols) >= self.max_positions:
                    held_scores = [
                        (s, pred_map[s].overall_score)
                        for s in long_symbols
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
                            long_symbols.discard(weakest_symbol)
                            held_symbols.discard(weakest_symbol)
                            held_cache.pop(weakest_symbol, None)
                            swapped_out.add(weakest_symbol)
                            price, qty = self._place_market_buy(new_pick.symbol, new_pick.current_price, portfolio_value)
                            if qty:
                                long_symbols.add(new_pick.symbol)
                                held_symbols.add(new_pick.symbol)
                                held_cache[new_pick.symbol] = {"type": trade_type, "direction": "LONG", "entered_at": datetime.now(_ET).isoformat()}
                                swaps_executed += 1
        
                                exec_log["swapped"].append({
                                    "out": weakest_symbol,
                                    "out_score": round(weakest_score, 1),
                                    "in": new_pick.symbol,
                                    "in_score": round(new_pick.overall_score, 1),
                                    "trade_type": trade_type,
                                    "direction": "LONG",
                                    "reason": swap_reason,
                                    "price": price,
                                    "qty": qty,
                                    "in_reasons": new_pick.reasoning,
                                })
                        except Exception as e:
                            console.print(f"  [red]✖  Swap failed for {new_pick.symbol}: {e}[/red]")

            # 5. Process Short Opportunities (BEARISH predictions with strong conviction)
            all_bearish = [p for p in predictions if p.prediction == "BEARISH"]
            short_candidates = [
                p for p in all_bearish
                if p.overall_score <= _SHORT_ENTRY_MAX_SCORE
                and p.symbol not in held_symbols
                and p.symbol not in swapped_out
            ]
            short_candidates.sort(key=lambda x: x.overall_score)  # lowest score = strongest BEARISH first

            if short_candidates:
                console.print(
                    f"\n  [dim]Short funnel:[/dim]  "
                    f"[red]{len(all_bearish)} BEARISH[/red]  →  "
                    f"[bold]{len(short_candidates)} short candidates (score≤{_SHORT_ENTRY_MAX_SCORE})[/bold]"
                )

            shorts_executed = 0
            for new_short in short_candidates:
                if not self._can_short(new_short.symbol):
                    console.print(f"  [dim]Skip short {new_short.symbol}: not shortable[/dim]")
                    continue
                trade_type = "DAY"
                slot = self._slot_size_for_score(portfolio_value)
                has_short_slot = len(short_symbols) < self.max_short_cap and len(held_symbols) < self.max_positions
                has_cash = cash >= slot

                # CASE 1: Standard short entry
                if has_cash and has_short_slot:
                    price, qty = self._place_market_short(new_short.symbol, new_short.current_price, portfolio_value)
                    if qty:
                        short_symbols.add(new_short.symbol)
                        held_symbols.add(new_short.symbol)
                        held_cache[new_short.symbol] = {"type": trade_type, "direction": "SHORT", "entered_at": datetime.now(_ET).isoformat()}
                        cash -= slot
                        shorts_executed += 1

                        exec_log["shorted"].append({
                            "symbol": new_short.symbol,
                            "score": round(new_short.overall_score, 1),
                            "archetype": new_short.archetype,
                            "trade_type": trade_type,
                            "direction": "SHORT",
                            "price": price,
                            "qty": qty,
                            "proceeds": round(price * qty, 2),
                            "reasons": new_short.reasoning,
                        })

                # CASE 2: Short swap — replace weakest short with stronger signal
                elif short_symbols and (len(short_symbols) >= self.max_short_cap or len(held_symbols) >= self.max_positions):
                    held_short_scores = [
                        (s, pred_map[s].overall_score)
                        for s in short_symbols
                        if s in pred_map
                    ]
                    if not held_short_scores:
                        continue
                    # Highest score = least bearish = weakest short
                    held_short_scores.sort(key=lambda x: x[1], reverse=True)
                    weakest_symbol, weakest_score = held_short_scores[0]
                    if new_short.overall_score < (weakest_score - 5):
                        console.print(
                            f"  [yellow]⇄  SHORT SWAP:[/yellow]"
                            f"  [dim]{weakest_symbol} {weakest_score:.1f}[/dim]"
                            f"  →  [red]{new_short.symbol} {new_short.overall_score:.1f}[/red]"
                        )
                        try:
                            self._close_position_safely(weakest_symbol)
                            short_symbols.discard(weakest_symbol)
                            held_symbols.discard(weakest_symbol)
                            held_cache.pop(weakest_symbol, None)
                            price, qty = self._place_market_short(new_short.symbol, new_short.current_price, portfolio_value)
                            if qty:
                                short_symbols.add(new_short.symbol)
                                held_symbols.add(new_short.symbol)
                                held_cache[new_short.symbol] = {"type": trade_type, "direction": "SHORT", "entered_at": datetime.now(_ET).isoformat()}
                                shorts_executed += 1

                                exec_log["swapped"].append({
                                    "out": weakest_symbol,
                                    "out_score": round(weakest_score, 1),
                                    "in": new_short.symbol,
                                    "in_score": round(new_short.overall_score, 1),
                                    "trade_type": trade_type,
                                    "direction": "SHORT",
                                    "reason": "STRONGER SHORT SIGNAL",
                                    "price": price,
                                    "qty": qty,
                                    "in_reasons": new_short.reasoning,
                                })
                        except Exception as e:
                            console.print(f"  [red]✖  Short swap failed for {new_short.symbol}: {e}[/red]")
                else:
                    break

            # Persist held cache — prune to symbols still held
            held_cache = {sym: meta for sym, meta in held_cache.items() if sym in held_symbols}
            self._save_held_cache(held_cache)

            exec_log["summary"] = {
                "bought": buys_executed,
                "shorted": shorts_executed,
                "sold": len(exec_log["sold"]),
                "covered": len(exec_log["covered"]),
                "swapped": swaps_executed,
                "held_long": len(long_symbols),
                "held_short": len(short_symbols),
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
                f"[green]+{buys_executed} long[/green]  ·  "
                f"[red]↓{shorts_executed} short[/red]  ·  "
                f"[yellow]{swaps_executed} swapped[/yellow]  ·  "
                f"[red]{len(exec_log['sold'])} exits · {len(exec_log['covered'])} covered[/red]  ·  "
                f"[dim]{len(long_symbols)}L / {len(short_symbols)}S held[/dim]"
            )

        except Exception as e:
            console.print(f"  [bold red]CRITICAL: Trade cycle failed: {e}[/bold red]")

        console.rule()
        return exec_log

    # ------------------------------------------------------------------
    # Stop management
    # ------------------------------------------------------------------

    def _place_stop_for_new_position(self, symbol: str, qty: int, is_long: bool, entry_price: float) -> bool:
        """Place a hard stop-loss immediately after entry: long –1.5%, short +0.8%.
        Retries up to 3 times (0.5s apart) to handle market-order fill race condition."""
        pct = 0.015 if is_long else 0.008
        stop_price = round(entry_price * (1 - pct) if is_long else entry_price * (1 + pct), 2)
        stop_side = OrderSide.SELL if is_long else OrderSide.BUY
        for attempt in range(3):
            try:
                self.client.submit_order(StopOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=stop_side,
                    time_in_force=TimeInForce.GTC,
                    stop_price=stop_price,
                ))
                console.print(f"  [dim]Stop placed {symbol}: ${stop_price:.2f} ({pct * 100:.1f}% from ${entry_price:.2f})[/dim]")
                return True
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.5)
                else:
                    console.print(f"  [red]✖  Stop failed {symbol} after 3 attempts: {e}[/red]")
        return False

    def _ensure_hard_stops(self, positions, held_cache: dict):
        """Audit every position for a hard stop-loss; step up price as gains accrue.

        Fixes vs old trailing-stop approach:
        - Order type matched by substring → handles SDK enum strings correctly
        - All stops per symbol collected (no silent last-writer-wins dedup)
        - Price-based comparison (higher for longs = tighter; lower for shorts = tighter)
        - Whole-share stops use GTC; fractional use DAY (Alpaca API constraint)
        """
        try:
            try:
                with open(_STOP_SKIPPED_FILE) as f:
                    prev_skipped: set = set(json.load(f))
            except Exception:
                prev_skipped = set()

            open_orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
            # Group ALL stop-type orders by symbol — handles duplicates from prior cycles
            stop_map: dict[str, list] = {}
            for o in open_orders:
                if "stop" in str(getattr(o, "order_type", "")).lower():
                    stop_map.setdefault(o.symbol, []).append(o)

            stops_set = 0
            stops_stepped = 0
            stops_skipped = []

            for p in positions:
                try:
                    is_long = _is_long_position(p)
                    stop_side = OrderSide.SELL if is_long else OrderSide.BUY
                    desired_stop = self._desired_stop_price(p)
                    qty = abs(float(p.qty))  # Alpaca returns negative qty for short positions
                    is_fractional = not qty.is_integer()
                    tif = TimeInForce.DAY if is_fractional else TimeInForce.GTC
                    qty_arg: int | str = str(qty) if is_fractional else int(qty)

                    existing = stop_map.get(p.symbol, [])

                    if existing:
                        # Find correct-side hard stops with a known stop_price
                        hard_correct = [
                            o for o in existing
                            if "trailing" not in str(getattr(o, "order_type", "")).lower()
                            and (("sell" in str(getattr(o, "side", "")).lower()) == is_long)
                            and getattr(o, "stop_price", None) is not None
                        ]

                        if len(existing) > 1 or not hard_correct:
                            # Duplicates, wrong type, or wrong side — cancel all and replace
                            for o in existing:
                                try:
                                    self.client.cancel_order_by_id(o.id)
                                except Exception:
                                    pass
                            stops_stepped += 1
                        else:
                            existing_price = float(hard_correct[0].stop_price)
                            needs_step = (
                                desired_stop > existing_price if is_long
                                else desired_stop < existing_price
                            )
                            if not needs_step:
                                continue
                            gain_pct = float(getattr(p, "unrealized_plpc", 0)) * 100
                            console.print(
                                f"  [dim]Stop stepped[/dim] {p.symbol}: "
                                f"[yellow]${existing_price:.2f} → ${desired_stop:.2f}[/yellow]"
                                f"  [dim](gain: +{gain_pct:.1f}%)[/dim]"
                            )
                            try:
                                self.client.cancel_order_by_id(hard_correct[0].id)
                            except Exception:
                                pass
                            stops_stepped += 1

                    # Place hard stop
                    try:
                        self.client.submit_order(StopOrderRequest(
                            symbol=p.symbol,
                            qty=qty_arg,
                            side=stop_side,
                            time_in_force=tif,
                            stop_price=desired_stop,
                        ))
                        stops_set += 1
                    except Exception as stop_err:
                        err_str = str(stop_err)
                        live_match = re.search(r'"market_price"\s*:\s*"([\d.]+)"', err_str)
                        if live_match:
                            live_price = float(live_match.group(1))
                            adj = round(live_price * 0.97 if is_long else live_price * 1.03, 2)
                            valid = adj < live_price if is_long else adj > live_price
                            if valid:
                                console.print(f"  [dim]Stop retry {p.symbol}: live=${live_price:.2f} → ${adj:.2f}[/dim]")
                                try:
                                    self.client.submit_order(StopOrderRequest(
                                        symbol=p.symbol, qty=qty_arg, side=stop_side,
                                        time_in_force=tif, stop_price=adj,
                                    ))
                                    stops_set += 1
                                except Exception:
                                    stops_skipped.append(p.symbol)
                            else:
                                console.print(f"  [yellow]⚠  Cannot stop {p.symbol}: live ${live_price:.2f} — stop invalid[/yellow]")
                                stops_skipped.append(p.symbol)
                        else:
                            console.print(f"  [red]✖  Stop failed {p.symbol}: {stop_err}[/red]")
                            stops_skipped.append(p.symbol)

                except Exception:
                    stops_skipped.append(p.symbol)

            try:
                os.makedirs(os.path.dirname(_STOP_SKIPPED_FILE), exist_ok=True)
                with open(_STOP_SKIPPED_FILE, "w") as f:
                    json.dump(stops_skipped, f)
            except Exception:
                pass

            persistent_no_stop = set(stops_skipped) & prev_skipped
            if persistent_no_stop:
                console.print(
                    f"  [bold red]⚠  NO STOP PROTECTION (2+ cycles): {', '.join(sorted(persistent_no_stop))}"
                    f" — force-closing unprotected positions[/bold red]"
                )
                for sym in sorted(persistent_no_stop):
                    try:
                        self._close_position_safely(sym)
                        console.print(f"  [red]✖  Force-closed {sym}: could not place stop after 2 cycles[/red]")
                    except Exception as close_err:
                        console.print(f"  [red]✖  Force-close failed {sym}: {close_err}[/red]")

            if stops_set or stops_stepped or stops_skipped:
                skipped_str = f"  [dim]·[/dim]  [yellow]{len(stops_skipped)} skipped: {', '.join(stops_skipped)}[/yellow]" if stops_skipped else ""
                console.print(f"  [dim]Stop audit:[/dim] {stops_set} set · {stops_stepped} stepped{skipped_str}")
        except Exception as e:
            console.print(f"  [red]✖  Safety audit setup failed: {e}[/red]")

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _close_position_safely(self, symbol: str):
        """Cancel open orders for the symbol to unlock shares, then close at market.
        Works for both long (market sell) and short (market buy to cover) positions."""
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

    def _place_market_buy(self, symbol: str, current_price: float, portfolio_value: float) -> tuple[float, int]:
        """Submit a market buy (long entry) using whole shares.
        Returns (price, qty) on success, (0.0, 0) on skip or error."""
        live_price = self._get_live_price(symbol, current_price)
        price_source = "live" if live_price != current_price else "screener"

        if live_price <= 0:
            console.print(f"  [dim]Skipping {symbol}: invalid price[/dim]")
            return 0.0, 0

        slot = self._slot_size_for_score(portfolio_value)
        shares_to_buy = math.floor(slot / live_price)

        if shares_to_buy <= 0:
            console.print(f"  [dim]Skipping {symbol}: ${live_price:.2f} exceeds slot ${slot:.0f}[/dim]")
            return 0.0, 0

        actual_cost = shares_to_buy * live_price
        console.print(
            f"  [green]✔  BUY {symbol}[/green]"
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
            placed = self._place_stop_for_new_position(symbol, shares_to_buy, is_long=True, entry_price=live_price)
            if not placed:
                console.print(f"  [yellow]⚠  {symbol} entered WITHOUT stop — audit will retry next cycle[/yellow]")
            return live_price, shares_to_buy
        except Exception as e:
            console.print(f"  [red]✖  Market buy failed for {symbol}: {e}[/red]")
            return 0.0, 0

    def _place_market_short(self, symbol: str, current_price: float, portfolio_value: float) -> tuple[float, int]:
        """Submit a market sell to open a short position using whole shares.
        Returns (price, qty) on success, (0.0, 0) on skip or error."""
        live_price = self._get_live_price(symbol, current_price)
        price_source = "live" if live_price != current_price else "screener"

        if live_price <= 0:
            console.print(f"  [dim]Skipping short {symbol}: invalid price[/dim]")
            return 0.0, 0

        slot = self._slot_size_for_score(portfolio_value)
        shares_to_short = math.floor(slot / live_price)

        if shares_to_short <= 0:
            console.print(f"  [dim]Skipping short {symbol}: ${live_price:.2f} exceeds slot ${slot:.0f}[/dim]")
            return 0.0, 0

        actual_value = shares_to_short * live_price
        console.print(
            f"  [red]✔  SHORT {symbol}[/red]"
            f"  [dim]qty=[/dim][bold]{shares_to_short}[/bold]"
            f"  [dim]@[/dim] ${live_price:.2f} [dim]({price_source})[/dim]"
            f"  [dim]≈[/dim] [bold]${actual_value:.2f}[/bold]"
        )

        try:
            self.client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=shares_to_short,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            time.sleep(2)  # wait for short fill before placing BUY stop (prevents wash-trade rejection)
            placed = self._place_stop_for_new_position(symbol, shares_to_short, is_long=False, entry_price=live_price)
            if not placed:
                console.print(f"  [yellow]⚠  {symbol} shorted WITHOUT stop — audit will retry next cycle[/yellow]")
            return live_price, shares_to_short
        except Exception as e:
            console.print(f"  [red]✖  Market short failed for {symbol}: {e}[/red]")
            return 0.0, 0
