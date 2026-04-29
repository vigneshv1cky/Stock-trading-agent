"""PortfolioAgent — tracks sector concentration and position counts in real time.

Subscribes to: trade.executed, trade.closed
Publishes to:  portfolio.state  (on every trade + every 60 s)

State is also accessible synchronously via PortfolioAgent.current (class-level)
so RiskAgent can read it without subscribing to an event.
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

_PUBLISH_INTERVAL_S = 60

# Simplified GICS sector membership for the watch universe
_SECTOR_MAP: dict[str, list[str]] = {
    "XLK": [
        "AAPL", "MSFT", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "MU", "AMAT", "KLAC",
        "LRCX", "TXN", "ADI", "MRVL", "CRWD", "PANW", "NET", "SNOW", "DDOG", "NOW",
        "TEAM", "SHOP", "FTNT", "OKTA", "ZS", "ARM", "ASML", "TSM", "SMCI", "IRDM",
        "RMBS", "DIOD", "SLAB", "POWI", "LSCC", "SITM", "MTSI", "CRUS", "CRDO", "GFS",
    ],
    "XLF": [
        "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "BX", "KKR", "APO", "ICE",
        "CME", "CBOE", "SPGI", "MCO", "CB", "TRV", "AFL", "MET", "PRU", "PNC",
        "USB", "FITB", "IBKR", "HOOD", "SOFI", "LC", "RKT", "AXP",
    ],
    "XLE": [
        "XOM", "CVX", "COP", "EOG", "OXY", "DVN", "HAL", "SLB", "MPC", "PSX",
        "VLO", "APA", "EQT", "AR", "RRC", "OVV", "CTRA", "MUR", "BP", "EQNR",
        "TTE", "PBR", "SHEL",
    ],
    "XLV": [
        "UNH", "JNJ", "LLY", "ABBV", "PFE", "MRNA", "ISRG", "VKTX", "GERN",
        "LEGN", "TMDX", "IOVA", "CRSP", "BEAM", "GKOS", "TDOC", "DNA",
    ],
    "XLI": [
        "RTX", "LMT", "NOC", "GD", "BA", "HII", "LHX", "LDOS", "BWXT", "KTOS",
        "AXON", "DAL", "UAL", "LUV", "UPS", "FDX", "NSC", "CSX", "UNP", "SAIA",
        "ODFL", "XPO", "HWM", "TDG", "RKLB",
    ],
    "XLC": [
        "META", "GOOGL", "NFLX", "DIS", "SPOT", "SNAP", "PINS", "RDDT", "RBLX",
        "WBD", "FOXA", "BMBL", "MTCH", "DUOL", "SE",
    ],
    "XLY": [
        "AMZN", "TSLA", "HD", "TGT", "NKE", "LULU", "CROX", "DECK", "RIVN",
        "LCID", "NIO", "LI", "XPEV", "BIRK", "SHAK", "BROS", "CAVA",
    ],
    "XLP": [
        "WMT", "COST", "MOS", "BG", "ADM", "CF", "NUE", "CLF", "SBUX",
    ],
    "XLRE": [
        "AMT", "EQIX", "PLD", "IRM", "DLR", "O", "SPG", "VICI", "IIPR", "ZG",
    ],
    "XLB": [
        "FCX", "NEM", "GOLD", "AU", "HMY", "KGC", "RGLD", "FNV", "CCJ",
        "UUUU", "LEU", "CTVA",
    ],
    "XLU": [
        "NEE", "ENPH", "FSLR", "SEDG", "RUN", "PLUG", "BE", "SMR", "OKLO",
    ],
    "CRYPTO": [
        "MSTR", "COIN", "MARA", "RIOT", "CLSK", "WULF", "IREN", "HUT", "CORZ",
        "CIFR", "QUBT", "RGTI", "IONQ",
    ],
}

_SECTOR_CONCENTRATION_WARN = 40.0  # % — log warning above this
_SECTOR_CONCENTRATION_BLOCK = 60.0  # % — RiskAgent should not add more


def get_sector(sym: str) -> str:
    for sector, members in _SECTOR_MAP.items():
        if sym in members:
            return sector
    return "OTHER"


class PortfolioAgent(BaseAgent):
    current: dict = {
        "positions": {},
        "sector_concentration": {},
        "long_count": 0,
        "short_count": 0,
        "total_count": 0,
        "dominant_sector": None,
        "max_concentration_pct": 0.0,
        "timestamp": "",
    }

    def __init__(self, bus: EventBus):
        super().__init__(bus, "PortfolioAgent")
        self._trade_queue = bus.subscribe("trade.executed", "trade.closed")
        self._positions: dict[str, dict] = {}
        self._last_warn: dict[str, float] = {}  # sector → last warned concentration

    async def run(self) -> None:
        self._load_from_cache()
        await asyncio.gather(
            self._consume_trades(),
            self._publish_loop(),
        )

    def _load_from_cache(self) -> None:
        import json
        import os
        path = os.path.expanduser("~/.stock_screener/held_cache.json")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    held = json.load(f)
                for sym, data in held.items():
                    self._positions[sym] = {
                        "direction": data.get("direction", "LONG"),
                        "sector": get_sector(sym),
                        "archetype": "",
                    }
                self._update_state()
                self.log.info("PortfolioAgent: seeded %d positions from cache", len(self._positions))
        except Exception as exc:
            self.log.warning("PortfolioAgent cache load failed: %s", exc)

    async def _consume_trades(self) -> None:
        while True:
            msg = await self._trade_queue.get()
            topic = msg["topic"]
            data = msg["data"]
            sym = data.get("symbol")
            if not sym:
                continue

            if topic == "trade.executed":
                self._positions[sym] = {
                    "direction": data.get("direction", "LONG"),
                    "sector": get_sector(sym),
                    "archetype": data.get("archetype", ""),
                }
            elif topic == "trade.closed":
                self._positions.pop(sym, None)

            self._update_state()
            await self.bus.publish("portfolio.state", PortfolioAgent.current)

    async def _publish_loop(self) -> None:
        while True:
            await asyncio.sleep(_PUBLISH_INTERVAL_S)
            self._update_state()
            if self._positions:
                await self.bus.publish("portfolio.state", PortfolioAgent.current)

    def _update_state(self) -> None:
        long_count = sum(1 for p in self._positions.values() if p["direction"] == "LONG")
        short_count = sum(1 for p in self._positions.values() if p["direction"] == "SHORT")
        total = len(self._positions)

        sector_counts: dict[str, int] = {}
        for pos in self._positions.values():
            sec = pos["sector"]
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        dominant = max(sector_counts, key=sector_counts.__getitem__) if sector_counts else None
        max_conc = (sector_counts[dominant] / max(total, 1) * 100) if dominant else 0.0

        PortfolioAgent.current = {
            "positions": dict(self._positions),
            "sector_concentration": {
                s: round(c / max(total, 1) * 100, 1)
                for s, c in sector_counts.items()
            },
            "long_count": long_count,
            "short_count": short_count,
            "total_count": total,
            "dominant_sector": dominant,
            "max_concentration_pct": round(max_conc, 1),
            "timestamp": datetime.now(_ET).isoformat(),
        }

        if dominant and max_conc >= _SECTOR_CONCENTRATION_WARN and total > 0:
            if abs(max_conc - self._last_warn.get(dominant, 0.0)) >= 5.0:
                self._last_warn[dominant] = max_conc
                self.log.warning(
                    "Sector concentration: %s at %.0f%% (%d/%d positions)",
                    dominant, max_conc, sector_counts.get(dominant, 0), total,
                )
