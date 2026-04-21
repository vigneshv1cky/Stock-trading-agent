"""Technical indicator computation from OHLCV data."""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from stock_sentiment.market.price_fetcher import PriceData


@dataclass
class TechnicalIndicators:
    symbol: str
    rsi_14: Optional[float] = None  # 0-100
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    macd_crossover: str = "none"  # "bullish", "bearish", "none"
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    ema_12: Optional[float] = None
    ema_26: Optional[float] = None
    price_vs_sma20: str = "unknown"  # "above", "below"
    price_vs_sma50: str = "unknown"
    volume_ratio: Optional[float] = None  # current vol / 20d avg
    trend_direction: str = "sideways"  # "up", "down", "sideways"
    trend_strength: float = 0.0  # 0.0 to 1.0
    days_to_earnings: Optional[int] = None


class TechnicalAnalyzer:
    """Computes technical indicators from OHLCV DataFrames."""

    def analyze(self, price_data: PriceData) -> TechnicalIndicators:
        """Compute all indicators for a single symbol."""
        df = price_data.ohlcv
        if df is None or df.empty or len(df) < 5:
            return TechnicalIndicators(symbol=price_data.symbol, days_to_earnings=price_data.days_to_earnings)

        closes = df["Close"].astype(float)
        current_price = float(closes.iloc[-1])

        ti = TechnicalIndicators(symbol=price_data.symbol, days_to_earnings=price_data.days_to_earnings)

        # RSI
        ti.rsi_14 = self.compute_rsi(closes, 14)

        # MACD
        macd = self.compute_macd(closes)
        if macd:
            ti.macd_line, ti.macd_signal, ti.macd_histogram = macd
            # Crossover detection
            if len(closes) >= 27:
                prev_hist = self._compute_macd_hist_at(closes.iloc[:-1])
                if prev_hist is not None and ti.macd_histogram is not None:
                    if prev_hist < 0 and ti.macd_histogram > 0:
                        ti.macd_crossover = "bullish"
                    elif prev_hist > 0 and ti.macd_histogram < 0:
                        ti.macd_crossover = "bearish"

        # Moving averages
        if len(closes) >= 20:
            ti.sma_20 = float(closes.tail(20).mean())
            ti.price_vs_sma20 = "above" if current_price > ti.sma_20 else "below"

        if len(closes) >= 50:
            ti.sma_50 = float(closes.tail(50).mean())
            ti.price_vs_sma50 = "above" if current_price > ti.sma_50 else "below"
        else:
            ti.price_vs_sma50 = ti.price_vs_sma20

        # EMA
        if len(closes) >= 12:
            ti.ema_12 = float(closes.ewm(span=12, adjust=False).mean().iloc[-1])
        if len(closes) >= 26:
            ti.ema_26 = float(closes.ewm(span=26, adjust=False).mean().iloc[-1])

        # Volume ratio
        if price_data.avg_volume_20d > 0:
            ti.volume_ratio = price_data.volume / price_data.avg_volume_20d

        # Trend
        direction, strength = self.classify_trend(closes)
        ti.trend_direction = direction
        ti.trend_strength = strength

        return ti

    def analyze_batch(
        self, price_map: dict[str, PriceData]
    ) -> dict[str, TechnicalIndicators]:
        """Compute indicators for multiple symbols."""
        return {
            symbol: self.analyze(pd)
            for symbol, pd in price_map.items()
        }

    @staticmethod
    def compute_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
        """RSI using Wilder's smoothing method."""
        if len(closes) < period + 1:
            return None

        deltas = closes.diff().dropna()
        gains = deltas.where(deltas > 0, 0.0)
        losses = (-deltas).where(deltas < 0, 0.0)

        avg_gain = gains.rolling(window=period, min_periods=period).mean().iloc[-1]
        avg_loss = losses.rolling(window=period, min_periods=period).mean().iloc[-1]

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return float(100 - (100 / (1 + rs)))

    @staticmethod
    def compute_macd(
        closes: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal_period: int = 9,
    ) -> Optional[tuple[float, float, float]]:
        """Returns (macd_line, signal_line, histogram)."""
        if len(closes) < slow + signal_period:
            return None

        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
        histogram = macd_line - signal_line

        return (
            float(macd_line.iloc[-1]),
            float(signal_line.iloc[-1]),
            float(histogram.iloc[-1]),
        )

    def _compute_macd_hist_at(self, closes: pd.Series) -> Optional[float]:
        """Compute MACD histogram for a given series (for crossover detection)."""
        result = self.compute_macd(closes)
        return result[2] if result else None

    @staticmethod
    def classify_trend(
        closes: pd.Series, lookback: int = 10
    ) -> tuple[str, float]:
        """Classify trend using linear regression slope.

        Returns (direction, strength) where strength is 0-1.
        """
        if len(closes) < lookback:
            return ("sideways", 0.0)

        recent = closes.tail(lookback).values.astype(float)
        x = np.arange(len(recent))

        # Linear regression
        x_mean = x.mean()
        y_mean = recent.mean()
        ss_xx = ((x - x_mean) ** 2).sum()
        ss_xy = ((x - x_mean) * (recent - y_mean)).sum()

        if ss_xx == 0:
            return ("sideways", 0.0)

        slope = ss_xy / ss_xx

        # Normalize slope as percentage of price per day
        slope_pct = (slope / y_mean) * 100 if y_mean != 0 else 0

        # Classify
        if slope_pct > 0.2:
            direction = "up"
        elif slope_pct < -0.2:
            direction = "down"
        else:
            direction = "sideways"

        # Strength: R-squared
        y_pred = slope * x + (y_mean - slope * x_mean)
        ss_res = ((recent - y_pred) ** 2).sum()
        ss_tot = ((recent - y_mean) ** 2).sum()
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        strength = max(0.0, min(1.0, r_squared))

        return (direction, strength)
