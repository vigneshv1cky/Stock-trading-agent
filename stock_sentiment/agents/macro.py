"""MacroAgent — polls market indices every 5 min and publishes macro regime context.

Publishes:  macro.context
State:      MacroAgent.current  (class-level dict, readable synchronously by any agent)

Regime ladder:
  RISK_ON   — VIX ≤ 15 and SPY positive
  NEUTRAL   — default / mixed signals
  RISK_OFF  — VIX ≥ 22
  PANIC     — VIX ≥ 30
"""

import asyncio
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus

_POLL_INTERVAL_S = 300  # 5 minutes
_SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLU", "XLC", "XLRE", "XLP", "XLB", "XLY"]
_INDICES = ["SPY", "QQQ", "IWM", "^VIX"]


class MacroAgent(BaseAgent):
    current: dict = {}  # class-level shared state — readable anywhere without queue

    def __init__(self, bus: EventBus):
        super().__init__(bus, "MacroAgent")

    async def run(self) -> None:
        await self._poll()  # immediate first fetch so downstream agents have context
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            await self._poll()

    async def _poll(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            ctx = await loop.run_in_executor(None, self._fetch_context)
            MacroAgent.current = ctx
            await self.bus.publish("macro.context", ctx)
            self.log.info(
                "Macro: regime=%-8s  VIX=%.1f  SPY=%+.1f%%  QQQ=%+.1f%%  breadth=%s",
                ctx.get("regime"), ctx.get("vix", 0),
                ctx.get("spy_change_pct", 0), ctx.get("qqq_change_pct", 0),
                ctx.get("breadth"),
            )
        except Exception as exc:
            self.log.error("Macro poll error: %s", exc)

    def _fetch_context(self) -> dict:
        import yfinance as yf

        # Fetch equity tickers in one batch; VIX separately (^ prefix causes issues in batch)
        equity_tickers = [t for t in _INDICES if t != "^VIX"] + _SECTOR_ETFS
        data = yf.download(
            equity_tickers, period="5d", interval="1d",
            progress=False, auto_adjust=True, threads=True,
        )

        def pct_change(sym: str) -> float:
            try:
                closes = data["Close"][sym].dropna()
                if len(closes) < 2:
                    return 0.0
                return float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100)
            except Exception:
                return 0.0

        def last(sym: str) -> float:
            try:
                return float(data["Close"][sym].dropna().iloc[-1])
            except Exception:
                return 0.0

        # VIX fetched individually to avoid batch-download column issues
        try:
            vix_data = yf.Ticker("^VIX").history(period="5d", interval="1d")
            vix = float(vix_data["Close"].dropna().iloc[-1]) if not vix_data.empty else 0.0
        except Exception:
            vix = 0.0
        spy_chg = pct_change("SPY")
        qqq_chg = pct_change("QQQ")
        iwm_chg = pct_change("IWM")

        sector_perf = {etf: pct_change(etf) for etf in _SECTOR_ETFS}
        sorted_sectors = sorted(sector_perf.items(), key=lambda x: x[1], reverse=True)
        leading = [s for s, _ in sorted_sectors[:3]]
        lagging = [s for s, _ in sorted_sectors[-3:]]

        if vix >= 30:
            regime = "PANIC"
        elif vix >= 22:
            regime = "RISK_OFF"
        elif vix <= 15 and spy_chg >= 0:
            regime = "RISK_ON"
        else:
            regime = "NEUTRAL"

        # Small-cap participation check (broad vs narrow advance)
        breadth = "BROAD" if abs(iwm_chg - spy_chg) <= 0.8 else "NARROW"
        tech_leading = qqq_chg > spy_chg + 0.3

        return {
            "timestamp": datetime.now(_ET).isoformat(),
            "regime": regime,
            "vix": round(vix, 2),
            "spy_change_pct": round(spy_chg, 2),
            "qqq_change_pct": round(qqq_chg, 2),
            "iwm_change_pct": round(iwm_chg, 2),
            "sector_performance": {k: round(v, 2) for k, v in sector_perf.items()},
            "leading_sectors": leading,
            "lagging_sectors": lagging,
            "breadth": breadth,
            "tech_leading": tech_leading,
        }
