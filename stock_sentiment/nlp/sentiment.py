"""FinBERT-based financial sentiment analyzer."""

from dataclasses import dataclass
from typing import Optional

from rich.console import Console

console = Console()


@dataclass
class SentimentResult:
    label: str  # "positive", "negative", "neutral"
    score: float  # Raw confidence 0.0 to 1.0
    normalized: float  # Mapped: -1.0 (bearish) to +1.0 (bullish)


class SentimentAnalyzer:
    """Wraps FinBERT for financial text sentiment analysis.

    Lazy-loads the model on first use. The model (~400MB) is downloaded
    once and cached by HuggingFace in ~/.cache/huggingface/.
    """

    def __init__(self, model_name: str = "ProsusAI/finbert"):
        self.model_name = model_name
        self._pipeline = None

    def _load_model(self):
        """Download and load FinBERT. Shows progress on first download."""
        if self._pipeline is not None:
            return

        console.print(
            "[yellow]Loading FinBERT model (first run downloads ~400MB)...[/yellow]"
        )

        from transformers import pipeline, AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModelForSequenceClassification.from_pretrained(self.model_name)

        self._pipeline = pipeline(
            "sentiment-analysis",
            model=model,
            tokenizer=tokenizer,
            device=-1,  # CPU
            truncation=True,
            max_length=512,
        )
        console.print("[green]FinBERT loaded successfully.[/green]")

    def analyze(self, text: str) -> SentimentResult:
        """Analyze a single text and return sentiment."""
        self._load_model()

        if not text or not text.strip():
            return SentimentResult(label="neutral", score=0.5, normalized=0.0)

        # Truncate long text
        text = text[:1500]

        try:
            result = self._pipeline(text)[0]
            label = result["label"].lower()
            score = result["score"]
            normalized = self._normalize(label, score)
            return SentimentResult(
                label=label, score=score, normalized=normalized
            )
        except Exception:
            return SentimentResult(label="neutral", score=0.5, normalized=0.0)

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        """Analyze multiple texts efficiently in a batch."""
        self._load_model()

        if not texts:
            return []

        print(f"[NLP] Starting AI Batch Analysis for {len(texts)} articles...")
        
        # Clean and truncate
        cleaned = [t[:1500] if t and t.strip() else "neutral" for t in texts]

        try:
            results = self._pipeline(cleaned, batch_size=16)
            sentiments = []
            for r in results:
                label = r["label"].lower()
                score = r["score"]
                normalized = self._normalize(label, score)
                sentiments.append(
                    SentimentResult(
                        label=label, score=score, normalized=normalized
                    )
                )
            print(f"[NLP] Batch analysis successful.")
            return sentiments
        except Exception as e:
            print(f"[NLP] ERROR in batch analysis: {e}")
            return [
                SentimentResult(label="neutral", score=0.5, normalized=0.0)
                for _ in texts
            ]

    @staticmethod
    def _normalize(label: str, score: float) -> float:
        """Map FinBERT output to -1 (bearish) to +1 (bullish).

        FinBERT returns one of: positive, negative, neutral
        with a confidence score.
        """
        if label == "positive":
            return score  # 0 to 1
        elif label == "negative":
            return -score  # -1 to 0
        else:
            return 0.0  # neutral
