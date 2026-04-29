"""AgentMemory — persists lessons learned from trade outcomes to disk.

Shared between LearningAgent (writes) and PredictorAgent + CriticAgent (reads).
File: ~/.stock_screener/agent_memory.json

Upgrades vs. prior version:
  • Per-archetype lesson buckets: lessons are tagged and retrieved by archetype
  • Confidence decay: older lessons lose weight (half-life = 30 trades)
  • format_for_prompt(archetype) returns archetype-specific lessons first, then global
"""

import json
import math
import os
from datetime import datetime, timezone
from typing import Optional

_MEMORY_FILE = os.path.expanduser("~/.stock_screener/agent_memory.json")
_MAX_LESSONS_PER_BUCKET = 8
_DECAY_HALF_LIFE_TRADES = 30   # lesson relevance halves every 30 trades

_KNOWN_ARCHETYPES = {"FRESH_BREAKOUT", "BREAKOUT", "MOMENTUM", "RECOVERY"}


class AgentMemory:
    def __init__(self):
        # Global lessons (no archetype tag) + per-archetype buckets
        self._global: list[dict] = []
        self._by_archetype: dict[str, list[dict]] = {a: [] for a in _KNOWN_ARCHETYPES}
        self._updated_at: Optional[str] = None
        self._trades_reviewed: int = 0
        self._total_trades_ever: int = 0
        self._load()

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def format_for_prompt(self, archetype: Optional[str] = None) -> str:
        """Return formatted lessons for prompt injection.

        Returns archetype-specific lessons first (if archetype is known),
        followed by global lessons, with decay-weighted ordering.
        """
        specific: list[str] = []
        if archetype and archetype in self._by_archetype:
            bucket = self._by_archetype[archetype]
            specific = [self._fmt(les) for les in self._decay_sort(bucket)[:4]]

        global_lessons = [self._fmt(les) for les in self._decay_sort(self._global)[:4]]

        lines: list[str] = []
        if specific:
            lines.append(f"LESSONS FOR {archetype}:")
            lines.extend(f"  {s}" for s in specific)
        if global_lessons:
            lines.append("GENERAL LESSONS:")
            lines.extend(f"  {g}" for g in global_lessons)

        if not lines:
            return ""
        return "LEARNED FROM RECENT TRADES (apply these):\n" + "\n".join(lines)

    def get_lessons(self) -> list[str]:
        """Flat list of top global lessons (backward compat)."""
        return [self._fmt(les) for les in self._decay_sort(self._global)[:5]]

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def update(
        self,
        lessons: list[dict],
        trades_reviewed: int,
        archetype: Optional[str] = None,
    ) -> None:
        """Write a batch of lessons, optionally scoped to an archetype."""
        self._trades_reviewed = trades_reviewed
        self._total_trades_ever += trades_reviewed
        self._updated_at = datetime.now(timezone.utc).isoformat()

        # Stamp each lesson with current trade count for decay calculation
        stamped = [
            {**les, "_trade_stamp": self._total_trades_ever}
            for les in lessons
        ]

        if archetype and archetype in self._by_archetype:
            bucket = self._by_archetype[archetype]
            bucket.extend(stamped)
            self._by_archetype[archetype] = self._decay_sort(bucket)[:_MAX_LESSONS_PER_BUCKET]
        else:
            self._global.extend(stamped)
            self._global = self._decay_sort(self._global)[:_MAX_LESSONS_PER_BUCKET]

        self._save()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _decay_weight(self, lesson: dict) -> float:
        """Exponential decay: weight halves every _DECAY_HALF_LIFE_TRADES trades."""
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
            self._global = data.get("global_lessons", data.get("lessons", []))
            self._by_archetype = {
                a: data.get("by_archetype", {}).get(a, [])
                for a in _KNOWN_ARCHETYPES
            }
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
                    "by_archetype": self._by_archetype,
                    "updated_at": self._updated_at,
                    "trades_reviewed": self._trades_reviewed,
                    "total_trades_ever": self._total_trades_ever,
                },
                f,
                indent=2,
            )
