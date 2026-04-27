"""Learns optimal scoring weights per archetype from historical backtest outcomes."""

import json
import os

import numpy as np

WEIGHTS_FILE = os.path.expanduser("~/.stock_screener/weights.json")
WEIGHT_KEYS = ["momentum", "volume", "technical", "sentiment"]
MIN_SAMPLES = 50          # global minimum before any optimization runs
MIN_ARCHETYPE_SAMPLES = 20  # per-archetype minimum; falls back to global below this

DEFAULT_WEIGHTS: dict[str, list[float]] = {
    "MOMENTUM": [0.30, 0.20, 0.25, 0.25],
    "BREAKOUT":  [0.30, 0.20, 0.25, 0.25],
    "RECOVERY":  [0.30, 0.20, 0.25, 0.25],
    "default":   [0.30, 0.20, 0.25, 0.25],
}


def load_weights() -> dict[str, list[float]]:
    """Load per-archetype weights from disk, falling back to defaults.

    Handles two legacy formats:
      - Old list  : [0.30, 0.20, 0.25, 0.25]
      - Old dict  : {"momentum": 0.30, "volume": 0.20, ...}  (lowercase keys)
    New format    : {"MOMENTUM": [...], "BREAKOUT": [...], "default": [...]}
    """
    try:
        if os.path.exists(WEIGHTS_FILE):
            with open(WEIGHTS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                w = [float(x) for x in data]
                return {k: w[:] for k in DEFAULT_WEIGHTS}
            if isinstance(data, dict):
                if "momentum" in data:  # old lowercase format
                    w = [float(data[k]) for k in WEIGHT_KEYS]
                    return {k: w[:] for k in DEFAULT_WEIGHTS}
                # new archetype-keyed format
                result = {k: v[:] for k, v in DEFAULT_WEIGHTS.items()}
                result.update({k: [float(x) for x in v] for k, v in data.items() if k in result})
                return result
    except Exception:
        pass
    return {k: v[:] for k, v in DEFAULT_WEIGHTS.items()}


def save_weights(weights: dict[str, list[float]]):
    os.makedirs(os.path.dirname(WEIGHTS_FILE), exist_ok=True)
    with open(WEIGHTS_FILE, "w") as f:
        json.dump({k: [round(w, 4) for w in v] for k, v in weights.items()}, f, indent=2)
    for archetype, w in weights.items():
        named = dict(zip(WEIGHT_KEYS, [f"{x:.3f}" for x in w]))
        print(f"[WeightOptimizer] {archetype:10s}: {named}")


class WeightOptimizer:
    """Fits scoring weights per archetype that maximise directional accuracy on backtest outcomes.

    Run `python run.py --optimize` after accumulating history.
    Global fit requires MIN_SAMPLES outcomes; per-archetype fit requires MIN_ARCHETYPE_SAMPLES.
    """

    def __init__(self, history):
        self.history = history

    def optimize(self) -> dict[str, list[float]]:
        outcomes = self.history.get_outcomes_with_subscores()
        valid = [
            o for o in outcomes
            if o.get("ret_5d_pct") is not None and o.get("momentum_score") is not None
        ]

        if len(valid) < MIN_SAMPLES:
            print(f"[WeightOptimizer] {len(valid)}/{MIN_SAMPLES} scored outcomes. Keeping current weights.")
            return load_weights()

        weights = load_weights()

        # --- Global default ---
        global_w = self._fit(valid)
        weights["default"] = global_w
        print(f"[WeightOptimizer] Global ({len(valid)} outcomes) → accuracy {self._accuracy(valid, global_w):.1%}")

        # --- Per-archetype ---
        by_archetype: dict[str, list] = {}
        for o in valid:
            arch = o.get("archetype") or "default"
            by_archetype.setdefault(arch, []).append(o)

        for arch in ("MOMENTUM", "BREAKOUT", "RECOVERY"):
            arch_outcomes = by_archetype.get(arch, [])
            if len(arch_outcomes) < MIN_ARCHETYPE_SAMPLES:
                print(f"[WeightOptimizer] {arch}: {len(arch_outcomes)}/{MIN_ARCHETYPE_SAMPLES} outcomes, using global")
                weights[arch] = global_w[:]
            else:
                w = self._fit(arch_outcomes)
                weights[arch] = w
                print(f"[WeightOptimizer] {arch} ({len(arch_outcomes)} outcomes) → accuracy {self._accuracy(arch_outcomes, w):.1%}")

        save_weights(weights)
        return weights

    # ------------------------------------------------------------------

    def _fit(self, outcomes: list[dict]) -> list[float]:
        """Find weights that maximise directional accuracy on these outcomes."""
        X = np.array([
            [o["momentum_score"], o["volume_score"], o["technical_score"], o["sentiment_score"]]
            for o in outcomes
        ], dtype=float)
        y = np.sign([float(o["ret_5d_pct"]) for o in outcomes])

        def neg_acc(w_raw: np.ndarray) -> float:
            w = np.abs(w_raw)
            w = w / w.sum()
            scores = X @ w
            return -float(np.mean(((scores >= 60) & (y > 0)) | ((scores <= 40) & (y < 0))))

        start = np.array(load_weights().get("default", DEFAULT_WEIGHTS["default"]))
        best_w, best_neg = start.copy(), neg_acc(start)

        try:
            from scipy.optimize import minimize
            result = minimize(neg_acc, x0=start, method="Nelder-Mead",
                              options={"maxiter": 2000, "xatol": 1e-4})
            candidate = np.abs(result.x)
            candidate /= candidate.sum()
            if neg_acc(candidate) < best_neg:
                best_w, best_neg = candidate, neg_acc(candidate)
        except ImportError:
            rng = np.random.default_rng(42)
            for _ in range(8000):
                w = rng.dirichlet(np.ones(4))
                if neg_acc(w) < best_neg:
                    best_neg, best_w = neg_acc(w), w

        return (np.abs(best_w) / np.abs(best_w).sum()).tolist()

    def _accuracy(self, outcomes: list[dict], weights: list[float]) -> float:
        X = np.array([
            [o["momentum_score"], o["volume_score"], o["technical_score"], o["sentiment_score"]]
            for o in outcomes
        ], dtype=float)
        y = np.sign([float(o["ret_5d_pct"]) for o in outcomes])
        w = np.array(weights, dtype=float)
        w /= w.sum()
        scores = X @ w
        return float(np.mean(((scores >= 60) & (y > 0)) | ((scores <= 40) & (y < 0))))
