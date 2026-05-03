"""Orchestrator — creates all agents, wires them to the shared EventBus, and runs them.

Usage:
    asyncio.run(Orchestrator().run())
    asyncio.run(Orchestrator(dry_run=True).run())   # no trade execution

Agent pipeline:
    WatcherAgent        → market.signal        (RVOL + price gate, reactive)
    ScannerAgent        → market.signal        (batch scan every 15 min, proactive)
    ScreenerAgent       → symbol.screened      (qualification + regime gates)
    ResearchAgent       → symbol.researched    (technicals: RSI, BB, MACD, ATR)
    NewsAgent           → symbol.analysed      (Polygon + Haiku sentiment)
    PredictorAgent      → symbol.predicted     (Haiku 4-step conviction score)
    PortfolioAgent      → portfolio.state      (sector concentration tracking)
    RiskAgent           → trade.approved       (Gate 1 Python + Haiku Gates 2-4)
    ExecutorAgent       → trade.executed / trade.closed
    LearningAgent       → memory.updated       (Haiku reflection, global lessons)
    MonitorAgent        → position.alert       (earnings + re-eval)
"""

import asyncio
import logging
import signal

from .crypto_watcher import CryptoWatcherAgent
from .event_bus import EventBus
from .executor import ExecutorAgent
from .learning import LearningAgent
from .memory import AgentMemory
from .monitor import MonitorAgent
from .news import NewsAgent
from .news_watcher import NewsWatcherAgent
from .portfolio import PortfolioAgent
from .predictor import PredictorAgent
from .research import ResearchAgent
from .risk import RiskAgent
from .scanner import ScannerAgent
from .screener import ScreenerAgent
from .watcher import WatcherAgent

log = logging.getLogger("Orchestrator")

SCREEN_UNIVERSE = [
    "PLTR", "SNAP", "U", "PINS", "RBLX", "PATH", "DDOG", "NET", "CRWD", "ZS",
    "MDB", "SNOW", "ROKU", "HOOD", "SOFI", "AFRM", "UPST", "IONQ", "RGTI", "QUBT",
    "LUNR", "RKLB", "ASTS", "AMD", "INTC", "QCOM", "MU", "MRVL", "ON", "SMCI",
    "UBER", "LYFT", "DASH", "ABNB", "TWLO", "OKTA", "ESTC", "DOCN", "BRZE", "MNDY",
    "GLBE", "GLOB", "TOST", "GTLB", "IOT", "AI", "BBAI", "SOUN", "GRAB", "SE",
    "SHOP", "SPOT", "OPEN", "DUOL", "BILL", "PCOR", "DT", "FRSH", "TENB", "RPD",
    "CRDO", "ANET", "PANW", "FTNT", "S", "QLYS", "CHKP", "GEN", "NVDA", "TSM",
    "AVGO", "ASML", "AMAT", "LRCX", "KLAC", "ADI", "TXN", "WOLF", "SLAB", "ACLS",
    "RMBS", "DIOD", "INDI", "SITM", "CRUS", "LSCC", "MTSI", "NVTS", "POWI",
    "V", "MA", "GS", "JPM", "BAC", "MS", "C", "WFC", "AXP", "BLK",
    "MSTR", "COIN", "WULF", "IREN", "MARA", "RIOT", "CLSK", "HUT", "CORZ", "CIFR",
    "PYPL", "NU", "IBKR", "ALLY", "STNE", "LC", "RKT", "PGR", "TRV", "CB",
    "AFL", "MET", "PRU", "BX", "KKR", "APO", "CME", "ICE", "CBOE",
    "SPGI", "MCO", "USB", "PNC", "TFC", "FITB",
    "LMT", "RTX", "NOC", "GD", "BA", "LHX", "HWM", "TDG", "HII", "LDOS", "BWXT",

    "AXON", "IRDM", "KTOS",
    "XOM", "CVX", "COP", "OXY", "EOG", "SLB", "PBR", "TTE", "SHEL", "BP", "EQNR",
    "MPC", "PSX", "VLO", "APA", "MUR", "DVN", "HAL", "OVV", "CTRA", "AR", "RRC", "EQT",
    "CTVA", "CF", "MOS", "ADM", "BG",
    "NEE", "FSLR", "ENPH", "PLUG", "RUN", "SEDG", "ARRY", "BE", "VIST", "SMR",
    "OKLO", "LEU", "CCJ", "UUUU",
    "ZIM", "MATX", "GNK", "DSX", "STNG", "FRO", "DAC", "EGLE", "SBLK",
    "FCX", "AA", "CLF", "NUE", "NEM", "GOLD", "AU", "HMY", "KGC", "RGLD", "FNV",
    "TSLA", "RACE", "UPS", "FDX", "NSC", "CSX", "UNP", "LUV", "DAL", "UAL", "AAL",
    "F", "GM", "RIVN", "LCID", "NIO", "XPEV", "LI", "JOBY", "ACHR", "PSNY",
    "PFE", "JNJ", "ABBV", "LLY", "UNH", "MRNA", "HIMS", "DOCS", "TDOC", "DNA",
    "BEAM", "CRSP", "VKTX", "LEGN", "GERN", "IOVA", "CORT", "ISRG",
    "NKE", "SBUX", "DIS", "AAPL", "AMZN", "WMT", "COST", "HD", "TGT", "TJX",
    "CAVA", "BIRK", "SHAK", "BROS", "WING", "LULU", "DECK", "CROX",
    "PLD", "AMT", "EQIX", "DLR", "IRM", "IIPR", "VICI", "O", "SPG",
    "META", "GOOGL", "NFLX", "TTD", "RDDT", "DJT", "WBD", "FOXA", "MTCH", "BMBL",
    "ZG", "APP", "CELH", "ARM", "TMDX", "GKOS",
    "SAIA", "ODFL", "XPO", "PAYC", "WIX", "GFS", "NOW", "WDAY", "HUBS", "TEAM",
    "MSFT", "ORCL", "IBM", "CRM", "ADBE", "INTU", "CSCO", "ACN", "DELL", "CDNS",
    "SNPS", "HPQ", "HPE",
    "PG", "KO", "PEP", "MO", "PM", "CL", "GIS", "MDLZ", "KHC", "HRL", "CLX", "KMB",
    "CVS", "CI", "HCA", "MDT", "ABT", "BMY", "GILD", "BIIB", "REGN", "AMGN",
    "TMO", "DHR", "BSX", "EW", "DXCM", "RMD", "ZTS", "IDXX", "IQV", "BDX", "ZBH",
    "ELV", "HUM", "CNC",
    "VRTX", "ILMN", "ALNY", "INCY", "BMRN", "SRPT", "RARE",
    "CAT", "DE", "HON", "GE", "ETN", "ROK", "ITW", "PH", "IR", "AME", "XYL",
    "DOV", "GNRC", "ROP", "FTV", "EMR", "MMM", "JCI", "CARR", "OTIS", "TT",
    "CPRT", "VRSK", "CTAS",
    "DHI", "LEN", "PHM", "TOL",
    "COF", "SYF", "RF", "HBAN", "KEY", "MTB", "SCHW", "TROW", "IVZ", "BEN",
    "FIS", "FISV", "GPN", "WU",
    "ET", "KMI", "WMB", "OKE", "FANG",
    "VST", "NRG", "AES",
    "LIN", "APD", "ECL", "PPG", "SHW", "DD", "IFF", "EMN",
    "LOW", "DLTR", "DG", "KR", "MCD", "YUM", "CMG", "QSR", "DPZ", "WEN",
    "MAR", "HLT", "H", "MGM", "LVS", "WYNN", "CZR", "RCL", "CCL", "NCLH",
    "EA", "TTWO", "DKNG", "LYV",
    "PSA", "EXR", "AVB", "EQR", "WELL", "VTR", "CCI",
    "VZ", "T", "TMUS",
    "MRK", "AZN", "NVO",
    "VEEV", "ASAN", "BOX", "SPT",
    "MCHP", "SWKS", "MPWR", "NXPI",
    "BIDU", "JD", "PDD", "BABA", "MELI",
    "ZM", "DOCU", "RNG",
    "URI", "PCAR", "FAST", "RSG", "WCN",
    "WAL", "ZION",
    "BURL", "FIVE",
    "EXEL", "ACAD",
    "CNQ", "CVE",
    "HEI", "PRGO", "JAZZ",
    "TKO", "CVNA",
]

