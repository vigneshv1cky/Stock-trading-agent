# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run real-time multi-agent trading system
python run_agents.py

# Dry-run (full pipeline, no order execution — safe for testing)
python run_agents.py --dry-run

# Start web dashboard
uvicorn web:app --reload --port 8000
```

### Cloud Deployment (AWS)
```bash
aws sso login --profile vignesh-sso-profile
./run_aws_bot.sh            # builds Docker, pushes to ECR, updates ECS

# Tail live logs
aws logs tail /ecs/stock-screener --region us-east-1 --follow --profile vignesh-sso-profile
```

## Architecture

This is a real-time multi-agent algorithmic trading system. Agents communicate via an in-process `asyncio.Queue`-based `EventBus` — no Redis or external broker needed.

### Agent Pipeline

```
Alpaca WebSocket (1-min bars)
         │
    WatcherAgent          ← detects RVOL ≥ 1.5× AND |price change| ≥ 2%
         │ market.signal
    ScreenerAgent         ← single-symbol qualification + archetype classification
         │ symbol.screened
    NewsAgent             ← Google RSS fetch + Bedrock sentiment scoring
         │ symbol.analysed
    PredictorAgent  ←──── AgentMemory (learned lessons injected into prompt)
    (formula + Haiku)
         │ symbol.predicted
    CriticAgent     ←──── AgentMemory (adversarial — find reasons the trade FAILS)
    (Haiku)
         │ symbol.reviewed
    RiskAgent             ← VIX gate, slot check, cooldown check
         │ trade.approved
    ExecutorAgent         ← Alpaca orders + hard stops + EOD close at 3:45 PM ET
         │ trade.closed
    LearningAgent         ← reflects on outcomes every 10 trades or 4:05 PM ET daily
         │ memory.updated
    AgentMemory           ← ~/.stock_screener/agent_memory.json

    MonitorAgent          ← polls positions every 30s → position.alert → RiskAgent
```

All agents run as concurrent `asyncio` tasks sharing one `EventBus` and one `AgentMemory`. Each agent has a `safe_run()` crash-restart loop with 5s backoff.

### Key Agent Behaviours

**WatcherAgent** (`agents/watcher.py`)
- Pre-loads 20-day avg daily volumes via yfinance at startup
- Streams Alpaca `StockDataStream` 1-min bars; computes RVOL = `cumulative_vol / (avg_daily × minutes_elapsed/390)`
- Signal gate: RVOL ≥ 1.5 AND |intraday change| ≥ 2%; 5-min per-symbol debounce
- Falls back to yfinance 90s polling when Alpaca WebSocket unavailable

**ScreenerAgent** (`agents/screener.py`)
- Fetches 3mo price history via `PriceFetcher` (600s cache)
- Checks: ≥20 days history, no earnings within 3 days
- Archetype classification with **static** thresholds (adaptive percentiles don't work for 1 symbol):
  - FRESH_BREAKOUT: |change_today| ≥ 3% AND rvol ≥ 2.0
  - BREAKOUT: change_1w ≥ 10% OR change_1m ≥ 15%
  - RECOVERY: drawdown ≤ −15% AND bounce ≥ 4% AND rvol > 1.1
  - MOMENTUM: change_3m ≥ 7%

**PredictorAgent** (`agents/predictor.py`)
- Reuses `StockPredictor._compute_sub_scores()` and single-stock Haiku call
- 50/50 formula+LLM blend; red-flag cap at 35; BULLISH ≥ 60, BEARISH ≤ 40
- Injects learned lessons from `AgentMemory` into every LLM call

**CriticAgent** (`agents/critic.py`)
- Second adversarial Haiku call designed to find trade failure reasons
- CONFIRM → score unchanged; DOWNGRADE → −15 pts; REJECT → cap at 45 (forces NEUTRAL)
- Does NOT block exits — CLOSE actions bypass the critic

**RiskAgent** (`agents/risk.py`)
- VIX regime thresholds: calm(55) → normal(60) → volatile(70) → panic(85)
- Max 10 open positions; 1-hour re-entry cooldown after stop-out

**ExecutorAgent** (`agents/executor.py`)
- Delegates to `PaperBroker` methods; stop audit every 5 min
- EOD close at 3:45 PM ET for all DAY trades and short positions

**LearningAgent** (`agents/learning.py`)
- Rolling buffer of last 100 closed trade outcomes
- Haiku reflection every 10 trades or daily at 4:05 PM ET
- Writes lessons to `AgentMemory`; triggers `WeightOptimizer.optimize()` when ≥50 outcomes

### Weight Optimizer (`stock_sentiment/market/weight_optimizer.py`)

Learns optimal `[momentum, volume, technical, sentiment]` weights per archetype using Nelder-Mead (falls back to random search if scipy unavailable). Requires ≥50 global outcomes, ≥20 per archetype. Weights persisted to `~/.stock_screener/weights.json`.

### Storage (Dual-Backend Pattern)

`stock_sentiment/history.py` abstracts local vs. cloud persistence:
- **Local dev**: SQLite at `~/.stock_screener/local_history.db`
- **Production** (`ENV=PROD`): DynamoDB tables `PROD_StockScreenerRuns`, `PROD_StockScreenerPredictions`, `PROD_StockScreenerStatus`

### Web Dashboard

`web.py` (FastAPI) + `templates/index.html` + `static/app.js`. The agent system starts automatically as a daemon thread when `web.py` starts. Tabs: Performance, Trade History, Screener, Settings. Auth: `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

### Cloud Infrastructure

ECS Fargate (1 vCPU, 4GB) behind an ALB. NLP runs entirely on AWS Bedrock — no large model files in the Docker image. Docker target is `linux/amd64`. Deployment fully scripted in `deploy/deploy.sh`.

## Environment Variables

```ini
# Required (paper trading)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...

# Live trading (set ALPACA_PAPER=false to activate)
ALPACA_PAPER=false
ALPACA_LIVE_API_KEY=...
ALPACA_LIVE_SECRET_KEY=...

# AWS (local dev — SSO profile)
AWS_PROFILE=vignesh-sso-profile
AWS_REGION=us-east-1

# Web dashboard auth
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# Production mode (switches SQLite → DynamoDB)
ENV=PROD

# Optional cloud features
S3_BUCKET=...
SES_FROM_EMAIL=...
SES_TO_EMAIL=...
```

## Key Design Notes

- **All NLP runs on AWS Bedrock**: Nova Micro (primary) → Nova Lite → Haiku fallback chain for article sentiment scoring; Haiku for Predictor conviction scores, Critic adversarial review, and LearningAgent reflection. No local model files — Bedrock credentials via `AWS_PROFILE` or instance role.
- **Archetype matters for scoring**: `MOMENTUM`, `BREAKOUT`, and `RECOVERY` archetypes use different RSI/momentum weightings — always check archetype context when modifying `stock_predictor.py`. Weights are learned per-archetype and stored in `~/.stock_screener/weights.json`.
- **Static archetype thresholds in ScreenerAgent**: unlike the batch screener which uses live-universe percentiles, the agent screener uses fixed thresholds because adaptive percentiles require a full universe of stocks to be meaningful.
- **Price data is cached 600s** in `price_fetcher.py` to avoid yfinance rate limits.
- **AgentMemory lessons** are injected into every Predictor and Critic LLM call, making the system self-improving over time. Lessons persisted at `~/.stock_screener/agent_memory.json`.
- **Runtime state files** live in `~/.stock_screener/`: `cooldowns.json` (stop-out re-entry blocks), `held_cache.json` (current holdings with direction/type), `weights.json` (learned scoring weights), `last_execution.json` (most recent trade execution log).
