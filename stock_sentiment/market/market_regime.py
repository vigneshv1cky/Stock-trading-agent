"""Market regime detection: classifies the macro environment to adjust buy threshold and position sizing."""

import math
from dataclasses import dataclass

import yfinance as yf


@dataclass
class MarketRegime:
    label: str            # "BULL", "NEUTRAL", "BEAR", "HIGH_VOL"
    spy_vs_sma200: float  # % SPY is above/below its 200-day SMA
    vix: float
    buy_threshold: float  # conviction score required to open a position
    size_multiplier: float  # applied to portfolio-% slot sizes (0.70–1.0)

    def __str__(self) -> str:
        direction = "above" if self.spy_vs_sma200 >= 0 else "below"
        return (
            f"{self.label} | SPY {abs(self.spy_vs_sma200):.1f}% {direction} 200-SMA"
            f" | VIX {self.vix:.1f}"
            f" | threshold={self.buy_threshold:.0f} | sizing={self.size_multiplier:.0%}"
        )


_NEUTRAL = MarketRegime("NEUTRAL", 0.0, 20.0, buy_threshold=60.0, size_multiplier=1.0)


def detect_regime() -> MarketRegime:
    """Fetch SPY and VIX from yfinance and classify the current market regime.

    Regime rules (checked in priority order):
      HIGH_VOL : VIX > 30          → threshold 70, size 70%
      BEAR     : SPY < 200-day SMA → threshold 65, size 85%
      BULL     : SPY > 3% above SMA and VIX < 20 → threshold 55, size 100%
      NEUTRAL  : everything else   → threshold 60, size 100%
    """
    print("[MarketRegime] Fetching SPY (1y) and ^VIX (5d) from yfinance...")
    try:
        spy_hist = yf.download("SPY", period="1y", progress=False, auto_adjust=True)["Close"].squeeze()
        if spy_hist.empty or len(spy_hist) < 200:
            print(f"[MarketRegime] Insufficient SPY data ({len(spy_hist)} bars), defaulting to NEUTRAL")
            return _NEUTRAL

        sma200 = float(spy_hist.rolling(200).mean().iloc[-1])
        spy_price = float(spy_hist.iloc[-1])

        if math.isnan(sma200) or math.isnan(spy_price):
            print("[MarketRegime] SPY data contains NaN, defaulting to NEUTRAL")
            return _NEUTRAL

        spy_vs_sma = ((spy_price - sma200) / sma200) * 100

        vix_hist = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)["Close"].squeeze()
        if vix_hist.empty:
            print("[MarketRegime] VIX data empty, defaulting to NEUTRAL")
            return _NEUTRAL

        vix = float(vix_hist.iloc[-1])
        if math.isnan(vix):
            print("[MarketRegime] VIX data contains NaN, defaulting to NEUTRAL")
            return _NEUTRAL

    except Exception as e:
        print(f"[MarketRegime] Data fetch failed ({e}), defaulting to NEUTRAL")
        return _NEUTRAL

    print(f"[MarketRegime] SPY={spy_price:.2f}, SMA200={sma200:.2f} ({spy_vs_sma:+.1f}%), VIX={vix:.1f}")

    if vix > 30:
        regime = MarketRegime("HIGH_VOL", spy_vs_sma, vix, buy_threshold=70.0, size_multiplier=0.70)
    elif spy_price < sma200:
        regime = MarketRegime("BEAR", spy_vs_sma, vix, buy_threshold=65.0, size_multiplier=0.85)
    elif spy_vs_sma > 3.0 and vix < 20:
        regime = MarketRegime("BULL", spy_vs_sma, vix, buy_threshold=55.0, size_multiplier=1.0)
    else:
        regime = MarketRegime("NEUTRAL", spy_vs_sma, vix, buy_threshold=60.0, size_multiplier=1.0)

    print(f"[MarketRegime] Classified as: {regime}")
    return regime
