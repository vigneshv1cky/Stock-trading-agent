# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**AlphaDesk** — a predictive multi-agent stock research engine. You trigger a run
("Find Trades"); it reads a wide window of financial news + earnings (world news
optional, off by default), a **team** of specialized LLM agents debates the best
opportunities live, a **Head** ranks
them head-to-head, and every call is written to a self-grading ledger that scores
itself forward against reality. **Research / paper only — no order execution.**

> **Plain-word vocabulary (2026-07-18):** the agent roles were renamed from
> trading jargon to plain words. Old→new: Triage→**Scout**, Analyst→**Researcher**,
> Skeptic→**Critic**, Arbiter→**Judge**, Chief→**Head**, Solo→**Loner**,
> Exposure→**Connections**. DB codes: arm COMMITTEE→**TEAM**/SOLO→**LONER**;
> edge RIPPLE→**SPILLOVER**/NARRATIVE→**THEME**/DRIFT→**MOMENTUM**/WORLD_EVENT→**WORLD**;
> verdict CONFIRM→**STRONG**/WEAKEN→**SOFT**/REJECT→**PASS**.

All LLM calls run on the **Claude Max subscription** via `claude-agent-sdk` (the
bundled Claude Code CLI). There is no Bedrock, no API key, no local model files.

> The legacy `stock_sentiment/` bot (FinBERT + AWS Bedrock) was removed 2026-07-16.
> AlphaDesk (`alphadesk/`) is the only system in this repo.

## Commands

```bash
pip install -r requirements.txt

# Web dashboard + hourly grader (v2 primary mode — trades run on button click)
python -m alphadesk.main dashboard        # then open http://localhost:8000

# Convene the team NOW on recent news (headless, writes to ledger)
python -m alphadesk.main desk

# One GDELT world-news tick
python -m alphadesk.main world

# Grade due picks / print the scorecard / one-month news backfill
python -m alphadesk.main grade
python -m alphadesk.main status
python -m alphadesk.main backfill

# Reaction-gate A/B: forward alpha vs SPY bucketed by reaction size (is the gate
# filtering noise or cutting quiet under-reactions? also reveals the right threshold)
python -m alphadesk.main abtest

# Legacy autonomous 24/7 scheduler (kept, not the v2 path)
python -m alphadesk.main run

# Rebuild the web UI (React → alphadesk/app/static/)
cd alphadesk/ui && pnpm build
```

## Design laws (every module obeys these)

1. **Agents own judgment; code owns facts, physics, safety, and scoring.** No
   hardcoded judgment thresholds — the scout has no RVOL cutoff, the score has no
   formula. Code owns arithmetic, hard facts (tradability), and rails (caps,
   injection defense, schema validation).
2. **Attention is information-driven, never price-driven.** Price *informs* a
   decision; it never *triggers* one. Decisions come from causes (news), not
   price-narration.
3. **Forward-only evidence.** Every pick declares `direction · horizon_days(1–10)
   · edge · confidence` and is graded at exactly that horizon vs SPY, net of
   friction. The system earns trust from its ledger, not its prose.

## Alpha thesis — three slow-digestion edges

- **SPILLOVER** — a shocked company reprices instantly; its suppliers/customers/
  competitors drift for days (the Connections desk finds the connected, unmoved names).
- **THEME** — investment themes build over days; mention-velocity leads the crowd.
- **MOMENTUM** — big moves continue for days; bet the continuation.

## Architecture

Two **entry points** run the same team (they have partially diverged — see
Tech debt):

- `desk/stream.py` — the on-demand **"Find Trades"** SSE flow (dashboard button).
  **v2's primary path.** Streams the agents' deliberation live to the browser.
