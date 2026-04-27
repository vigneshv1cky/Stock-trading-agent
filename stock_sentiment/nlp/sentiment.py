"""Amazon Nova Micro-based financial sentiment scorer (article-level).

Cheaper than Haiku (~7x) for bulk per-article scoring.
Qualitative conviction scoring (red flags, catalysts) stays on Haiku in stock_predictor.py.
"""

import json
import os
import re
from dataclasses import dataclass


import boto3

_MODEL_ID = "amazon.nova-micro-v1:0"
_SYSTEM = (
    "You are a financial sentiment scorer for equity investors. "
    "Score each numbered headline from -1.0 (very negative) to 1.0 (very positive). "
    "Return ONLY a JSON array of numbers in order, no explanation."
)


@dataclass
class SentimentResult:
    label: str       # "positive", "negative", "neutral"
    score: float     # confidence 0.0–1.0
    normalized: float  # -1.0 (bearish) to +1.0 (bullish)


def _neutral() -> SentimentResult:
    return SentimentResult(label="neutral", score=0.5, normalized=0.0)


def _from_score(s: float) -> SentimentResult:
    s = max(-1.0, min(1.0, float(s)))
    label = "positive" if s > 0.2 else ("negative" if s < -0.2 else "neutral")
    return SentimentResult(label=label, score=abs(s), normalized=s)


class SentimentAnalyzer:
    """Nova Micro-backed bulk article sentiment scorer."""

    def __init__(self, region_name: str | None = None):
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def analyze(self, text: str) -> SentimentResult:
        results = self.analyze_batch([text])
        return results[0] if results else _neutral()

    _BATCH_SIZE = 50  # keeps output well within token limits

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        if not texts:
            return []

        print(f"[NLP] Nova Micro sentiment scoring {len(texts)} articles (batch_size={self._BATCH_SIZE})...")
        results: list[SentimentResult] = []
        for batch_start in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[batch_start: batch_start + self._BATCH_SIZE]
            results.extend(self._score_batch(batch, batch_start))
        print(f"[NLP] Nova Micro scoring complete: {len(results)} results.")
        return results

    def _score_batch(self, texts: list[str], offset: int) -> list[SentimentResult]:
        numbered = "\n".join(f"{offset + i + 1}. {t[:300]}" for i, t in enumerate(texts))

        try:
            resp = self._get_client().converse(
                modelId=_MODEL_ID,
                system=[{"text": _SYSTEM}],
                messages=[{"role": "user", "content": [{"text": numbered}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0},
            )
            text_out = resp["output"]["message"]["content"][0]["text"].strip()
            # Try 1: extract the JSON array even if the model prepends explanation text
            m = re.search(r'\[[\s\S]*\]', text_out)
            if m:
                try:
                    scores = json.loads(m.group())
                    if isinstance(scores, list):
                        while len(scores) < len(texts):
                            scores.append(0.0)
                        return [_from_score(s) for s in scores[: len(texts)]]
                except Exception:
                    pass
            # Try 2: pull every float/int from the response as a last resort
            raw_nums = re.findall(r'-?\d+(?:\.\d+)?', text_out)
            if raw_nums:
                scores = [float(n) for n in raw_nums[: len(texts)]]
                while len(scores) < len(texts):
                    scores.append(0.0)
                return [_from_score(s) for s in scores]
            raise ValueError("No scores extractable from response")
        except Exception as e:
            print(f"[NLP] Nova Micro batch error (offset={offset}): {e} — falling back to neutral.")
            return [_neutral() for _ in texts]
