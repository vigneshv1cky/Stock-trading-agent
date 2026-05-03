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
        # Mega-cap tech
        "AAPL", "MSFT", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "MU", "AMAT", "KLAC",
        "LRCX", "TXN", "ADI", "MRVL", "CRWD", "PANW", "NET", "SNOW", "DDOG", "NOW",
        "TEAM", "SHOP", "FTNT", "OKTA", "ZS", "ARM", "ASML", "TSM", "SMCI", "IRDM",
        "RMBS", "DIOD", "SLAB", "POWI", "LSCC", "SITM", "MTSI", "CRUS", "CRDO", "GFS",
        # Large-cap software/cloud
        "ORCL", "IBM", "CRM", "ADBE", "INTU", "CSCO", "ACN", "DELL", "CDNS", "SNPS",
        "HPQ", "HPE", "PAYC", "WIX", "WDAY", "HUBS", "APP",
        # Semis extras
        "ON", "WOLF", "ACLS", "INDI", "NVTS",
        # Security
        "S", "QLYS", "CHKP", "GEN", "TENB", "RPD", "ANET",
        # SaaS/cloud extras
        "PLTR", "U", "PATH", "MDB", "ROKU", "TWLO", "ESTC", "DOCN", "BRZE", "MNDY",
        "GLBE", "GLOB", "TOST", "GTLB", "IOT", "AI", "BBAI", "SOUN", "BILL", "PCOR",
        "DT", "FRSH",
    ],
    "XLF": [
        "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "BX", "KKR", "APO", "ICE",
        "CME", "CBOE", "SPGI", "MCO", "CB", "TRV", "AFL", "MET", "PRU", "PNC",
        "USB", "FITB", "IBKR", "HOOD", "SOFI", "LC", "RKT", "AXP",
        # Payments
        "V", "MA", "PYPL", "NU", "AFRM", "UPST",
        # Banks/insurance extras
        "BRK-B", "TFC", "COF", "SYF", "RF", "HBAN", "WAL", "ZION", "ALLY", "STNE",
        "PGR",
    ],
    "XLE": [
        "XOM", "CVX", "COP", "EOG", "OXY", "DVN", "HAL", "SLB", "MPC", "PSX",
        "VLO", "APA", "EQT", "AR", "RRC", "OVV", "CTRA", "MUR", "BP", "EQNR",
        "TTE", "PBR", "SHEL",
        # Canadian energy (NYSE-listed)
        "CNQ", "CVE", "VIST",
    ],
    "XLV": [
        "UNH", "JNJ", "LLY", "ABBV", "PFE", "MRNA", "ISRG", "VKTX", "GERN",
        "LEGN", "TMDX", "IOVA", "CRSP", "BEAM", "GKOS", "TDOC", "DNA",
        # Managed care
        "ELV", "HUM", "CNC", "CVS", "CI",
        # Large pharma / biotech
        "BMY", "GILD", "BIIB", "REGN", "AMGN", "VRTX", "ILMN", "ALNY", "INCY",
        "BMRN", "SRPT", "RARE", "EXEL", "ACAD", "PRGO", "JAZZ", "HIMS", "DOCS", "CORT",
        # MedTech
        "MDT", "ABT", "TMO", "DHR", "BSX", "EW", "DXCM", "RMD", "ZTS", "IDXX",
        "IQV", "BDX", "ZBH", "HOLX", "HCA",
    ],
    "XLI": [
        "RTX", "LMT", "NOC", "GD", "BA", "HII", "LHX", "LDOS", "BWXT", "KTOS",
        "AXON", "DAL", "UAL", "LUV", "UPS", "FDX", "NSC", "CSX", "UNP", "SAIA",
        "ODFL", "XPO", "HWM", "TDG", "RKLB",
        # Airlines
        "AAL",
        # Industrials large-cap
        "CAT", "DE", "HON", "GE", "ETN", "ROK", "ITW", "PH", "IR", "AME", "XYL",
        "DOV", "GNRC", "ROP", "FTV", "EMR", "MMM", "JCI", "CARR", "OTIS", "TT",
        "CPRT", "VRSK", "CTAS", "HEI",
        # Shipping
        "ZIM", "MATX", "GNK", "DSX", "STNG", "FRO", "DAC", "EGLE",
        # eVTOL
        "JOBY", "ACHR",
    ],
    "XLC": [
        "META", "GOOGL", "NFLX", "DIS", "SPOT", "SNAP", "PINS", "RDDT", "RBLX",
        "WBD", "FOXA", "BMBL", "MTCH", "DUOL", "SE",
        # Extras
        "TTD", "DJT", "TKO", "GRAB",
    ],
    "XLY": [
        "AMZN", "TSLA", "RACE", "HD", "TGT", "NKE", "LULU", "CROX", "DECK", "RIVN",
        "LCID", "NIO", "LI", "XPEV", "BIRK", "SHAK", "BROS", "CAVA",
        # Autos
        "F", "GM", "PSNY",
        # Travel/leisure
        "ABNB", "BKNG", "EXPE",
        # Cruise lines
        "NCLH", "RCL", "CCL",
        # Hotels
        "MAR", "HLT", "H",
        # Casinos / gaming
        "MGM", "WYNN", "LVS", "CZR",
        # Restaurants
        "DPZ", "QSR", "YUM", "WEN",
        # Retail
        "TJX", "WING", "BURL", "FIVE",
        # Homebuilders
        "DHI", "LEN", "PHM", "TOL",
        # Used-car / proptech
        "CVNA", "OPEN",
    ],
    "XLP": [
        "WMT", "COST", "MOS", "BG", "ADM", "CF", "SBUX",
        # Consumer staples large-cap
        "PG", "KO", "PEP", "MO", "PM", "CL", "GIS", "MDLZ", "KHC", "HRL", "CLX", "KMB",
        "CELH",
    ],
    "XLRE": [
        "AMT", "EQIX", "PLD", "IRM", "DLR", "O", "SPG", "VICI", "IIPR", "ZG",
    ],
    "XLB": [
        "FCX", "NEM", "GOLD", "AU", "HMY", "KGC", "RGLD", "FNV", "CCJ",
        "UUUU", "LEU", "CTVA",
        # Steel / aluminum
        "NUE", "CLF", "AA",
    ],
    "XLU": [
        "NEE", "ENPH", "FSLR", "SEDG", "RUN", "PLUG", "BE", "SMR", "OKLO", "ARRY",
    ],
    "CRYPTO": [
        "MSTR", "COIN", "MARA", "RIOT", "CLSK", "WULF", "IREN", "HUT", "CORZ",
        "CIFR", "QUBT", "RGTI", "IONQ",
    ],
}

