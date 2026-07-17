# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**AlphaDesk** — a predictive multi-agent stock research engine. You trigger a run
("Find Trades"); it reads a wide window of world + financial news, a committee of
specialized LLM agents debates the best opportunities live, a Chief Strategist ranks
them head-to-head, and every call is written to a self-grading ledger that scores
itself forward against reality. **Research / paper only — no order execution.**

All LLM calls run on the **Claude Max subscription** via `claude-agent-sdk` (the
bundled Claude Code CLI). There is no Bedrock, no API key, no local model files.

> The legacy `stock_sentiment/` bot (FinBERT + AWS Bedrock) was removed 2026-07-16.
> AlphaDesk (`alphadesk/`) is the only system in this repo.

## Commands

```bash
pip install -r requirements.txt

# Web dashboard + hourly grader (v2 primary mode — trades run on button click)
python -m alphadesk.main dashboard        # then open http://localhost:8000

# Convene the committee NOW on recent news (headless, writes to ledger)
python -m alphadesk.main desk

# One GDELT world-news tick
python -m alphadesk.main world

# Grade due picks / print the scorecard / one-month news backfill
python -m alphadesk.main grade
python -m alphadesk.main status
python -m alphadesk.main backfill

# Legacy autonomous 24/7 scheduler (kept, not the v2 path)
python -m alphadesk.main run

# Rebuild the web UI (React → alphadesk/app/static/)
cd alphadesk/ui && pnpm build
```

## Design laws (every module obeys these)

1. **Agents own judgment; code owns facts, physics, safety, and scoring.** No
   hardcoded judgment thresholds — triage has no RVOL cutoff, the score has no
   formula. Code owns arithmetic, hard facts (tradability), and rails (caps,
   injection defense, schema validation).
2. **Attention is information-driven, never price-driven.** Price *informs* a
   decision; it never *triggers* one. Decisions come from causes (news), not
   price-narration.
3. **Forward-only evidence.** Every pick declares `direction · horizon_days(1–10)
   · edge · confidence` and is graded at exactly that horizon vs SPY, net of
   friction. The system earns trust from its ledger, not its prose.

## Alpha thesis — three slow-digestion edges

- **RIPPLE** — a shocked company reprices instantly; its suppliers/customers/
  competitors drift for days (the Exposure Desk finds the connected, unmoved names).
- **NARRATIVE** — investment themes build over days; mention-velocity leads the crowd.
- **DRIFT** — big moves continue for days; bet the continuation.

## Architecture

Two **entry points** run the same committee (they have partially diverged — see
Tech debt):

- `desk/stream.py` — the on-demand **"Find Trades"** SSE flow (dashboard button).
  **v2's primary path.** Streams the agents' deliberation live to the browser.
