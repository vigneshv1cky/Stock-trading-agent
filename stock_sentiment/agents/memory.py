"""AgentMemory — persists lessons learned from trade outcomes to disk.

Shared between LearningAgent (writes) and PredictorAgent + CriticAgent (reads).
File: ~/.stock_screener/agent_memory.json

  • Confidence decay: older lessons lose weight (half-life = 30 trades)
  • format_for_prompt() returns top lessons sorted by recency-weighted relevance
"""

import json
import math
import os
from datetime import datetime, timezone
from typing import Optional

_MEMORY_FILE = os.path.expanduser("~/.stock_screener/agent_memory.json")
_MAX_LESSONS = 16
_DECAY_HALF_LIFE_TRADES = 30


class AgentMemory:
    def __init__(self):
        self._global: list[dict] = []
        self._updated_at: Optional[str] = None
        self._trades_reviewed: int = 0
        self._total_trades_ever: int = 0
        self._load()

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def format_for_prompt(self) -> str:
        lessons = [self._fmt(les) for les in self._decay_sort(self._global)[:6]]
        if not lessons:
            return ""
        return "LEARNED FROM RECENT TRADES (apply these):\n" + "\n".join(f"  {lesson}" for lesson in lessons)

    def get_lessons(self) -> list[str]:
        return [self._fmt(les) for les in self._decay_sort(self._global)[:5]]

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def update(self, lessons: list[dict], trades_reviewed: int, **_kwargs) -> None:
        self._trades_reviewed = trades_reviewed
        self._total_trades_ever += trades_reviewed
        self._updated_at = datetime.now(timezone.utc).isoformat()
        stamped = [{**les, "_trade_stamp": self._total_trades_ever} for les in lessons]
        self._global.extend(stamped)
        self._global = self._decay_sort(self._global)[:_MAX_LESSONS]
        self._save()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _decay_weight(self, lesson: dict) -> float:
        stamp = lesson.get("_trade_stamp", 0)
        age = max(0, self._total_trades_ever - stamp)
        return math.exp(-age * math.log(2) / _DECAY_HALF_LIFE_TRADES)

    def _decay_sort(self, lessons: list[dict]) -> list[dict]:
        return sorted(lessons, key=self._decay_weight, reverse=True)

    @staticmethod
    def _fmt(lesson: dict) -> str:
        wr = lesson.get("win_rate", 0)
        n = lesson.get("sample_size", 0)
        return (
            f"- {lesson.get('pattern', '')} → {lesson.get('instruction', '')}"
            f" (win_rate={wr:.0%}, n={n})"
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if not os.path.exists(_MEMORY_FILE):
                return
            with open(_MEMORY_FILE) as f:
                data = json.load(f)
            # Support old format with by_archetype — merge everything into global
            self._global = data.get("global_lessons", data.get("lessons", []))
            for bucket in data.get("by_archetype", {}).values():
                self._global.extend(bucket)
            self._global = self._decay_sort(self._global)[:_MAX_LESSONS]
            self._updated_at = data.get("updated_at")
            self._trades_reviewed = data.get("trades_reviewed", 0)
            self._total_trades_ever = data.get("total_trades_ever", self._trades_reviewed)
        except Exception:
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(_MEMORY_FILE), exist_ok=True)
        with open(_MEMORY_FILE, "w") as f:
            json.dump(
                {
                    "global_lessons": self._global,
                    "updated_at": self._updated_at,
                    "trades_reviewed": self._trades_reviewed,
                    "total_trades_ever": self._total_trades_ever,
                },
                f,
                indent=2,
            )
