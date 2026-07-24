"""Paper portfolio manager — routes the desk's booked picks to an Alpaca PAPER account and
reconciles them, so the Alpaca account is an honest real-fill scoreboard (real fills, slippage,
portfolio P&L) instead of only the internal simulated ledger.

OPT-IN: nothing routes to Alpaca unless `PAPER_TRADING` is set. Research/paper only — the Alpaca
PAPER endpoint (paper-api.alpaca.markets); no real money.

Design — a RECONCILIATION loop, not inline order-placing, so it is idempotent and restart-safe:
`reconcile()` makes Alpaca *match* the ledger's open-taken positions —
  • OPEN what the ledger holds but Alpaca doesn't (highest conviction first, capped at
    PM_MAX_POSITIONS; conviction-weighted size), stamping the order id on the pick;
  • CLOSE what Alpaca holds but the ledger has exited/graded.
Sizing is conviction-weighted: $PM_BASE_USD for a conviction-50 pick, scaled by adjusted_score,
capped at PM_MAX_POSITION_USD. So thin leans get tiny positions and high-conviction gets more —
selection re-expressed as SIZE now that the desk takes everything.

Limitations (v1): one position per SYMBOL (Alpaca aggregates); a short that isn't shortable is
rejected and not retried; partial fills aren't tracked beyond the submit.
"""

import logging
import threading

from alphadesk.config import (
    PAPER_TRADING,
    PM_BASE_USD,
    PM_MAX_POSITION_USD,
    PM_MAX_POSITIONS,
)
from alphadesk.ledger import store

log = logging.getLogger("alphadesk.portfolio")

_client = None
_client_lock = threading.Lock()


def _trading_client():
    """Lazily-built Alpaca PAPER trading client (same keys as the data client). None if the
    keys are missing or the SDK can't initialise."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    import os

                    from alpaca.trading.client import TradingClient
                    _client = TradingClient(
                        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"],
                        paper=True)
                except Exception as exc:      # missing keys / import failure
                    log.warning("Alpaca trading client unavailable: %s", exc)
                    return None
    return _client


def _size_shares(pick: dict, price: float | None) -> int:
    """Conviction-weighted whole-share size: $PM_BASE_USD at conviction 50, scaled linearly by
    adjusted_score (floor 0.1x), capped at PM_MAX_POSITION_USD. 0 if no usable price."""
    if not price or price <= 0:
        return 0
    conv = pick.get("adjusted_score") or pick.get("confidence") or 50
    dollars = min(PM_MAX_POSITION_USD, PM_BASE_USD * max(0.1, float(conv) / 50.0))
    return int(dollars // price)


def reconcile() -> dict:
    """Make the Alpaca paper account match the ledger's open-taken positions. Idempotent —
    safe to call on a loop. Returns a summary dict."""
    if not PAPER_TRADING:
        return {"enabled": False}
    client = _trading_client()
    if client is None:
        return {"enabled": True, "error": "no trading client"}
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    try:
        positions = {p.symbol.upper(): p for p in client.get_all_positions()}
    except Exception as exc:
        log.warning("get_all_positions failed: %s", exc)
        return {"enabled": True, "error": str(exc)}

    open_taken = store.open_taken_picks()
    open_syms = {p["symbol"].upper() for p in open_taken}
    opened = closed = 0

    # ENTRIES — open what the ledger has but Alpaca doesn't, best conviction first, capped.
    slots = len(positions)
    submitted: set[str] = set()
    for pick in sorted(open_taken, key=lambda p: -(p.get("adjusted_score") or 0)):
        sym = pick["symbol"].upper()
        if (sym in positions or sym in submitted or pick.get("broker_order_id")
                or (pick.get("broker_status") or "").startswith("rejected")
                or pick.get("entry_price") is None):   # already routed / rejected / not filled
            continue
        if slots >= PM_MAX_POSITIONS:
            break
        qty = _size_shares(pick, pick.get("entry_price"))
        if qty < 1:
            continue
        side = OrderSide.BUY if pick["direction"] == "LONG" else OrderSide.SELL
        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty, side=side, time_in_force=TimeInForce.DAY))
            store.set_broker_order(pick["id"], str(order.id), "submitted", qty)
            submitted.add(sym)
            slots += 1
            opened += 1
            log.info("PM opened %s %s x%d (conv %s)", pick["direction"], sym, qty,
                     pick.get("adjusted_score"))
        except Exception as exc:
            store.set_broker_order(pick["id"], None, f"rejected: {exc}", 0)
            log.warning("PM order REJECTED %s %s: %s", pick["direction"], sym, exc)

    # EXITS — close what Alpaca holds but the ledger has exited/graded (no longer open-taken).
    for sym in positions:
        if sym not in open_syms:
            try:
                client.close_position(sym)
                closed += 1
                log.info("PM closed %s (ledger exited)", sym)
            except Exception as exc:
                log.warning("PM close failed %s: %s", sym, exc)

    return {"enabled": True, "opened": opened, "closed": closed,
            "alpaca_positions": len(positions)}
