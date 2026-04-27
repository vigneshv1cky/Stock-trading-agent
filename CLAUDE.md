# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run screener once
python run.py

# Run autonomous scheduler (every 30 mins)
python run.py --schedule

# Run with custom interval (e.g., hourly)
python run.py --schedule --every 1

# Backtest predictions (checks ones 5+ days old)
python run.py --backtest

# Show recent alerts
python run.py --alerts

# Optimize per-archetype scoring weights from backtest history
python run.py --optimize

# Start web dashboard
uvicorn web:app --reload --port 8000
```

### Testing & Validation
```bash
python test_run.py          # manual test runner
python verify_weighting.py  # validates conviction score formula
```

### Cloud Deployment (AWS)
```bash
aws sso login --profile vignesh-sso-profile
./run_aws_bot.sh            # builds Docker, pushes to ECR, updates ECS

# Tail live logs
aws logs tail /ecs/stock-screener --region us-east-1 --follow --profile vignesh-sso-profile
```

## Architecture

This is an algorithmic swing trading bot built on a **three-layer decision stack**: Screen → Predict → Execute.

### Decision Pipeline

1. **Screener** (`stock_sentiment/market/screener.py`) — Filters 200+ curated equities down to top 40:
   - Hard gates: RVOL ≥ 1.0, no earnings within 3 days
   - Two-pass adaptive thresholds: Pass 1 collects raw metrics, Pass 2 classifies archetypes against live-universe percentiles (75th/60th/30th/65th) — avoids static cutoffs being gamed by market conditions
   - Archetype OR logic: **Breakout Star** (1w ≥10% OR 1m ≥15%), **Recovery Phoenix** (3m ≤-15% AND 3d ≥4%), **Momentum King** (3m ≥7%)

2. **Predictor** (`stock_sentiment/market/stock_predictor.py`) — Generates 0–100 conviction scores:
   - Formula blend (70%): momentum, volume, technicals, sentiment sub-scores with per-archetype learned weights from `weight_optimizer.py`
   - LLM blend (30%): single Claude Haiku batch call scores all 40 stocks qualitatively via `stock_sentiment/nlp/sentiment.py`
   - Article sentiment uses Amazon Nova Micro (`amazon.nova-micro-v1:0`) for bulk scoring; recency decay halves score weight every 48h; source quality tiers (Reuters/Bloomberg 1.5×, etc.)
   - Red-flag override: Haiku `red_flag=true` hard-caps final score at 35 (forces BEARISH)
   - BULLISH threshold: ≥ regime-adjusted value (55–70) | BEARISH: ≤40

3. **Broker** (`stock_sentiment/market/broker.py`) — Executes via Alpaca API:
   - Max 10 positions, flat 9% of portfolio per position, whole shares only
   - Market orders with 3.0% trailing GTC stops; stops tighten to 1.5% at +15% gain, 0.8% at +30%
   - Smart Conviction Swapping: new pick with score >5 points above weakest holding triggers a swap
   - BEARISH downgrade → immediate liquidation; earnings ≤3 days away → pre-emptive close
   - 1-hour re-entry cooldown after stop-out (`~/.stock_screener/cooldowns.json`)
   - Paper mode is default; set `ALPACA_PAPER=false` to switch to live (uses `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_SECRET_KEY`)

**Orchestrator:** `stock_sentiment/screener_app.py` wires the pipeline; `stock_sentiment/scheduler.py` runs it every 15 min by default (0.25h), respecting Alpaca market clock.

### Market Regime (`stock_sentiment/market/market_regime.py`)

Fetches SPY and ^VIX from yfinance each cycle and classifies macro conditions:

- **HIGH_VOL** (VIX > 30): buy threshold 70, position sizing 70%
- **BEAR** (SPY < 200-SMA): buy threshold 65, sizing 85%
- **BULL** (SPY > 3% above SMA AND VIX < 20): buy threshold 55, sizing 100%
- **NEUTRAL** (everything else): buy threshold 60, sizing 100%

### Weight Optimizer (`stock_sentiment/market/weight_optimizer.py`)

Learns optimal `[momentum, volume, technical, sentiment]` weights per archetype using Nelder-Mead (falls back to random search if scipy unavailable). Requires ≥50 global outcomes, ≥20 per archetype. Weights persisted to `~/.stock_screener/weights.json`.

### Real-Time News Monitor (`stock_sentiment/market/news_monitor.py`)

Runs alongside the 30-min scheduler in a daemon thread. Two-stage pipeline per article:

1. Nova Micro quick score — filters noise (|score| must exceed 0.65)
2. Claude Haiku deep call — detects red flags and assigns conviction

Actions: red flag on held position → immediate close; strong catalyst on watchlist candidate → entry attempt. 15-min per-symbol cooldown.

Provider abstraction (`stock_sentiment/market/news_providers.py`):

- **RSS** (default): Google News RSS, no API key required
- **Polygon** (`PolygonNewsProvider`): polls REST API every 60s, free tier compatible
- **Alpaca** (`AlpacaNewsProvider`): WebSocket stream, requires paid subscription
- Provider selection and Polygon API key stored in `~/.stock_screener/settings.json` (managed by `stock_sentiment/config.py`). Dashboard Settings tab allows changing provider at runtime — takes effect on next cycle.

### Storage (Dual-Backend Pattern)

`stock_sentiment/history.py` abstracts local vs. cloud persistence:
- **Local dev**: SQLite at `~/.stock_screener/local_history.db`
- **Production** (`ENV=PROD`): DynamoDB tables `PROD_StockScreenerRuns`, `PROD_StockScreenerPredictions`, `PROD_StockScreenerStatus`

### Web Dashboard

`web.py` (FastAPI) + `templates/index.html` + `static/app.js` — four tabs: Performance, Trade History, Screener, Settings. Auth: `ADMIN_USERNAME` / `ADMIN_PASSWORD`. Settings tab exposes news provider selection and Polygon API key (stored masked, never returned by API).

### Cloud Infrastructure

ECS Fargate (1 vCPU, 4GB) behind an ALB. NLP runs entirely on AWS Bedrock — no large model files in the Docker image. Deployment fully scripted in `deploy/deploy.sh`.

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

- **All NLP runs on AWS Bedrock**: Nova Micro for bulk article scoring, Haiku for qualitative conviction and news red-flag detection. No local model files — Bedrock credentials via `AWS_PROFILE` or instance role.
- **Archetype matters for scoring**: `MOMENTUM`, `BREAKOUT`, and `RECOVERY` archetypes use different RSI/momentum weightings — always check archetype context when modifying `stock_predictor.py`. Weights are learned per-archetype and stored in `~/.stock_screener/weights.json`.
- **Price data is cached 600s** in `price_fetcher.py` to avoid yfinance rate limits.
- **Alerts detect state changes** by diffing consecutive runs in `alerts.py` — they require at least two historical runs to be meaningful.
- **Docker target is `linux/amd64`** — specify platform when building locally on Apple Silicon.
- **Runtime state files** live in `~/.stock_screener/`: `cooldowns.json` (stop-out re-entry blocks), `held_cache.json` (previous cycle holdings for stop-out detection), `weights.json` (learned scoring weights), `settings.json` (news provider config), `last_execution.json` (most recent trade execution log).