- `desk/workflow.py` — `research_run()`, the pure batch pipeline (the `desk` CLI,
  the scheduler's autonomous mode, and future replay). Returns ledger IDs only.

### Pipeline

```
Polygon (financial news) + earnings drift (+ since-report move) + Alpaca real-time last trade / yfinance history (price context)
        │  candidates (symbol → enriched articles)
        │  [+ GDELT world news if WORLD_MAX_CATEGORIES>0 — OFF by default]
   [Connections desk]  (expose=true) shock → 1 web-grounded opus call → spillover candidates
        │
   SCOUT (sonnet)  ── picks ≤5, reasons for every pick AND skip
        │
   GATE (haiku)  ── drop picks with no real external catalyst BEFORE the debate (fail-open)
        │  per surviving pick, in parallel:
   2 NOTES (haiku): market (price+valuation+priced-in, incl. realized-vs-implied "spent move" ratio) · news
   + calibration prior (the desk's own graded scorecard, sample-gated at 8 trades)
        │
   RESEARCHER (sonnet) → CRITIC (opus) → fact-check (code) → RESEARCHER rebuttal → JUDGE (opus)
   every 3rd pick → LONER (opus) control arm (kill-criterion: does the team beat one agent?)
        │
   HEAD (opus) → head-to-head ranking, TAKE/pass
        │
   LEDGER (SQLite/WAL) → GRADER (hourly, alpha_net vs SPY at own horizon)
        │
   POSITION WATCHER (~180s): target/stop cross → close (pure code);
     cheap give-back / near-target SCREEN → selective opus REVIEW → HOLD/EXIT (close a spent move before it decays)
```

### Model tiering (`config.MODEL_MAP`, every role env-overridable `MODEL_<ROLE>`)

- **haiku**: enrichment, notes/briefs, news_check, gate (high-volume extraction)
- **sonnet**: scout, researcher, earnings_reader
- **opus**: critic, judge, loner, head, review, connections (web-grounded)

Researcher is sonnet, Critic is opus **on purpose** — different models between debate
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
    earnings.py        Nasdaq earnings calendar → post-earnings-drift candidates the moment a report is PUBLIC
                       (NOT gated on eps_actual, which lags a day — direction from the price reaction, not the result)
    world.py           GDELT world-news (11-cat taxonomy) — OFF by default in Find Trades
                       (WORLD_MAX_CATEGORIES=0); still used by the scheduler + `world` CLI
    prices.py          lazy per-symbol context — real-time Alpaca last trade (yfinance history fallback); NO triggers, NO sweeps
  desk/
    stream.py          on-demand "Find Trades" SSE flow (v2 primary path)
    workflow.py        research_run() — batch pipeline (desk CLI, scheduler, replay)
    debate.py          deliberate() — the shared Researcher→Critic→Judge core
    scout.py           all attention judgment, in one prompt (was triage.py)
    gate.py            pre-debate catalyst screen — drop phantom setups (haiku, fail-open)
    notes.py           2 parallel haiku note subagents: market (incl. realized-vs-implied spent-move ratio), news (was briefs.py)
    connections.py     the Connections desk (web-grounded spillover mapping; was exposure.py)
    team.py            Researcher ⇄ Critic → Judge, + calibration_block, + head_ranking (was committee.py)
    loner.py           single-agent control arm (was solo.py)
    plan.py            trade plan (entry/target/stop, agent) + level_crossed / exit_signal / realized_exit (pure-code exit physics)
    review.py          position review — HOLD/EXIT on open TAKEs, per run + between-run watcher escalations (was reeval.py)
    news_check.py      same-story vs new-catalyst check on a recently-debated name
    earnings_reader.py web-grounded read of an actual earnings report
  ledger/
    store.py           SQLite/WAL: picks (+ exit/mfe/source cols), earnings, funnel, token_usage, relationships
    grader.py          forward grading vs SPY + MFE/MAE paths + skip-grading — pure code
  app/
    dashboard.py       FastAPI + Basic Auth + SSE endpoint + static SPA
    scheduler.py       hourly grader loop (v2); legacy 24/7 loop (run mode)
  main.py              CLI entrypoint (dashboard/desk/world/grade/status/backfill/run) + position watcher (level cross + give-back screen → review)
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
SOLO_ARM_EVERY_N=0            # 0=off (lean default); set e.g. 6 to measure committee-vs-solo
WORLD_MAX_CATEGORIES=0        # GDELT world news in Find Trades: 0=off (default); 4=full sweep every ~3 runs; 11=every run (slow)
MATERIAL_REACTION_PCT=1.5     # earnings drift needs a visible reaction to be a directional candidate; below this % (live vs pre-report close) = skip
REACTION_AB_HORIZON_DAYS=3    # shadow A/B: forward-grade EVERY reporter's reaction (passed AND dropped) over this horizon → `abtest` shows if the gate cuts winners
# Exit-monitoring screens (tunable; the opus reviewer is the real filter — defaults escalate generously):
EXIT_NEAR_TARGET_FRAC=0.85    # ≥ this much of the entry→target move captured → escalate to review
EXIT_GIVEBACK_MIN_PEAK=4.0    # watch give-back only after the favorable move peaks above this % (below = noise)
EXIT_GIVEBACK_FRAC=0.40       # faded ≥ this fraction of that peak → escalate (MFE-decay flag)
EXIT_REVIEW_COOLDOWN_S=1800   # min seconds between reviews of the same open position
```

## Key design notes

- **No order execution** — research/paper only until the ledger earns it.
- **Self-improvement is grounded, not RL**: a numeric calibration scorecard is fed
  into agent prompts (dormant until ~8 graded trades); the real self-correction is the
  pre-committed **kill criteria** (drop the debate / an edge / the team if the
  ledger says they don't pay). No free-form "lessons" memory (persistent injection risk).
  NB: this is NOT the agent learning — the model is frozen; the loop builds evidence so
  *humans* retune. In-context feedback changing behavior is itself an unproven experiment.
- **Anti-survivorship** — the ledger grades REJECTED picks (counterfactuals), and
  `grader.grade_skips()` grades scout SKIPS too (directionless: a move vs SPY over
  `SKIP_GRADE_DAYS` above `SKIP_MISS_ABS_ALPHA`% = a dislocation we ignored). `team.
  false_negative_block()` feeds the reject/skip miss-rate into scout + judge — sample-
  gated, removable, tagged as an experiment.
- **Miss diagnosis is conversational** — ask Claude "why did we miss X?"; it traces
  `store.symbol_traces` / `symbol_skips` and fixes data/prompt/bug. No UI tool for it.
- **The material-reaction gate is A/B-tested, not assumed** — the gate that drops
  earnings reporters with a sub-`MATERIAL_REACTION_PCT` reaction could be filtering noise
  OR discarding the quiet under-reactions that ARE the drift edge. So `earnings.
  drift_candidates` logs EVERY public reporter's reaction (passed AND dropped) to
  `earnings_reactions`, and `grader.grade_reactions()` forward-grades both arms vs SPY in
  the reaction direction (same Model-A entry + benchmark + friction as booked picks) over
  `REACTION_AB_HORIZON_DAYS`. `abtest` buckets the graded rows by reaction size: if
  forward alpha turns on at the threshold the gate is justified (and shows the right
  threshold); if the dropped arm pays as well, the gate is cutting winners. No LLM cost —
  a simultaneous, same-tape shadow A/B, dormant as evidence until the sample is real.
- **Spent-move symmetry** — "how much of the expected move is left?" is asked at BOTH
  ends. At ENTRY the market note gets an explicit realized-vs-implied ratio (today/5d
  move ÷ options-implied move) plus the earnings since-report move, so a fully-repriced
  setup reads "spent → pass" (the fix for entering a gap that already happened). At EXIT
  the give-back screen closes a position once the *remaining* move plays out. Both are
  evidence the agents weigh (not gates), and the ratio only fires where options data
  exists (liquid names); thin names fall back to the qualitative priced-in read.
- **Gap vs capturable drift** — `prices.moves_since_report` splits the move since a
  report into the uncapturable overnight **gap** (pre-report close → first post-report
  OPEN — repriced before you could act) and the **drift** (from that open — what you
  could actually trade). Entry candidates, the Calendar "Move" column, and the true/
  false-miss verdict all key on the **drift**, so a pure-gap reprice isn't counted as a
  tradeable miss. NB: the exit/hold side is unchanged — a position held *through* a gap
  DID capture it, so its P&L/MFE still measure from the original entry.
- **Same-day earnings visibility** — the drift pool (`store.recently_reported`) is NOT
  gated on `eps_actual` (Nasdaq backfills it ~a day late, which hid every same-day
  reporter); a reporter becomes a candidate the moment it's PUBLIC (time-aware, past its
  9:30/16:00 boundary) and its direction comes from the price reaction. Run Find Trades
  just AFTER 9:30 so BMO reporters are public. (Prioritization — the scout still picks ≤5
  of ~150 — is a separate, unbuilt lever.)
- **Position review (exits)** — the team only opens positions; three things close them
  early, all research/paper (a ledger `exit_ts`/`exit_reason` stamp, never an order):
  (1) each Find Trades run, BEFORE hunting new trades, the opus `review` agent re-checks
  every open TAKE (`store.open_taken_picks`) vs price + fresh news → HOLD/EXIT, surfaced
  first (you may have traded it); (2) the **position watcher** (`main._position_watch_loop`,
  ~180s) closes on a target/stop level cross (pure code); (3) between runs, a cheap code
  SCREEN (`plan.exit_signal`: near-target, or MFE give-back seeded from the persisted
  `mfe_pct` so it survives restarts) flags a spent move and escalates that ONE position to
  the same opus reviewer — so a played-out move is closed before the gain decays, not only
  on the next run. HOLD is always the fail-safe default; escalation is throttled per
  position (`EXIT_REVIEW_COOLDOWN_S`).

## Tech debt / honest status

- **Team core is converged** (`desk/debate.py`): both entry points run the same
  `deliberate()` async generator for the researcher→critic→judge→ledger-write sequence,
  and now the same notes (market + news), so they no longer drift. Only the loner-arm
  record is still lightly duplicated between them.
- **Unproven.** The ledger clock is running but the sample is tiny and shows **no edge
  yet** (~28 graded as of 2026-07-22, direction ≈ 43% ≈ coin-flip, mean alpha negative —
  statistically indistinguishable from zero). The calibration prior and kill criteria stay
  dormant until the sample is large enough. A **stale-price bug** (fixed 2026-07-22) had
  inflated early paper-exit P&L and priced-in reasoning by anchoring to yfinance's stale
  daily close the morning after an earnings gap; the forward grade (`alpha_net`) was never
  affected (it enters at the real next-session open). Highest-value next step: let the
  current honestly-priced cohort grade to a real read before changing anything.
