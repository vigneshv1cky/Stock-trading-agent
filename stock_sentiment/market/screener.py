"""Stock screener: finds stocks with strong 3-month performance."""

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

    # ===== Cybersecurity (High War Sensitivity) =====
    "PANW", "FTNT", "CRWD", "ZS", "CYBR", "S", "QLYS", "CHKP", "AVAST", "GEN",

    # ===== Semiconductors =====
    "NVDA", "TSM", "AVGO", "ASML", "AMAT", "LRCX", "KLAC", "MU", "ADI", "TXN",
    "WOLF", "SLAB", "ACLS", "RMBS", "DIOD", "INDI", "SITM", "CRUS",
    "LSCC", "MTSI", "NVTS", "POWI",

    # ===== Fintech / Finance / Crypto =====
    "SQ", "V", "MA", "GS", "JPM", "BAC", "MS", "C", "WFC", "AXP", "BLK",
    "MSTR", "COIN", "WULF", "IREN", "MARA", "RIOT", "CLSK", "BITF", "HUT",
    "CORZ", "CIFR", "PYPL", "NU", "HOOD", "SOFI", "IBKR", "ALLY", "STNE",

    # ===== Aerospace / Defense (Direct Beneficiaries) =====
    "LMT", "RTX", "NOC", "GD", "BA", "LHX", "HWM", "TDG", "HII", "LDOS",
    "BWXT", "TEXT", "HEI", "CAE", "AVAV", "BCO", "SPR", "AXON", "RKLB", 
    "IRDM", "SPCE", "RDW", "KTOS",

    # ===== Energy / Oil & Gas (War Catalyst) =====
    "XOM", "CVX", "COP", "OXY", "EOG", "SLB", "PBR", "TTE", "SHEL", "BP", "EQNR",
    "MPC", "PSX", "VLO", "APA", "MUR", "DVN", "HAL", "OVV", "CTRA", "AR", "RRC", 
    "EQT", "CTVA", "CF", "MOS", "ADM", "BG",

    # ===== Clean Energy / Solar / Nuclear =====
    "NEE", "FSLR", "ENPH", "PLUG", "RUN", "SEDG", "ARRY", "BE", "VIST", "SMR", 
    "OKLO", "LEU", "CCJ", "UUUU",

    # ===== Shipping & Maritime (Supply Chain Impact) =====
    "ZIM", "MATX", "GNK", "DSX", "STNG", "FRO", "DAC", "EGLE", "SBLK", "NM",

    # ===== Commodities / Materials / Gold =====
    "FCX", "AA", "CLF", "NUE", "NEM", "GOLD", "AU", "HMY", "KGC", "RGLD", "FNV",

    # ===== EV / Auto / Transport =====
    "TSLA", "RACE", "UPS", "FDX", "NSC", "CSX", "UNP", "LUV", "DAL", "UAL", "AAL",
    "F", "GM", "RIVN", "LCID", "NIO", "XPEV", "LI", "JOBY", "ACHR", "PSNY",

    # ===== Healthcare / Biotech =====
    "PFE", "JNJ", "ABBV", "LLY", "UNH", "MRNA", "HIMS", "DOCS", "TDOC", "DNA", 
    "BEAM", "CRSP", "VKTX", "LEGN", "GERN", "IOVA", "CORT", "ISRG",

    # ===== Consumer / Retail / Brands =====
    "NKE", "SBUX", "DIS", "AAPL", "AMZN", "WMT", "COST", "HD", "TGT", "TJX",
    "CAVA", "BIRK", "SHAK", "BROS", "WING", "LULU", "DECK", "CROX",

    # ===== Real Estate / REITs / Data =====
    "PLD", "AMT", "EQIX", "DLR", "IRM", "IIPR", "VICI", "O", "SPG",

    # ===== Media / Social / AdTech =====
    "META", "GOOGL", "NFLX", "SPOT", "TTD", "RDDT", "DJT", "WBD", "PARA", "FOXA",
    "MTCH", "BMBL", "ZG",

    # ===== Growth & Mid-caps =====
    "APP", "CELH", "ARM", "TMDX", "GKOS", "ACLX", "SAIA", "ODFL", "XPO",
    "PAYC", "WIX", "GFS", "FTNT", "PANW", "CYBR", "NOW", "WDAY", "HUBS", "TEAM",
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
    """Screens for stocks with strong 3-month performance."""

    def __init__(
        self,
        min_3m_return: float = 10.0,  # Minimum 10% gain in 3 months
        top_n: int = 30,  # Return top N stocks
    ):
        self.min_3m_return = min_3m_return
        self.top_n = top_n

    def screen(self, universe: list[str] = None) -> list[ScreenedStock]:
        """Screen stocks and return those meeting criteria, sorted by 3m return."""
        import yfinance as yf

        symbols = universe or SCREEN_UNIVERSE
        print(f"[StockScreener] Filtering {len(symbols)} stocks in universe...")
        console.print(
            f"[cyan]Screening {len(symbols)} stocks "
            f"(3-month return > {self.min_3m_return}%)...[/cyan]"
        )

        # Batch download 3 months of data
        print("[StockScreener] Downloading 3-month OHLCV data via yfinance...")
        try:
            data = yf.download(
                symbols,
                period="3mo",
                group_by="ticker",
                progress=False,
                threads=True,
            )
        except Exception as e:
            print(f"[StockScreener] CRITICAL: Batch download failed: {e}")
            console.print(f"[red]Screen failed: {e}[/red]")
            return []

        if data.empty:
            print("[StockScreener] Downloaded data is empty.")
            return []

        print(f"[StockScreener] Processing momentum metrics for each stock...")
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

                # Filter: minimum price
                if current_price < 1.0:
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

        print(f"[StockScreener] Found {len(results)} stocks passing all momentum filters.")
        console.print(
            f"  [green]Found {len(results)} stocks matching criteria[/green]"
        )
        return results
