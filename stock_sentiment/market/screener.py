"""Stock screener: filters and classifies stocks for the decision engine."""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from rich.console import Console

console = Console()

SCREEN_UNIVERSE = [
    # ... (Keep the large SCREEN_UNIVERSE)
    "PLTR",
    "SNAP",
    "U",
    "PINS",
    "RBLX",
    "PATH",
    "DDOG",
    "NET",
    "CRWD",
    "ZS",
    "MDB",
    "SNOW",
    "ROKU",
    "HOOD",
    "SOFI",
    "AFRM",
    "UPST",
    "IONQ",
    "RGTI",
    "QUBT",
    "LUNR",
    "RKLB",
    "ASTS",
    "AMD",
    "INTC",
    "QCOM",
    "MU",
    "MRVL",
    "ON",
    "SMCI",
    "UBER",
    "LYFT",
    "DASH",
    "ABNB",
    "TWLO",
    "OKTA",
    "CFLT",
    "ESTC",
    "DOCN",
    "BRZE",
    "MNDY",
    "GLBE",
    "GLOB",
    "TOST",
    "GTLB",
    "IOT",
    "AI",
    "BBAI",
    "SOUN",
    "GRAB",
    "SE",
    "SHOP",
    "SPOT",
    "OPEN",
    "DUOL",
    "BILL",
    "PCOR",
    "DT",
    "FRSH",
    "TENB",
    "RPD",
    "CRDO",
    "ANET",
    "PANW",
    "FTNT",
    "CYBR",
    "S",
    "QLYS",
    "CHKP",
    "AVAST",
    "GEN",
    "NVDA",
    "TSM",
    "AVGO",
    "ASML",
    "AMAT",
    "LRCX",
    "KLAC",
    "ADI",
    "TXN",
    "WOLF",
    "SLAB",
    "ACLS",
    "RMBS",
    "DIOD",
    "INDI",
    "SITM",
    "CRUS",
    "LSCC",
    "MTSI",
    "NVTS",
    "POWI",
    "SQ",
    "V",
    "MA",
    "GS",
    "JPM",
    "BAC",
    "MS",
    "C",
    "WFC",
    "AXP",
    "BLK",
    "MSTR",
    "COIN",
    "WULF",
    "IREN",
    "MARA",
    "RIOT",
    "CLSK",
    "BITF",
    "HUT",
    "CORZ",
    "CIFR",
    "PYPL",
    "NU",
    "IBKR",
    "ALLY",
    "STNE",
    "LMT",
    "RTX",
    "NOC",
    "GD",
    "BA",
    "LHX",
    "HWM",
    "TDG",
    "HII",
    "LDOS",
    "BWXT",
    "TEXT",
    "HEI",
    "CAE",
    "AVAV",
    "BCO",
    "SPR",
    "AXON",
    "RKLB",
    "IRDM",
    "SPCE",
    "RDW",
    "KTOS",
    "XOM",
    "CVX",
    "COP",
    "OXY",
    "EOG",
    "SLB",
    "PBR",
    "TTE",
    "SHEL",
    "BP",
    "EQNR",
    "MPC",
    "PSX",
    "VLO",
    "APA",
    "MUR",
    "DVN",
    "HAL",
    "OVV",
    "CTRA",
    "AR",
    "RRC",
    "EQT",
    "CTVA",
    "CF",
    "MOS",
    "ADM",
    "BG",
    "NEE",
    "FSLR",
    "ENPH",
    "PLUG",
    "RUN",
    "SEDG",
    "ARRY",
    "BE",
    "VIST",
    "SMR",
    "OKLO",
    "LEU",
    "CCJ",
    "UUUU",
    "ZIM",
    "MATX",
    "GNK",
    "DSX",
    "STNG",
    "FRO",
    "DAC",
    "EGLE",
    "SBLK",
    "NM",
    "FCX",
    "AA",
    "CLF",
    "NUE",
    "NEM",
    "GOLD",
    "AU",
    "HMY",
    "KGC",
    "RGLD",
    "FNV",
    "TSLA",
    "RACE",
    "UPS",
    "FDX",
    "NSC",
    "CSX",
    "UNP",
    "LUV",
    "DAL",
    "UAL",
    "AAL",
    "F",
    "GM",
    "RIVN",
    "LCID",
    "NIO",
    "XPEV",
    "LI",
    "JOBY",
    "ACHR",
    "PSNY",
    "PFE",
    "JNJ",
    "ABBV",
    "LLY",
    "UNH",
    "MRNA",
    "HIMS",
    "DOCS",
    "TDOC",
    "DNA",
    "BEAM",
    "CRSP",
    "VKTX",
    "LEGN",
    "GERN",
    "IOVA",
    "CORT",
    "ISRG",
    "NKE",
    "SBUX",
    "DIS",
    "AAPL",
    "AMZN",
    "WMT",
    "COST",
    "HD",
    "TGT",
    "TJX",
    "CAVA",
    "BIRK",
    "SHAK",
    "BROS",
    "WING",
    "LULU",
    "DECK",
    "CROX",
    "PLD",
    "AMT",
    "EQIX",
    "DLR",
    "IRM",
    "IIPR",
    "VICI",
    "O",
    "SPG",
    "META",
    "GOOGL",
    "NFLX",
    "SPOT",
    "TTD",
    "RDDT",
    "DJT",
    "WBD",
    "PARA",
    "FOXA",
    "MTCH",
    "BMBL",
    "ZG",
    "APP",
    "CELH",
    "ARM",
    "TMDX",
    "GKOS",
    "ACLX",
    "SAIA",
    "ODFL",
    "XPO",
    "PAYC",
    "WIX",
    "GFS",
    "FTNT",
    "PANW",
    "CYBR",
    "NOW",
    "WDAY",
    "HUBS",
    "TEAM",
]


