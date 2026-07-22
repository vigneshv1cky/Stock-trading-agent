# AlphaDesk

A predictive **multi-agent stock research engine**. You trigger a run; it reads a wide
window of financial news + earnings (world news optional, off by default), a **team** of
specialized LLM agents debates the best opportunities live, a **Head** ranks them
head-to-head, and every call is written to a self-grading ledger that scores itself
forward against reality.

**Research / paper only — no order execution.** All LLM calls run on a **Claude Max
subscription** via `claude-agent-sdk` (the bundled Claude Code CLI) — no API keys, no
Bedrock, no local model files.

---

## Table of contents

- [The idea](#the-idea)
- [Alpha thesis — three slow-digestion edges](#alpha-thesis--three-slow-digestion-edges)
- [How a run works](#how-a-run-works)
- [The agent team](#the-agent-team)
- [Model tiering](#model-tiering)
- [The LLM guardrail stack](#the-llm-guardrail-stack)
- [The ledger and forward grading](#the-ledger-and-forward-grading)
- [Anti-survivorship](#anti-survivorship)
- [Grounded self-improvement, not RL](#grounded-self-improvement-not-rl)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [Design principles](#design-principles)
- [Status](#status)
- [Disclaimer](#disclaimer)

---

## The idea

Markets price a headline in seconds, but the *consequences* of that headline take days to
propagate. A supplier hasn't repriced yet. A theme is still building. A big move has more
room to run. AlphaDesk is a machine for finding and betting on those lags — and, crucially,
for **keeping score of whether it was right**.

It is not a signal generator that emits numbers. It is a simulated research desk: a scout
allocates attention, specialists write briefs, a researcher argues a thesis, a critic
attacks it (and may flip it), a judge rules, and a head decides what actually gets
committed. Then reality grades the whole thing at the horizon each call declared for
itself. The system is designed to **earn trust from its ledger, not its prose**.

---

## Alpha thesis — three slow-digestion edges

Predictions live in the lag between a headline and its full digestion:

- **SPILLOVER** — a shocked company reprices instantly; its suppliers, customers, and
  competitors drift for days. A web-grounded **Connections desk** maps the shock to the
  connected, *still-unmoved* names and proposes the implied trade.
- **THEME** — investment themes build over days; mention-velocity leads the crowd.
- **MOMENTUM** — big moves (especially post-earnings) continue for days; bet the
  continuation in the direction of the *reaction*, not the *result*.

Every pick declares `direction · horizon_days (1–10) · edge · confidence` and is graded at
exactly that horizon versus SPY, net of trading friction. Nothing is graded on vibes.

---

## How a run works

Clicking **Find Trades** on the dashboard (or running `alphadesk desk` headless) drives one
pass of the pipeline. Both entry points share the same debate core (`desk/debate.py`) so
they can't drift apart.

```
Polygon (financial news) + earnings drift (+ since-report move) + Alpaca real-time last trade / yfinance history
        │  candidates: symbol → enriched articles
        │  GDELT world news is OFF by default (set WORLD_MAX_CATEGORIES>0 to enable)
        │
   [Position review]  re-check every still-open pick FIRST → HOLD / EXIT      (opus)
        │
   [Earnings drift]   names that reported in the last few days → candidates
   [Connections desk] top-N shocks → 1 web-grounded call each → spillover candidates  (opus)
        │
   [Anti-double-dip]  drop names already held, or same-story within the cooldown
        │
   SCOUT ── picks ≤5, with a one-line reason for every pick AND every skip      (sonnet)
        │
   GATE  ── drop picks with no real external catalyst, in parallel, fail-open   (haiku)
        │  per surviving pick:
   2 NOTES (market — incl. realized-vs-implied "spent move" ratio · news) + optional earnings read + calibration scorecard
        │
   RESEARCHER → CRITIC → code fact-check → RESEARCHER rebuttal → JUDGE          (debate)
   PLAN: entry / target / stop / instruction for the committed call            (sonnet)
   every Nth pick → LONER control arm  (does the team actually beat one agent?) (opus)
        │
   HEAD → genuine head-to-head ranking across all ideas, marks TAKE / pass      (opus)
        │
   LEDGER (SQLite/WAL) → GRADER (hourly, alpha_net vs SPY at each pick's own horizon)
        │
   POSITION WATCHER (~180s, between runs): target/stop cross → close (code);
     cheap give-back / near-target screen → selective opus REVIEW → HOLD / EXIT
```

Every intermediate step — each thesis, each concern, each rebuttal, each verdict — is
streamed to the browser over SSE, so you watch the desk think in real time rather than
waiting for a final answer.

---

## The agent team

| Role | Model | Job |
|------|-------|-----|
| **Scout** | sonnet | Sees every news-active symbol (headlines + sentiment + price context) and today's movers. Allocates the team's scarce attention: picks ≤5, with a reason for every pick *and* every skip. No thresholds — words, not numbers. |
| **Gate** | haiku | Cheap pre-debate screen. Drops picks with no verifiable external catalyst *before* spending the expensive debate. Fail-open (an error keeps the pick). |
| **Notes** | haiku | Two parallel briefs per pick — a *market* note (price, valuation, what's priced in, incl. an explicit realized-vs-implied "spent move" ratio where options data exists) and a *news* note. Feed the researcher. |
| **Earnings reader** | sonnet | If a pick just reported, a web-grounded read of the actual results / guidance / reaction. Cached per event. |
| **Connections** | opus | The web-grounded spillover desk. Maps a shock to its supplier/customer/competitor graph and proposes the connected, unmoved names to trade. |
| **Researcher** | sonnet | Forms the directional thesis from the briefs and the desk's own track record on the symbol. Picks the horizon that matches the mechanism. |
| **Critic** | opus | Attacks the thesis with evidence-cited concerns. Has the power to **FLIP** the call to the opposite side, **SUPPORT** it, or **STAND_ASIDE** — not just poke holes. |
| **Judge** | opus | Reads the full transcript, weighs argument quality, and always commits to a direction (LONG/SHORT, never neutral). `approved` marks a conviction call to size up vs a thin tracked lean. |
| **Plan** | sonnet | Turns the committed call into an actionable trade plan: entry, target, stop, one-line instruction. Fail-open — a missing plan never blocks the pick. |
| **Loner** | opus | A single strong agent works the same briefs blind to the team. The control arm for kill-criterion #2: *does the committee actually beat one good agent?* Off by default. |
| **Head** | opus | Compares all debated ideas head-to-head on one common standard (isolated conviction scores aren't comparable), de-dups correlated bets and share classes, and marks what actually gets taken. |
| **Review** | opus | Re-checks a still-open position against current price + fresh news → HOLD or EXIT with a reason. Runs before hunting new trades each run, and *between* runs when the position watcher's cheap screen flags a spent move (near-target or MFE give-back). The team opens positions; the reviewer and the level-cross watcher are what close them early. |

Researcher (sonnet) and Critic (opus) run **different models on purpose** — decorrelated
errors, so the critic isn't just agreeing with a copy of itself.

---

## Model tiering

Roles are mapped to model tiers by the work they do (`config.MODEL_MAP`). Every role is
overridable via `MODEL_<ROLE>=...`.

- **haiku** — high-volume extraction: enrichment, notes, gate, news-check.
- **sonnet** — structured judgment: scout, researcher, plan, earnings reader.
- **opus** — hard reasoning and web grounding: critic, judge, head, loner, review, connections.

On a rate limit, each role **steps down its own ladder** (opus → sonnet → haiku) for a
window; the actual model used is tagged on the ledger row. If even the bottom tier is
limited, a circuit breaker opens and calls fail fast to their safe defaults rather than
storming the API.

---

## The LLM guardrail stack

Every single model call in the system passes through one function — `llm.call_role` — which
applies these layers in order:

1. **Model resolution** — `MODEL_MAP[role]` + env override + current downgrade-ladder state.
2. **Injection defense** — external text (headlines, article bodies, web results) only ever
   enters a prompt wrapped in `<data:*>` delimiters, with a standing system instruction that
   content inside those blocks is untrusted data, never instructions. Web results are tagged
   UNTRUSTED.
3. **Breaker check** — if the rate-limit breaker is open, fail fast without calling.
4. **Input-size cap** — oversized upstream data is truncated (cost + DoS/injection surface).
5. **Schema validation** — every role returns JSON validated against a strict schema
   (types, ranges, enums). One re-ask on failure, then a safe default.
6. **Universe whitelist** — any ticker a model emits must exist in the Alpaca-tradable
   universe. Invented tickers are rejected. This is the key output-security rail.
7. **Concurrency + budget** — a semaphore caps concurrent CLI subprocesses (memory), and
   tool-using (web) calls carry a hard per-call dollar ceiling and turn limit.
8. **Token telemetry** — per role/model/decision, written to the ledger.

Fail-safe doctrine: a failed call raises, and the call site drops that candidate with a
logged reason. **Never a phantom pick, never a retry storm.**

---

## The ledger and forward grading

Every evaluation — team or loner, approved or rejected — is one row in a SQLite/WAL ledger
(`~/.alphadesk/ledger.db`). The grader is **pure code, zero judgment**:

- **Entry.** A pick decided while the market is closed enters at the *open* of the next
  trading day, never at a stale prior close. A pick decided during market hours stamps its
  entry price live.
- **Outcomes.** `ret_1d` = close one trading day after entry; `ret_horizon` = close at the
  pick's declared horizon. Direction-aware (SHORT inverts the sign).
- **Benchmark.** SPY over the identical window; a short benchmarks against short-SPY, so
  alpha stays symmetric.
- **Net alpha.** `alpha_net = directional_return − benchmark − friction`, where friction is
  `2 × 15 bps` per round trip, doubled again for low-liquidity names.

The hourly grader marks the paper portfolio forward even when nothing else is running.
Open positions are re-reviewed on the next run, closed on a target/stop cross by the ~180s
position watcher, and — between runs — escalated to the opus reviewer when a cheap code
screen flags a spent move (near-target, or a give-back from the persisted MFE peak), so a
played-out move is closed before the gain decays. The forward grade (`alpha_net`) settles
at the declared horizon regardless of any early exit.

---

## Anti-survivorship

A desk that only grades the trades it took learns nothing from the ones it passed on.
AlphaDesk grades the counterfactuals too:

- **Rejected picks** are still graded — a rejection that would have beaten SPY is a recorded
  false negative.
- **Scout skips** are graded directionlessly: a skipped name that then makes a large move in
  *either* direction versus SPY (above a threshold, over a short window) counts as a
  dislocation the desk chose not to even look at.

Both feed a false-negative record back into the scout and judge prompts — sample-gated, so
it stays silent until there's enough signal to be worth weighing.

---

## Grounded self-improvement, not RL

The model is frozen. Self-improvement here is **evidence accumulation for humans to
retune**, not weight updates:

- A numeric **calibration scorecard** — the desk's own graded hit-rate and net alpha by
  edge, horizon, and confidence bucket — is injected into agent prompts as *facts to weigh,
  not lessons to obey*. It stays dormant until ~8 graded trades exist, because below that
  it's noise, and superstition is exactly the failure mode being avoided.
- The real self-correction mechanism is **pre-committed kill criteria**: drop the debate,
  drop an edge, or drop the whole team if the ledger says they don't pay. Each is
  falsifiable and removable.
- There is deliberately **no free-form "lessons learned" memory** — a persistent, model-
  writable text buffer is an injection surface and a superstition generator. The scorecard
  is fixed-size and falsifiable instead.

---

## Quick start

```bash
pip install -r requirements.txt

# Web dashboard + hourly grader (primary mode — trades run on a button click)
python -m alphadesk.main dashboard        # http://localhost:8000

# Or convene the team now, headless, on the last 8h of news
python -m alphadesk.main desk

# Rebuild the web UI after editing it (React 19 + TS + Vite → app/static/)
cd alphadesk/ui && pnpm build
```

You need a Claude Max subscription with the Claude Code CLI available on the machine (that's
what `claude-agent-sdk` drives), plus Alpaca keys for market data and the tradable universe.

---

## Commands

```bash
python -m alphadesk.main dashboard          # v2 on-demand: dashboard + hourly grader (primary)
python -m alphadesk.main desk [--hours 8]   # convene the team NOW on recent news, headless
python -m alphadesk.main world [--categories 3] [--to-desk]   # one GDELT world-news tick
python -m alphadesk.main grade              # grade all due picks once, print the count
python -m alphadesk.main status             # ledger summary + today's token usage
python -m alphadesk.main earnings           # refresh the calendar; show upcoming + recent
python -m alphadesk.main backfill [--hours 72]   # one-shot news backfill into the caches
python -m alphadesk.main run                # legacy autonomous 24/7 scheduler + dashboard
```

The primary mode is `dashboard`: trades run only when you click **Find Trades**, while the
grader keeps the paper portfolio marked forward in the background. `run` is the legacy
always-on scheduler, kept but not the v2 path.

---

## Configuration

Set via a `.env` file or the environment.

**Required / common**

```ini
ALPACA_API_KEY=...            # market data + tradable universe (paper keys are fine)
ALPACA_SECRET_KEY=...
POLYGON_API_KEY=...           # financial news (optional; GDELT world news needs no key)
ADMIN_USERNAME=admin          # dashboard Basic Auth (fail-closed if unset)
ADMIN_PASSWORD=...
ALPHADESK_DATA=~/.alphadesk   # ledger.db, universe cache, enrichment cache
```

**Tuning knobs (all optional, sensible defaults)**

```ini
SOLO_ARM_EVERY_N=0            # 0=off; set e.g. 6 to accumulate team-vs-loner comparisons
WORLD_MAX_CATEGORIES=0        # GDELT world news in Find Trades: 0=off (default); 4=full sweep every ~3 runs; 11=every run
EXPOSURE_MAX_SHOCKS=2         # how many top shocks the Connections desk web-maps per run
REPICK_COOLDOWN_HOURS=24      # don't re-debate the same name/story within this window
EXIT_NEAR_TARGET_FRAC=0.85    # exit screen: ≥ this much of entry→target captured → escalate to review
EXIT_GIVEBACK_MIN_PEAK=4.0    # watch give-back only after the favorable move peaks above this %
EXIT_GIVEBACK_FRAC=0.40       # faded ≥ this fraction of that peak → escalate (MFE-decay flag)
EXIT_REVIEW_COOLDOWN_S=1800   # min seconds between reviews of the same open position
LLM_MAX_CONCURRENCY=4         # cap on concurrent Claude CLI subprocesses (memory)
LLM_MAX_INPUT_CHARS=48000     # per-call input truncation (~12k tokens; cost/DoS cap)
LLM_TOOL_BUDGET_USD=0.50      # hard ceiling on a single web-search agent call
MAX_CONCURRENT_WORKFLOWS=4    # lower on tiny hosts (e.g. 1 on a 1GB micro VM)
MAX_RUNS_PER_DAY=50           # Find Trades runaway guard
MODEL_<ROLE>=opus|sonnet|haiku   # override any single role, e.g. MODEL_CRITIC=sonnet
DASHBOARD_HOST=127.0.0.1      # set 0.0.0.0 to expose on a VM
DASHBOARD_PORT=8000
```

---

## Repository layout

```
alphadesk/
  config.py            model map, caps, sessions, tradable universe (weekly Alpaca cache)
  llm.py               the guarded call stack — every LLM call goes through call_role
  ingest/
    news.py            Polygon poll → haiku enrichment → candidates
    world.py           GDELT world-news (11-cat taxonomy) — OFF by default in Find Trades
                       (WORLD_MAX_CATEGORIES=0); still used by the scheduler + `world` CLI
    prices.py          lazy per-symbol context — real-time Alpaca last trade (yfinance history fallback); no triggers, no sweeps
    earnings.py        Nasdaq earnings calendar + post-earnings-drift candidates (+ realized since-report move)
  desk/
    stream.py          on-demand "Find Trades" SSE flow (v2 primary path)
    workflow.py        research_run() — the batch pipeline (desk CLI, scheduler, replay)
    debate.py          deliberate() — the shared researcher→critic→judge core + ledger write
    scout.py           all attention judgment, in one prompt
    gate.py            pre-debate catalyst screen (haiku, fail-open)
    notes.py           2 parallel note subagents: market, news
    connections.py     the web-grounded spillover desk
    team.py            researcher ⇄ critic → judge, calibration block, head ranking
    plan.py            execution desk — entry/target/stop + pure-code exit physics (level_crossed, exit_signal, realized_exit)
    loner.py           single-agent control arm
    review.py          position review — HOLD/EXIT on still-open takes (per run + between-run watcher escalations)
    news_check.py      same-story vs new-catalyst check on a recently-debated name
    earnings_reader.py web-grounded read of an actual earnings report
  ledger/
    store.py           SQLite/WAL: picks, runs, funnel, skips, earnings, tokens, relationships
    grader.py          forward grading vs SPY, friction haircut — pure code
  app/
    dashboard.py       FastAPI + Basic Auth + SSE endpoint + static SPA
    scheduler.py       hourly grader loop (v2); legacy 24/7 loop (run mode)
  main.py              CLI entrypoint
  ui/                  React 19 + TS + Vite + shadcn/ui → built into app/static/
```

---

## Design principles

- **Agents own judgment; code owns facts, physics, safety, and scoring.** No hardcoded
  judgment thresholds — the LLM assesses signals from raw data; code owns arithmetic,
  tradability, injection defense, schema validation, and the universe whitelist.
- **Attention is information-driven, never price-driven.** Price *informs* a decision; it
  never *triggers* one. Decisions come from causes (news), not price-narration.
- **Forward-only evidence.** The system earns trust from its graded ledger, not its prose,
  with pre-committed kill criteria for every component — including the debate and itself.
- **Fail safe, not loud.** A failed stage drops one candidate with a logged reason. The
  system never invents a pick to fill a gap and never retries into a rate-limit storm.

---

## Status

Early and **unproven by design.** The ledger clock is running, but the sample is tiny and
shows **no edge yet** (~28 graded as of 2026-07-22, direction ≈ coin-flip, mean alpha
negative — statistically indistinguishable from zero). The calibration prior, the kill
criteria, and the alpha thesis itself stay dormant until enough honestly-priced picks
grade. A stale-price bug (fixed 2026-07-22) had inflated early paper-exit P&L and priced-in
reasoning by anchoring to yfinance's stale daily close the morning after an earnings gap;
the forward grade (`alpha_net`) was never affected, since it enters at the real
next-session open. The highest-value next step is to let the current cohort grade to a real
read before changing anything.

---

## Disclaimer

For educational and informational purposes only. **Not financial advice.** This system does
not place trades. Algorithmic trading carries significant risk of loss.