CRYPTO_UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD",
    "LTC/USD", "XRP/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "DOT/USD",
    "MATIC/USD", "MKR/USD", "CRV/USD", "GRT/USD", "BAT/USD", "SHIB/USD",
]


class Orchestrator:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.bus = EventBus()
        self.memory = AgentMemory()
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    async def run(self) -> None:
        stock_symbols = list(dict.fromkeys(SCREEN_UNIVERSE))
        crypto_symbols = list(dict.fromkeys(CRYPTO_UNIVERSE))

        from .base import BaseAgent
        agents: list[BaseAgent] = [
            WatcherAgent(self.bus, stock_symbols),
            ScannerAgent(self.bus, stock_symbols),
            CryptoWatcherAgent(self.bus, crypto_symbols),
            NewsWatcherAgent(self.bus, stock_symbols),
            ScreenerAgent(self.bus),
            ResearchAgent(self.bus),
            NewsAgent(self.bus),
            PredictorAgent(self.bus, self.memory),
            PortfolioAgent(self.bus),
            RiskAgent(self.bus),
            ExecutorAgent(self.bus, dry_run=self.dry_run),
            LearningAgent(self.bus, self.memory),
            MonitorAgent(self.bus),
        ]

        self._tasks = [
            asyncio.create_task(agent.safe_run(), name=agent.name)
            for agent in agents
        ]

        run_mode = "DRY-RUN" if self.dry_run else "LIVE"
        log.info(
            "Multi-agent trading system started [%s] — %d agents, %d stocks, %d crypto",
            run_mode, len(agents), len(stock_symbols), len(crypto_symbols),
        )  # agent count is now dynamic — len(agents) is the source of truth

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass  # Windows

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            log.info("All agents cancelled — shutdown complete")

    def _request_shutdown(self) -> None:
        log.info("Shutdown signal received — cancelling agents…")
        for task in self._tasks:
            task.cancel()
