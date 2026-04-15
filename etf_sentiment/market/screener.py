"""Stock screener: finds stocks under $100 with strong 3-month performance."""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from rich.console import Console

console = Console()

# Broad universe of ~250 liquid stocks to screen from.
# Covers S&P 500 components, mid-caps, small-caps, and popular growth/meme stocks.
SCREEN_UNIVERSE = [
    # ===== Tech / Software / Cloud =====
    "PLTR", "SNAP", "U", "PINS", "RBLX", "PATH", "DDOG", "NET", "CRWD",
    "ZS", "MDB", "SNOW", "ROKU", "HOOD", "SOFI", "AFRM", "UPST",
    "IONQ", "RGTI", "QUBT", "LUNR", "RKLB", "ASTS",
    "AMD", "INTC", "QCOM", "MU", "MRVL", "ON", "SMCI",
    "UBER", "LYFT", "DASH", "ABNB",
    "TWLO", "OKTA", "CFLT", "ESTC", "DOCN", "BRZE", "MNDY",
    "GLBE", "GLOB", "TOST", "GTLB", "IOT", "AI", "BBAI", "SOUN", "GRAB",
    "SE", "SHOP", "SPOT", "OPEN",
    "DUOL", "BILL", "PCOR", "DT", "FRSH", "TENB", "RPD", "CRDO", "ANET",

    # ===== Semiconductors =====
    "WOLF", "SLAB", "ACLS", "RMBS", "DIOD", "INDI", "SITM", "CRUS",
    "LSCC", "MTSI", "NVTS", "POWI",

    # ===== Fintech / Finance / Crypto =====
    "PYPL", "COIN", "NU", "MARA", "RIOT", "CLSK", "BITF", "HUT",
    "CORZ", "CIFR", "XYZ", "ALLY", "LC", "LPLA",
    "FOUR", "STNE", "PAGS", "VIRT", "IBKR",

    # ===== Healthcare / Biotech =====
    "MRNA", "HIMS", "DOCS", "TDOC", "DNA", "BEAM", "CRSP",
    "INSM", "SAVA", "ARDX", "VKTX", "LEGN", "GERN", "IOVA",
    "RXRX", "ACAD", "ARCT", "EXAS", "NUVB",
    "CORT", "FOLD", "TGTX", "ARVN", "ALNY", "PCVX",
    "IRTC", "ISRG", "HALO", "INSP", "PODD",

    # ===== Consumer / Retail / Restaurants =====
    "NKE", "SBUX", "DIS", "NCLH", "CCL", "RCL",
    "GME", "AMC", "CHWY", "ETSY", "W", "RVLV",
    "CAVA", "BIRK", "CART", "SHAK", "BROS", "WING",
    "LULU", "DECK", "CROX", "HAS", "MAT",
    "BKNG", "EXPE", "LYV", "DKNG", "PENN", "MGM",
    "WYNN", "CZR",

    # ===== Energy / Clean Energy =====
    "FSLR", "ENPH", "PLUG", "RUN", "SEDG", "ARRY",
    "DVN", "HAL", "OVV", "CTRA", "AR", "RRC", "EQT",
    "CLNE", "CHPT", "EVGO", "BLDP", "BE",

    # ===== EV / Auto / Transport =====
    "F", "GM", "RIVN", "LCID", "NIO", "XPEV", "LI",
    "JOBY", "ACHR", "PSNY", "NKLA",
    "ZIM", "MATX", "DAL", "UAL", "AAL", "JBLU",

    # ===== Industrials / Aerospace / Defense =====
    "AXON", "RKLB", "IRDM", "SPCE", "RDW",
    "BWA", "TER", "GNRC", "BLDR",
    "KTOS",

    # ===== Real Estate / REITs =====
    "IRM", "IIPR", "GOOD", "NLY", "AGNC",

    # ===== Media / Social / Entertainment =====
    "RDDT", "DJT", "WBD",
    "MTCH", "BMBL", "ZG",
    "NFLX", "SPOT",

    # ===== Mid-cap Growth / Other =====
    "APP", "TTD", "CELH", "ARM",
    "TMDX", "GKOS", "ACLX", "SAIA", "ODFL", "XPO",
    "PAYC", "WIX", "GFS",
    "FTNT", "PANW", "CYBR",
]


