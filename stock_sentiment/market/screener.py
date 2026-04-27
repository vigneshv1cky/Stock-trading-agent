"""Stock screener: filters and classifies stocks using adaptive percentile thresholds (Option B)."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from rich.console import Console

console = Console()

SCREEN_UNIVERSE = [
    "PLTR", "SNAP", "U", "PINS", "RBLX", "PATH", "DDOG", "NET", "CRWD", "ZS",
    "MDB", "SNOW", "ROKU", "HOOD", "SOFI", "AFRM", "UPST", "IONQ", "RGTI", "QUBT",
    "LUNR", "RKLB", "ASTS", "AMD", "INTC", "QCOM", "MU", "MRVL", "ON", "SMCI",
    "UBER", "LYFT", "DASH", "ABNB", "TWLO", "OKTA", "CFLT", "ESTC", "DOCN", "BRZE",
    "MNDY", "GLBE", "GLOB", "TOST", "GTLB", "IOT", "AI", "BBAI", "SOUN", "GRAB",
    "SE", "SHOP", "SPOT", "OPEN", "DUOL", "BILL", "PCOR", "DT", "FRSH", "TENB",
    "RPD", "CRDO", "ANET", "PANW", "FTNT", "S", "QLYS", "CHKP", "GEN",
    "NVDA", "TSM", "AVGO", "ASML", "AMAT", "LRCX", "KLAC", "ADI", "TXN", "WOLF",
    "SLAB", "ACLS", "RMBS", "DIOD", "INDI", "SITM", "CRUS", "LSCC", "MTSI", "NVTS",
    "POWI", "V", "MA", "GS", "JPM", "BAC", "MS", "C", "WFC",
    "AXP", "BLK", "MSTR", "COIN", "WULF", "IREN", "MARA", "RIOT", "CLSK",
    "HUT", "CORZ", "CIFR", "PYPL", "NU", "IBKR", "ALLY", "STNE", "LMT", "RTX",
    "NOC", "GD", "BA", "LHX", "HWM", "TDG", "HII", "LDOS", "BWXT", "AXON",
    "RKLB", "IRDM", "KTOS", "XOM", "CVX", "COP", "OXY", "EOG", "SLB", "PBR",
    "TTE", "SHEL", "BP", "EQNR", "MPC", "PSX", "VLO", "APA", "MUR", "DVN",
    "HAL", "OVV", "CTRA", "AR", "RRC", "EQT", "CTVA", "CF", "MOS", "ADM",
    "BG", "NEE", "FSLR", "ENPH", "PLUG", "RUN", "SEDG", "ARRY", "BE", "VIST",
    "SMR", "OKLO", "LEU", "CCJ", "UUUU", "ZIM", "MATX", "GNK", "DSX", "STNG",
    "FRO", "DAC", "EGLE", "SBLK", "FCX", "AA", "CLF", "NUE", "NEM",
    "GOLD", "AU", "HMY", "KGC", "RGLD", "FNV", "TSLA", "RACE", "UPS", "FDX",
    "NSC", "CSX", "UNP", "LUV", "DAL", "UAL", "AAL", "F", "GM", "RIVN",
    "LCID", "NIO", "XPEV", "LI", "JOBY", "ACHR", "PSNY", "PFE", "JNJ", "ABBV",
    "LLY", "UNH", "MRNA", "HIMS", "DOCS", "TDOC", "DNA", "BEAM", "CRSP", "VKTX",
    "LEGN", "GERN", "IOVA", "CORT", "ISRG", "NKE", "SBUX", "DIS", "AAPL", "AMZN",
    "WMT", "COST", "HD", "TGT", "TJX", "CAVA", "BIRK", "SHAK", "BROS", "WING",
    "LULU", "DECK", "CROX", "PLD", "AMT", "EQIX", "DLR", "IRM", "IIPR", "VICI",
    "O", "SPG", "META", "GOOGL", "NFLX", "TTD", "RDDT", "DJT", "WBD",
    "FOXA", "MTCH", "BMBL", "ZG", "APP", "CELH", "ARM", "TMDX", "GKOS", "ACLX",
    "SAIA", "ODFL", "XPO", "PAYC", "WIX", "GFS", "NOW", "WDAY", "HUBS", "TEAM",
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

    def screen(self, universe: Optional[list[str]] = None) -> list[ScreenedStock]:
        import yfinance as yf

        symbols = list(dict.fromkeys(universe or SCREEN_UNIVERSE))  # deduplicate, preserve order
        print(f"[StockScreener] Filtering {len(symbols)} stocks through Institutional Barricades...")
        print(f"[StockScreener] Downloading 3mo OHLCV for {len(symbols)} symbols via yfinance...")

        try:
            data = yf.download(symbols, period="3mo", group_by="ticker", progress=False,
                               threads=True, auto_adjust=False)
            print(f"[StockScreener] yfinance download complete ({len(data.columns.get_level_values(0).unique())} symbols with data).")
        except Exception as e:
            console.print(f"[red]Screen failed: {e}[/red]")
            return []

        now = datetime.now(timezone.utc).date()
        rvol_rejected = 0
        earnings_rejected = 0
        pre_screened = []

        # --- Pass 1: Hard barricades + collect raw metrics ---
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

                # Barricade 1: Relative Volume
                volumes = df["Volume"].astype(float)
                avg_vol = float(volumes.tail(20).mean())
                rvol = float(volumes.iloc[-1]) / avg_vol if avg_vol > 0 else 0
                if rvol < 1.0:
                    rvol_rejected += 1
                    continue

                # Barricade 2: Earnings blackout
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
                        except Exception:
                            pass

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

                # Compute momentum metrics
                base_3m = float(closes.iloc[0])
                change_3m = ((cur_p - base_3m) / base_3m) * 100 if base_3m > 0 else 0.0
                base_1m = float(closes.iloc[-21]) if len(closes) > 21 else base_3m
                change_1m = ((cur_p - base_1m) / base_1m) * 100 if base_1m > 0 else 0.0
                base_1w = float(closes.iloc[-5]) if len(closes) > 5 else base_3m
                change_1w = ((cur_p - base_1w) / base_1w) * 100 if base_1w > 0 else 0.0
                max_3m = float(highs.max())
                drawdown = ((cur_p - max_3m) / max_3m) * 100 if max_3m > 0 else 0.0
                base_3d = float(closes.iloc[-3]) if len(closes) > 3 else cur_p
                bounce = ((cur_p - base_3d) / base_3d) * 100 if base_3d > 0 else 0.0

                pre_screened.append({
                    "symbol": symbol, "cur_p": cur_p, "avg_vol": avg_vol, "rvol": rvol,
                    "change_3m": change_3m, "change_1m": change_1m, "change_1w": change_1w,
                    "drawdown": drawdown, "bounce": bounce,
                    "closes": closes, "days_to_earnings": days_to_earnings,
                })
            except Exception:
                continue

        # --- Compute adaptive percentile thresholds from live universe ---
        if len(pre_screened) >= 10:
            arr_1w = np.array([m["change_1w"] for m in pre_screened])
            arr_1m = np.array([m["change_1m"] for m in pre_screened])
            arr_3m = np.array([m["change_3m"] for m in pre_screened])
            arr_dd = np.array([m["drawdown"] for m in pre_screened])
            arr_bounce = np.array([m["bounce"] for m in pre_screened])

            thresh_break_1w = float(np.percentile(arr_1w, 75))
            thresh_break_1m = float(np.percentile(arr_1m, 75))
            thresh_momentum = float(np.percentile(arr_3m, 60))
            thresh_recovery_dd = float(np.percentile(arr_dd, 30))
            thresh_recovery_bounce = float(np.percentile(arr_bounce, 65))

            console.print(
                f"  [dim]Adaptive thresholds — "
                f"Breakout: 1w≥{thresh_break_1w:.1f}% or 1m≥{thresh_break_1m:.1f}% | "
                f"Momentum: 3m≥{thresh_momentum:.1f}% | "
                f"Recovery: dd≤{thresh_recovery_dd:.1f}% & bounce≥{thresh_recovery_bounce:.1f}%[/dim]"
            )
        else:
            # Fallback to static thresholds when universe is too small
            thresh_break_1w, thresh_break_1m = 10.0, 15.0
            thresh_momentum = 7.0
            thresh_recovery_dd, thresh_recovery_bounce = -15.0, 4.0

        # --- Pass 2: Classify archetypes using adaptive thresholds ---
        results = []
        strategy_rejected = 0

        for m in pre_screened:
            change_1w = m["change_1w"]
            change_1m = m["change_1m"]
            change_3m = m["change_3m"]
            drawdown = m["drawdown"]
            bounce = m["bounce"]
            rvol = m["rvol"]
            closes = m["closes"]

            archetype = None
            if change_1w >= thresh_break_1w or change_1m >= thresh_break_1m:
                archetype = "BREAKOUT"
            elif drawdown <= thresh_recovery_dd and bounce >= thresh_recovery_bounce and rvol > 1.1:
                archetype = "RECOVERY"
            elif change_3m >= thresh_momentum:
                archetype = "MOMENTUM"

            if not archetype:
                strategy_rejected += 1
                continue

            results.append(ScreenedStock(
                symbol=m["symbol"],
                current_price=m["cur_p"],
                change_3m_pct=change_3m,
                change_1m_pct=change_1m,
                change_1w_pct=change_1w,
                avg_volume=m["avg_vol"],
                volume_ratio=rvol,
                archetype=archetype,
                daily_closes_3m=[float(v) for v in closes.values],
                days_to_earnings=m["days_to_earnings"],
            ))

        results.sort(key=lambda s: (s.volume_ratio, max(0, s.change_1w_pct)), reverse=True)
        top_results = results[: self.top_n]

        print(
            f"[StockScreener] Barricade Report: {rvol_rejected} Low-Vol, "
            f"{earnings_rejected} Earnings, {strategy_rejected} Inactive."
        )
        console.print(f"  [green]Passed {len(top_results)} high-alpha stocks to the Brain.[/green]")
        return top_results
