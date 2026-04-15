"""Base data types and abstract interface for news sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Article:
    title: str
    summary: str
    source: str
    url: str
    published_at: datetime
    raw_text: str = ""  # title + summary, fed to NLP

    def __post_init__(self):
        if not self.raw_text:
            self.raw_text = f"{self.title}. {self.summary}".strip()


@dataclass
class ScoredArticle:
    article: Article
    sentiment_label: str  # "positive", "negative", "neutral"
    sentiment_score: float  # Raw confidence 0-1
    normalized_score: float  # Mapped to -1 (bearish) to +1 (bullish)
    etf_symbols: list = field(default_factory=list)
    relevance_scores: dict = field(default_factory=dict)  # {etf: score}


class NewsSource(ABC):
    """Abstract base class for news sources."""

    @abstractmethod
    def fetch(self, query: str, max_results: int = 10) -> list[Article]:
        """Fetch articles matching the query."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this source is configured and available."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name."""
        ...