@dataclass
class ScreenedStock:
    symbol: str
    current_price: float
    change_3m_pct: float
    change_1m_pct: float
    change_1w_pct: float
    avg_volume: float
    volume_ratio: float
    archetype: str
    daily_closes_3m: list
    days_to_earnings: Optional[int] = None


class StockScreener:
    def __init__(self, top_n: int = 40):
        self.top_n = top_n

    def screen(self, universe: list[str] = None) -> list[ScreenedStock]:
        import yfinance as yf

        symbols = universe or SCREEN_UNIVERSE
        print(f"[StockScreener] Filtering {len(symbols)} stocks through Archetype Doors...")

        try:
            data = yf.download(symbols, period="3mo", group_by="ticker", progress=False, threads=True, auto_adjust=False)
        except Exception as e:
            console.print(f"[red]Screen failed: {e}[/red]")
            return []

        now = datetime.now(timezone.utc).date()
        results = []
        rvol_rejected = 0
        earnings_rejected = 0
        strategy_rejected = 0

        for symbol in symbols:
            try:
                if symbol not in data.columns.get_level_values(0):
                    continue
                
                df = data[symbol].dropna(subset=["Close", "High", "Volume"])
                if len(df) < 20:
                    continue

                closes = df["Close"].astype(float)
                highs = df["High"].astype(float)
                cur_p = float(closes.iloc[-1])

                # --- 1. Institutional Barricade: Volume ---
                volumes = df["Volume"].astype(float)
                avg_vol = float(volumes.tail(20).mean())
                rvol = float(volumes.iloc[-1]) / avg_vol if avg_vol > 0 else 0
                if rvol < 1.0:
                    rvol_rejected += 1
                    continue

                # --- 2. Institutional Barricade: Earnings ---
                days_to_earnings = None
                try:
                    ticker = yf.Ticker(symbol)
                    cal = ticker.calendar
                    dates = []
                    if isinstance(cal, dict) and "Earnings Date" in cal:
                        dates = cal["Earnings Date"]
                    elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                        dates = cal.loc["Earnings Date"].values
                    elif isinstance(cal, pd.DataFrame) and "Value" in cal.columns:
                        try:
                            dates = cal.loc["Earnings Date"].iloc[0]
                            if not isinstance(dates, (list, tuple, pd.Series)):
                                dates = [dates]
                        except: pass

                    if dates:
                        d = [dt.date() if hasattr(dt, "date") else dt for dt in dates]
                        future = [dt for dt in d if dt >= now]
                        if future:
                            days_to_earnings = (future[0] - now).days
                            if days_to_earnings <= 3:
                                earnings_rejected += 1
                                continue
                except Exception:
                    pass

                # --- 3. Archetype Doors (The "OR" Gate) ---
                base_3m = closes.iloc[0]
                change_3m = ((cur_p - base_3m) / base_3m) * 100 if base_3m > 0 else 0
                
                base_1m = closes.iloc[-21] if len(closes) > 21 else closes.iloc[0]
                change_1m = ((cur_p - base_1m) / base_1m) * 100 if base_1m > 0 else 0
                
                base_1w = closes.iloc[-5] if len(closes) > 5 else closes.iloc[0]
                change_1w = ((cur_p - base_1w) / base_1w) * 100 if base_1w > 0 else 0

                archetype = None

                # Door 1: Breakout Star
                if change_1w >= 10.0 or change_1m >= 15.0:
                    archetype = "BREAKOUT"

                # Door 2: Recovery Phoenix
                else:
                    max_3m = highs.max()
                    drawdown = ((cur_p - max_3m) / max_3m) * 100 if max_3m > 0 else 0
                    base_3d = closes.iloc[-3] if len(closes) > 3 else closes.iloc[0]
                    recent_bounce = ((cur_p - base_3d) / base_3d) * 100 if base_3d > 0 else 0
                    if drawdown <= -15.0 and recent_bounce >= 4.0 and rvol > 1.1:
                        archetype = "RECOVERY"

                    # Door 3: Momentum King (Fixed 7% floor)
                    elif change_3m >= 7.0:
                        archetype = "MOMENTUM"

                if not archetype:
                    strategy_rejected += 1
                    continue

                results.append(
                    ScreenedStock(
                        symbol=symbol,
                        current_price=cur_p,
                        change_3m_pct=change_3m,
                        change_1m_pct=change_1m,
                        change_1w_pct=change_1w,
                        avg_volume=avg_vol,
                        volume_ratio=rvol,
                        archetype=archetype,
                        daily_closes_3m=[float(v) for v in closes.values],
                        days_to_earnings=days_to_earnings,
                    )
                )
            except Exception as e:
                continue

        # Final Rank (Prioritize RVOL)
        results.sort(key=lambda s: (s.volume_ratio, max(0, s.change_1w_pct)), reverse=True)
        top_results = results[: self.top_n]

        print(
            f"[StockScreener] Barricade Report: {rvol_rejected} Low-Vol, {earnings_rejected} Earnings, {strategy_rejected} Inactive."
        )
        console.print(f"  [green]Passed {len(top_results)} high-alpha stocks to the Brain.[/green]")
        return top_results