- `desk/workflow.py` — `research_run()`, the pure batch pipeline (the `desk` CLI,
  the scheduler's autonomous mode, and future replay). Returns ledger IDs only.

### Pipeline

```
Polygon (financial) + GDELT (world, 11-cat) + Alpaca/yfinance (price context)
        │  candidates (symbol → enriched articles)
   [Exposure Desk]  (expose=true) shock → 3 web-grounded specialists → synth → ripple candidates
        │
   TRIAGE (sonnet)  ── picks ≤5, reasons for every pick AND skip
        │  per pick, in parallel:
   4 BRIEFS (haiku): technical · news · fundamentals · freshness   (workflow.py: technical · news · graph)
   + calibration prior (the desk's own graded scorecard, sample-gated at 8 trades)
        │
   ANALYST (sonnet) → SKEPTIC (opus) → fact-check (code) → ANALYST rebuttal → ARBITER (opus)
   every 3rd pick → SOLO (opus) control arm (kill-criterion: does the committee beat one agent?)
        │
   CHIEF STRATEGIST (opus) → head-to-head ranking, TAKE/pass
        │
   LEDGER (SQLite/WAL) → GRADER (hourly, alpha_net vs SPY at own horizon)
```

### Model tiering (`config.MODEL_MAP`, every role env-overridable `MODEL_<ROLE>`)

- **haiku**: enrichment, briefs (high-volume extraction)
- **sonnet**: triage, analyst, exposure_specialist
- **opus**: skeptic, arbiter, solo, chief, exposure_synth

Analyst is sonnet, Skeptic is opus **on purpose** — different models between debate
roles decorrelate errors. On rate-limit each role steps down opus→sonnet→haiku (tagged
on the ledger row); if the bottom tier is limited too, the breaker opens.

## The LLM layer — `llm.py` (every model call passes through `call_role`)

Guardrails, in order: model resolution (+ downgrade ladder) · injection defense
(`wrap_data` delimiters + `_INJECTION_GUARD`; web results tagged UNTRUSTED) · input-size
cap (`LLM_MAX_INPUT_CHARS`) · schema validation + one retry, then safe default (a failed
stage drops the candidate, never a phantom pick) · **universe whitelist** (invented
tickers rejected — the key output-security limit) · concurrency semaphore
(`LLM_MAX_CONCURRENCY`) + per-tool-call `max_budget_usd`/`max_turns` · token telemetry.

## File structure

```
alphadesk/
  config.py            MODEL_MAP, caps, sessions, tradable universe (weekly Alpaca cache)
  llm.py               the guarded call stack — every LLM call goes here
  ingest/
    news.py            Polygon poll → Haiku enrichment → candidates
    world.py           GDELT world-news (11-category taxonomy, action-over-talk gradient)
    prices.py          lazy per-symbol context — NO triggers, NO universe sweeps
  knowledge/graph.py   Neo4j world model (gated off by default: ALPHADESK_GRAPH)
  desk/
    stream.py          on-demand "Find Trades" SSE flow (v2 primary path)
    workflow.py        research_run() — batch pipeline (desk CLI, scheduler, replay)
    triage.py          all attention judgment, in one prompt
    briefs.py          4 parallel haiku brief subagents
    exposure.py        the Exposure Desk (web-grounded ripple mapping; replaces Neo4j)
    committee.py       Analyst ⇄ Skeptic → Arbiter, + calibration_block, + chief_synthesis
    solo.py            single-agent control arm
  ledger/
    store.py           SQLite/WAL: picks, funnel, token_usage, relationships
    grader.py          forward grading vs SPY, friction haircut — pure code
  app/
    dashboard.py       FastAPI + Basic Auth + SSE endpoint + static SPA
    scheduler.py       hourly grader loop (v2); legacy 24/7 loop (run mode)
  main.py              CLI entrypoint (dashboard/desk/world/grade/status/backfill/run)
  ui/                  React 19 + TS + Vite + shadcn/ui → built into app/static/
```

## Environment variables

```ini
ALPACA_API_KEY=...            # market data + universe (paper keys fine)
ALPACA_SECRET_KEY=...
POLYGON_API_KEY=...           # financial news (optional)
ADMIN_USERNAME=admin          # dashboard Basic Auth (fail-closed if unset)
ADMIN_PASSWORD=...
ALPHADESK_DATA=~/.alphadesk   # ledger.db, universe.json, relationship cache
ALPHADESK_GRAPH=off           # set on/1/true to enable the Neo4j graph
```

## Key design notes

- **No order execution** — research/paper only until the ledger earns it.
- **Self-improvement is grounded, not RL**: a numeric calibration scorecard is fed
  into agent prompts (dormant until ~8 graded trades); the real self-correction is the
  pre-committed **kill criteria** (drop the debate / an edge / the committee if the
  ledger says they don't pay). No free-form "lessons" memory (persistent injection risk).
  NB: this is NOT the agent learning — the model is frozen; the loop builds evidence so
  *humans* retune. In-context feedback changing behavior is itself an unproven experiment.
- **Anti-survivorship** — the ledger grades REJECTED picks (counterfactuals), and
  `grader.grade_skips()` grades triage SKIPS too (directionless: a move vs SPY over
  `SKIP_GRADE_DAYS` above `SKIP_MISS_ABS_ALPHA`% = a dislocation we ignored). `committee.
  false_negative_block()` feeds the reject/skip miss-rate into triage + arbiter — sample-
  gated, removable, tagged as an experiment.
- **Miss diagnosis is conversational** — ask Claude "why did we miss X?"; it traces
  `store.symbol_traces` / `symbol_skips` and fixes data/prompt/bug. No UI tool for it.
- **Position review (exits)** — each run, BEFORE hunting new trades, re-checks every
  still-open TAKE (`store.open_taken_picks`) against current price + fresh news via the
  opus `reeval` agent → HOLD or EXIT with a reason, surfaced first (you may have traded
  it). Exits are stamped (`exit_ts`/`exit_reason`); HOLD is the fail-safe default. The
  committee opens positions; `desk/reeval.py` is the only thing that closes them early.

## Tech debt / honest status

- **Committee core is converged** (`desk/debate.py`): both entry points run the same
  `deliberate()` async generator for the analyst→skeptic→arbiter→ledger-write sequence,
  so the core debate can't drift. What still differs per entry point *by design*:
  brief-gathering (`stream.py` = fundamentals/freshness; `workflow.py` = the Neo4j graph
  brief) and the solo-arm record (lightly duplicated). Unify those too if the CLI/replay
  path ever needs to match the button exactly.
- **Unproven.** The full deep-scan path has not been run end-to-end live, and there
  are **zero graded trades** — so the calibration prior, kill criteria, and the entire
  alpha thesis are dormant. The highest-value next step is a supervised live run to
  start the forward-only ledger clock.