@dataclass
class ScreenedStock:
    """A stock that passed the screening criteria."""
    symbol: str
    current_price: float
    price_3m_ago: float
    change_3m_pct: float  # 3-month return
    change_1m_pct: float  # 1-month return
    change_1w_pct: float  # 1-week return
    high_3m: float
    low_3m: float
    avg_volume: float
    current_volume: int
    daily_closes_3m: list  # For sparkline


class StockScreener:
    """Screens for stocks under $100 with strong 3-month performance."""

    def __init__(
        self,
        max_price: float = 100.0,
        min_3m_return: float = 10.0,  # Minimum 10% gain in 3 months
        top_n: int = 30,  # Return top N stocks
    ):
        self.max_price = max_price
        self.min_3m_return = min_3m_return
        self.top_n = top_n

    def screen(self, universe: list[str] = None) -> list[ScreenedStock]:
        """Screen stocks and return those meeting criteria, sorted by 3m return."""
        import yfinance as yf

        symbols = universe or SCREEN_UNIVERSE
        console.print(
            f"[cyan]Screening {len(symbols)} stocks "
            f"(price < ${self.max_price}, 3-month return > {self.min_3m_return}%)...[/cyan]"
        )

        # Batch download 3 months of data
        try:
            data = yf.download(
                symbols,
                period="3mo",
                group_by="ticker",
                progress=False,
                threads=True,
            )
        except Exception as e:
            console.print(f"[red]Screen failed: {e}[/red]")
            return []

        if data.empty:
            return []

        results = []
        for symbol in symbols:
            try:
                if len(symbols) == 1:
                    df = data.copy()
                else:
                    df = data[symbol].copy()

                df = df.dropna(how="all")
                if df.empty or len(df) < 10:
                    continue

                # Flatten MultiIndex if needed
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                closes = df["Close"].astype(float)
                current_price = float(closes.iloc[-1])

                # Filter: must be under max_price
                if current_price > self.max_price or current_price < 1.0:
                    continue

                # 3-month return
                price_3m_ago = float(closes.iloc[0])
                if price_3m_ago <= 0:
                    continue
                change_3m = ((current_price - price_3m_ago) / price_3m_ago) * 100

                # Filter: must have minimum return
                if change_3m < self.min_3m_return:
                    continue

                # 1-month return (last ~21 trading days)
                idx_1m = max(0, len(closes) - 21)
                price_1m = float(closes.iloc[idx_1m])
                change_1m = ((current_price - price_1m) / price_1m) * 100 if price_1m > 0 else 0

                # 1-week return (last 5 trading days)
                idx_1w = max(0, len(closes) - 5)
                price_1w = float(closes.iloc[idx_1w])
                change_1w = ((current_price - price_1w) / price_1w) * 100 if price_1w > 0 else 0

                # High/low
                high_3m = float(df["High"].astype(float).max())
                low_3m = float(df["Low"].astype(float).min())

                # Volume
                volumes = df["Volume"].astype(float)
                avg_vol = float(volumes.tail(20).mean())
                cur_vol = int(volumes.iloc[-1]) if not pd.isna(volumes.iloc[-1]) else 0

                results.append(ScreenedStock(
                    symbol=symbol,
                    current_price=current_price,
                    price_3m_ago=price_3m_ago,
                    change_3m_pct=change_3m,
                    change_1m_pct=change_1m,
                    change_1w_pct=change_1w,
                    high_3m=high_3m,
                    low_3m=low_3m,
                    avg_volume=avg_vol,
                    current_volume=cur_vol,
                    daily_closes_3m=[float(v) for v in closes.values],
                ))

            except Exception:
                continue

        # Sort by 3-month return, take top N
        results.sort(key=lambda s: s.change_3m_pct, reverse=True)
        results = results[:self.top_n]

        console.print(
            f"  [green]Found {len(results)} stocks matching criteria[/green]"
        )
        return results
