"""PromptTunerAgent — rewrites agent system prompts based on learned trade patterns.

Subscribes to: memory.updated
Publishes to:  prompts.updated

After each LearningAgent reflection cycle, uses Claude Sonnet to incorporate the
latest lessons directly into Predictor, Critic, and Risk agent system prompts.
This goes further than lesson injection (a prefix): it rewrites the prompt's
actual instructions so the lesson becomes part of the agent's standing behaviour.

Optimized prompts are persisted to ~/.stock_screener/agent_prompts.json and
loaded at call time by each agent (falling back to the hardcoded default).
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory

_PROMPTS_FILE = os.path.expanduser("~/.stock_screener/agent_prompts.json")
_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_TUNER_SYSTEM = (
    "You are a prompt engineer for an LLM-based algorithmic trading system. "
    "You receive a current agent system prompt and lessons learned from real trade outcomes. "
    "Rewrite the prompt to incorporate the lessons as concrete, specific trading instructions.\n\n"
    "Rules:\n"
    "  • Keep the same overall structure and purpose of the prompt\n"
    "  • Preserve the JSON output format specification at the end EXACTLY as written\n"
    "  • Convert each lesson into a specific instruction with clear thresholds or actions\n"
    "  • Remove or weaken instructions the lessons show were consistently wrong\n"
    "  • Do not pad the prompt or add verbose commentary\n"
    "  • Do not add markdown, headers, or code fences\n"
    "Return ONLY the rewritten prompt text — no preamble, no explanation."
)


def load_optimized_prompt(key: str, fallback: str) -> str:
    """Return the tuner-optimized prompt for this key if available, else the hardcoded fallback."""
    try:
        if os.path.exists(_PROMPTS_FILE):
            with open(_PROMPTS_FILE) as fh:
                prompts = json.load(fh)
            optimized = prompts.get(key, "")
            if len(optimized) > 100:
                return optimized
    except Exception:
        pass
    return fallback


class PromptTunerAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "PromptTunerAgent")
        self._queue = bus.subscribe("memory.updated")
        self._memory = memory
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    async def run(self) -> None:
        while True:
            await self._queue.get()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._tune_all_prompts)

    # ------------------------------------------------------------------
    # Tune all three agent prompts in one reflection cycle
    # ------------------------------------------------------------------

    def _tune_all_prompts(self) -> dict | None:
        lessons = self._memory.format_for_prompt()
        if not lessons:
            self.log.debug("No lessons yet — skipping prompt tuning")
            return None

        current = self._load_prompts()

        # Lazy imports inside function body to avoid circular dependency
        # (predictor/critic/risk import load_optimized_prompt from this module)
        from stock_sentiment.agents.predictor import _SYSTEM_PROMPT as _PRED
        from stock_sentiment.agents.critic import _SYSTEM_PROMPT as _CRIT
        from stock_sentiment.agents.risk import _RISK_SYSTEM_PROMPT as _RISK

        targets = [
            (
                "predictor_system",
                current.get("predictor_system", _PRED),
                "PredictorAgent — scores a single stock for swing trading using a 4-step process",
            ),
            (
                "critic_system",
                current.get("critic_system", _CRIT),
                "CriticAgent — adversarial reviewer that stress-tests trade proposals to find failure reasons",
            ),
            (
                "risk_system",
                current.get("risk_system", _RISK),
                "RiskAgent — makes the final APPROVE/BLOCK decision before any order is placed",
            ),
        ]

        updated = dict(current)
        changed_keys: list[str] = []

        for key, prompt, label in targets:
            optimized = self._optimize(label, prompt, lessons)
            if optimized and optimized != prompt:
                updated[key] = optimized
                changed_keys.append(key)
                self.log.info(
                    "Tuned prompt: %s (%d → %d chars)", key, len(prompt), len(optimized)
                )

        if not changed_keys:
            self.log.debug("No prompt changes from this reflection cycle")
            return None

        generation = current.get("generation", 0) + 1
        updated["updated_at"] = datetime.now(_ET).isoformat()
        updated["generation"] = generation
        self._save_prompts(updated)
        self.log.info(
            "Prompt tuning complete — generation %d, updated: %s",
            generation, ", ".join(changed_keys),
        )
        return {"generation": generation, "keys_updated": changed_keys}

    # ------------------------------------------------------------------
    # Single-prompt optimization — blocking Sonnet call
    # ------------------------------------------------------------------

    def _optimize(self, agent_label: str, current_prompt: str, lessons: str) -> str | None:
        user_text = (
            f"Agent: {agent_label}\n\n"
            f"Current prompt:\n{current_prompt}\n\n"
            f"Lessons learned from real trade outcomes:\n{lessons}\n\n"
            "Rewrite the prompt to incorporate these lessons as concrete, specific instructions. "
            "Preserve the JSON output format specification at the end exactly as written."
        )
        try:
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _TUNER_SYSTEM}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": 2000, "temperature": 0.2},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            text = content[0]["text"].strip() if content else ""
            if len(text) < 100:
                self.log.warning(
                    "Prompt tune returned suspiciously short output for %s — discarding",
                    agent_label,
                )
                return None
            return text
        except Exception as exc:
            self.log.warning("Prompt tune failed for %s: %s", agent_label, exc)
            return None

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_prompts() -> dict:
        try:
            if os.path.exists(_PROMPTS_FILE):
                with open(_PROMPTS_FILE) as fh:
                    return json.load(fh)
        except Exception:
            pass
        return {}

    @staticmethod
    def _save_prompts(prompts: dict) -> None:
        try:
            os.makedirs(os.path.dirname(_PROMPTS_FILE), exist_ok=True)
            with open(_PROMPTS_FILE, "w") as fh:
                json.dump(prompts, fh, indent=2)
        except Exception:
            pass
