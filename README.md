# AlphaDesk

A predictive **multi-agent stock research engine**. You trigger a run; it reads a wide
window of world + financial news, a **team** of specialized LLM agents debates the best
opportunities live, a **Head** ranks them head-to-head, and every call is written
to a self-grading ledger that scores itself forward against reality.

**Research / paper only — no order execution.** All LLM calls run on a **Claude Max
subscription** via `claude-agent-sdk` — no API keys, no Bedrock, no local model files.

---

## Alpha thesis — three slow-digestion edges

Markets price headlines in seconds but digest three things slowly. Predictions live in that lag:

- **SPILLOVER** — a shocked company reprices instantly; its suppliers/customers/competitors
  drift for days. A web-grounded Connections desk finds the connected, *unmoved* names.
- **THEME** — investment themes build over days; mention-velocity leads the crowd.
- **MOMENTUM** — big moves continue for days; bet the continuation.

Every pick declares `direction · horizon_days (1–10) · edge · confidence` and is graded at
exactly that horizon vs SPY, net of friction.

---

## How it works

```
Polygon (financial) + GDELT (world news) + Alpaca/yfinance (price context)
        │  candidates (symbol → enriched articles)
   [Connections desk]  shock → 3 web-grounded specialists → synth → spillover candidates
        │
   SCOUT ── picks ≤5, with a reason for every pick AND skip
        │  per pick, in parallel:
   2 notes (market · news) + the desk's own calibration scorecard
        │
   RESEARCHER → CRITIC → fact-check → RESEARCHER rebuttal → JUDGE      (adversarial debate)
   every 3rd pick → LONER control arm  (does the team actually beat one good agent?)
        │
   HEAD → head-to-head ranking, TAKE / pass
        │
   LEDGER (SQLite) → GRADER (hourly, alpha vs SPY at each pick's own horizon)
```

Model tiering decorrelates errors: **haiku** for enrichment/notes, **sonnet** for
scout/researcher, **opus** for critic/judge/head. Researcher and Critic run *different*
models on purpose so the critic isn't just agreeing with itself.

---

## Quick start

```bash
pip install -r requirements.txt

# Web dashboard + hourly grader (primary mode — trades run on a button click)
python -m alphadesk.main dashboard        # http://localhost:8000

# Or convene the team now, headless
python -m alphadesk.main desk

# Rebuild the web UI after editing it
cd alphadesk/ui && pnpm build
```

### Environment

```ini
ALPACA_API_KEY=...        # market data + tradable universe (paper keys are fine)
ALPACA_SECRET_KEY=...
POLYGON_API_KEY=...       # financial news (optional)
ADMIN_USERNAME=admin      # dashboard Basic Auth (fail-closed if unset)
ADMIN_PASSWORD=...
ALPHADESK_DATA=~/.alphadesk   # ledger.db, universe cache, enrichment cache
SOLO_ARM_EVERY_N=0            # 0=off; set e.g. 6 to measure team-vs-loner
```

---

## Design principles

- **Agents own judgment; code owns facts, physics, safety, and scoring.** No hardcoded
  judgment thresholds — the LLM assesses signals from raw data; code owns arithmetic,
  tradability, injection defense, schema validation, and the universe whitelist.
- **Attention is information-driven, never price-driven.** Price informs a decision; it
  never triggers one. Decisions come from causes, not price-narration.
- **Forward-only evidence.** The system earns trust from its graded ledger, not its prose,
  with pre-committed kill criteria for every component — including the debate and itself.
- **Grounded self-improvement, not RL.** A numeric calibration scorecard is fed back into
  agent prompts; the real self-correction is the kill criteria (drop the debate / an edge /
  the team if the ledger says they don't pay). No free-form "lessons" memory.

---

## Status

Early / unproven by design. The engine is built and unit-verified, but the forward-only
ledger has **zero graded trades** yet — so the calibration prior, kill criteria, and the
alpha thesis itself are all dormant until real picks accumulate and get graded.

## Disclaimer

For educational and informational purposes only. Not financial advice. This system does
not place trades. Algorithmic trading carries significant risk of loss.
