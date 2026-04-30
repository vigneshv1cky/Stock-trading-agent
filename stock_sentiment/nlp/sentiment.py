"""Financial sentiment scorer using Claude Haiku."""

import json
import os
import re
from dataclasses import dataclass

import boto3

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

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


def _parse_scores(text_out: str, n: int) -> list[float] | None:
    """Extract a list of n floats from model output. Returns None if unparseable."""
    m = re.search(r'\[[\s\S]*\]', text_out)
    if m:
        try:
            scores = json.loads(m.group())
            if isinstance(scores, list):
                while len(scores) < n:
                    scores.append(0.0)
                return [float(s) for s in scores[:n]]
        except Exception:
            pass
    raw_nums = re.findall(r'-?\d+(?:\.\d+)?', text_out)
    if raw_nums:
        scores = [float(x) for x in raw_nums[:n]]
        while len(scores) < n:
            scores.append(0.0)
        return scores
    return None


class SentimentAnalyzer:
    """Bulk article sentiment scorer with Nova Micro → Nova Lite → Haiku fallback."""

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

    _BATCH_SIZE = 50

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        if not texts:
            return []

        print(f"[NLP] Sentiment scoring {len(texts)} articles (batch_size={self._BATCH_SIZE})...")
        results: list[SentimentResult] = []
        for batch_start in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[batch_start: batch_start + self._BATCH_SIZE]
            results.extend(self._score_batch(batch, batch_start))
        print(f"[NLP] Sentiment scoring complete: {len(results)} results.")
        return results

    def _score_batch(self, texts: list[str], offset: int) -> list[SentimentResult]:
        numbered = "\n".join(f"{offset + i + 1}. {t}" for i, t in enumerate(texts))
        try:
            resp = self._get_client().converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _SYSTEM}],
                messages=[{"role": "user", "content": [{"text": numbered}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0},
            )
            text_out = resp["output"]["message"]["content"][0]["text"].strip()
            scores = _parse_scores(text_out, len(texts))
            if scores is not None:
                return [_from_score(s) for s in scores]
            print(f"[NLP] Haiku returned unparseable output for batch (offset={offset}) — returning neutral.")
        except Exception as e:
            print(f"[NLP] Haiku failed (offset={offset}): {e} — returning neutral.")
        return [_neutral() for _ in texts]