_SECTOR_CONCENTRATION_WARN = 40.0  # % — log warning above this
_SECTOR_CONCENTRATION_BLOCK = 60.0  # % — RiskAgent should not add more


def get_sector(sym: str) -> str:
    if "/" in sym:
        return "CRYPTO"
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
                }
            elif topic == "trade.closed":
                self._positions.pop(sym, None)

            self._update_state()
            await self.bus.publish("portfolio.state", PortfolioAgent.current)
            self._log_book_summary()

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

    def _log_book_summary(self) -> None:
        state = PortfolioAgent.current
        total = state["total_count"]
        if total == 0:
            self.log.info("Portfolio: empty")
            return
        longs = [s for s, p in state["positions"].items() if p["direction"] == "LONG"]
        shorts = [s for s, p in state["positions"].items() if p["direction"] == "SHORT"]
        top_sectors = sorted(
            state["sector_concentration"].items(), key=lambda x: x[1], reverse=True
        )[:4]
        sector_str = " ".join(f"{s}={v:.0f}%" for s, v in top_sectors)
        self.log.info(
            "Portfolio: %dL %dS | longs=[%s] shorts=[%s] | %s",
            len(longs), len(shorts),
            ",".join(longs), ",".join(shorts),
            sector_str or "no sectors",
        )
