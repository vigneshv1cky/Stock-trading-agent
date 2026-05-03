"""ResearchAgent — deep per-symbol research: options flow, advanced technicals, short interest.

Subscribes to: market.signal  (runs in parallel with ScreenerAgent)
Publishes to:  symbol.researched

Results are cached 120 s per symbol and also accessible synchronously via
ResearchAgent.get_cached(sym) so PredictorAgent can merge them in without
needing to subscribe to a separate event.
"""

import asyncio
import os
import time
from datetime import timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus

_CACHE_TTL_S = 120
_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = (
    "You are a quantitative research analyst producing a 2-sentence trade setup brief. "
    "Given technical indicators, state: "
    "(1) whether the technical setup confirms or contradicts the price/volume signal, "
    "(2) what the momentum and volatility metrics imply about near-term risk. "
    "Be direct and specific. No hedging phrases."
)


class ResearchAgent(BaseAgent):
    _cache: dict[str, tuple[float, dict]] = {}  # sym → (timestamp, result)
    _semaphore: asyncio.Semaphore = asyncio.Semaphore(5)  # cap concurrent yfinance calls

    def __init__(self, bus: EventBus):
        super().__init__(bus, "ResearchAgent")
        self._queue = bus.subscribe("market.signal")
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, data: dict) -> None:
        sym = data["symbol"]
        cached = self._cache.get(sym)
        if cached and time.time() - cached[0] < _CACHE_TTL_S:
            return

        loop = asyncio.get_running_loop()
        try:
            async with ResearchAgent._semaphore:
                result = await loop.run_in_executor(None, self._research, sym, data)
            ResearchAgent._cache[sym] = (time.time(), result)
            self.log.info(
                "Researched: %s  RSI=%.0f  BB=%.2f  MACD=%+.4f  ATR=%.1f%%",
                sym,
                result.get("rsi", 0),
                result.get("bb_pct", 0),
                result.get("macd_hist", 0),
                result.get("atr_pct", 0),
            )
            await self.bus.publish("symbol.researched", {"symbol": sym, "research": result})
        except Exception as exc:
            self.log.error("Research error %s: %s", sym, exc)

    @classmethod
    def get_cached(cls, sym: str) -> Optional[dict]:
        cached = cls._cache.get(sym)
        if cached and time.time() - cached[0] < _CACHE_TTL_S * 3:
            return cached[1]
        return None

    # ------------------------------------------------------------------
    # Blocking research — runs in executor
    # ------------------------------------------------------------------

    def _research(self, sym: str, signal_data: dict) -> dict:
        if "/" in sym:
            return self._research_crypto(sym, signal_data)

        import yfinance as yf

        ticker = yf.Ticker(sym)
        result: dict = {
            "symbol": sym,
            "rvol": signal_data.get("rvol", 0),
            "price_change_pct": signal_data.get("price_change_pct", 0),
        }

        # ---- Technical indicators (60-day history) ----
        try:
            hist = ticker.history(period="60d", interval="1d", auto_adjust=True)
            if not hist.empty and len(hist) >= 20:
                close = hist["Close"]
                high = hist["High"]
                low = hist["Low"]
                volume = hist["Volume"]

                # ATR-14
                prev_close = close.shift(1)
                tr = (high - low).combine(
                    (high - prev_close).abs(), max
                ).combine((low - prev_close).abs(), max)
                atr = float(tr.rolling(14).mean().iloc[-1])
                atr_pct = atr / float(close.iloc[-1]) * 100

                # Bollinger %B (20/2)
                sma20 = close.rolling(20).mean()
                std20 = close.rolling(20).std()
                bb_upper = sma20 + 2 * std20
                bb_lower = sma20 - 2 * std20
                span = float(bb_upper.iloc[-1] - bb_lower.iloc[-1])
                bb_pct = float((close.iloc[-1] - bb_lower.iloc[-1]) / span) if span > 0 else 0.5

                # MACD histogram (12/26/9)
                ema12 = close.ewm(span=12).mean()
                ema26 = close.ewm(span=26).mean()
                macd_hist = float((ema12 - ema26 - (ema12 - ema26).ewm(span=9).mean()).iloc[-1])

                # RSI-14
                delta = close.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rsi = float(100 - 100 / (1 + gain.iloc[-1] / max(float(loss.iloc[-1]), 1e-6)))

                # 5d vs 20d volume trend
                vol_trend = float(volume.tail(5).mean() / max(float(volume.tail(20).mean()), 1) - 1)

                result.update({
                    "atr_pct": round(atr_pct, 2),
                    "bb_pct": round(bb_pct, 3),
                    "macd_hist": round(macd_hist, 4),
                    "rsi": round(rsi, 1),
                    "vol_trend": round(vol_trend, 3),
                })
        except Exception as exc:
            self.log.debug("Technicals failed %s: %s", sym, exc)

        # ---- Claude synthesis ----
        try:
            user_text = (
                f"{sym} | RVOL={result.get('rvol', 0):.1f}x "
                f"price={result.get('price_change_pct', 0):+.1f}%\n"
                f"Technical: RSI={result.get('rsi', 0):.0f} "
                f"BB={result.get('bb_pct', 0):.2f} "
                f"MACD_hist={result.get('macd_hist', 0):+.4f} "
                f"ATR={result.get('atr_pct', 0):.1f}% "
                f"vol_trend={result.get('vol_trend', 0):+.1%}"
            )
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            result["synthesis"] = content[0]["text"].strip() if content else ""
        except Exception as exc:
            self.log.debug("Synthesis failed %s: %s", sym, exc)
            result["synthesis"] = ""

        return result

    # ------------------------------------------------------------------
    # Crypto research — Alpaca historical for technicals; skip options/short
    # ------------------------------------------------------------------

    def _research_crypto(self, sym: str, signal_data: dict) -> dict:
        result: dict = {
            "symbol": sym,
            "rvol": signal_data.get("rvol", 0),
            "price_change_pct": signal_data.get("price_change_pct", 0),
        }

        try:
            from datetime import timezone as _tz

            from alpaca.data.historical import CryptoHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import CryptoBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame  # type: ignore[import-untyped]

            import pandas as pd

            client = CryptoHistoricalDataClient()
            import datetime as _dt
            end = _dt.datetime.now(_tz.utc)
            start = end - _dt.timedelta(days=75)
            request = CryptoBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            sym_bars = client.get_crypto_bars(request)[sym]

            if sym_bars and len(sym_bars) >= 20:
                close = pd.Series([float(b.close) for b in sym_bars])
                high = pd.Series([float(b.high) for b in sym_bars])
                low = pd.Series([float(b.low) for b in sym_bars])
                volume = pd.Series([float(b.volume) for b in sym_bars])

                prev_close = close.shift(1)
                tr = (high - low).combine(
                    (high - prev_close).abs(), max
                ).combine((low - prev_close).abs(), max)
                atr = float(tr.rolling(14).mean().iloc[-1])
                atr_pct = atr / float(close.iloc[-1]) * 100

                sma20 = close.rolling(20).mean()
                std20 = close.rolling(20).std()
                bb_upper = sma20 + 2 * std20
                bb_lower = sma20 - 2 * std20
                span = float(bb_upper.iloc[-1] - bb_lower.iloc[-1])
                bb_pct = float((close.iloc[-1] - bb_lower.iloc[-1]) / span) if span > 0 else 0.5

                ema12 = close.ewm(span=12).mean()
                ema26 = close.ewm(span=26).mean()
                macd_hist = float((ema12 - ema26 - (ema12 - ema26).ewm(span=9).mean()).iloc[-1])

                delta = close.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rsi = float(100 - 100 / (1 + gain.iloc[-1] / max(float(loss.iloc[-1]), 1e-6)))

                vol_trend = float(volume.tail(5).mean() / max(float(volume.tail(20).mean()), 1) - 1)

                result.update({
                    "atr_pct": round(atr_pct, 2),
                    "bb_pct": round(bb_pct, 3),
                    "macd_hist": round(macd_hist, 4),
                    "rsi": round(rsi, 1),
                    "vol_trend": round(vol_trend, 3),
                })
        except Exception as exc:
            self.log.debug("Crypto technicals failed %s: %s", sym, exc)

        try:
            user_text = (
                f"{sym} (crypto) | RVOL={result.get('rvol', 0):.1f}x "
                f"price={result.get('price_change_pct', 0):+.1f}%\n"
                f"Technical: RSI={result.get('rsi', 0):.0f} "
                f"BB={result.get('bb_pct', 0):.2f} "
                f"MACD_hist={result.get('macd_hist', 0):+.4f} "
                f"ATR={result.get('atr_pct', 0):.1f}% "
                f"vol_trend={result.get('vol_trend', 0):+.1%}"
            )
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            result["synthesis"] = content[0]["text"].strip() if content else ""
        except Exception as exc:
            self.log.debug("Crypto synthesis failed %s: %s", sym, exc)
            result["synthesis"] = ""

        return result
