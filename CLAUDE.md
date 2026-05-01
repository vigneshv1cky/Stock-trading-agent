# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run real-time multi-agent trading system (stocks + crypto simultaneously)
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
    WatcherAgent          ← detects RVOL ≥ 1.2× AND |price change| ≥ 1.5% (stocks)
    CryptoWatcherAgent    ← detects RVOL ≥ 1.0× AND |price change| ≥ 2.0% (crypto, 24/7)
    ScannerAgent          ← batch scan every 15 min (proactive, all 487 stocks)
         │ market.signal
    ScreenerAgent         ← qualification + regime gates
    ResearchAgent         ← RSI, BB, MACD, ATR (runs in parallel with Screener)
         │ symbol.screened
    NewsAgent             ← Polygon.io news + Haiku sentiment scoring
         │ symbol.analysed
    PredictorAgent  ←──── AgentMemory (learned lessons injected into prompt)
    (Haiku)
         │ symbol.predicted
    CriticAgent     ←──── AgentMemory (adversarial — find reasons the trade FAILS)
    (Haiku)
         │ symbol.reviewed
    RiskAgent             ← LLM decision: APPROVE/BLOCK; VIX, position caps, sector, cooldown
         │ trade.approved
    ExecutorAgent         ← Alpaca orders + trailing stops + EOD close at 3:30 PM ET
         │ trade.closed
    LearningAgent         ← reflects on outcomes every 10 trades or 4:05 PM ET daily
         │ memory.updated
    AgentMemory           ← ~/.stock_screener/agent_memory.json

    MonitorAgent          ← polls positions every 30s → position.alert → RiskAgent
    PortfolioAgent        ← tracks sector concentration
    MacroAgent            ← VIX + SPY + QQQ + breadth every 5 min
    PromptTunerAgent      ← loads optimized prompts from disk
```

All agents run as concurrent `asyncio` tasks sharing one `EventBus` and one `AgentMemory`. Each agent has a `safe_run()` crash-restart loop with 5s backoff.

### Key Agent Behaviours

**WatcherAgent** (`agents/watcher.py`)
- Pre-loads 20-day avg daily volumes and today's intraday open/cumulative volume via Alpaca at startup (yfinance fallback)
- Streams Alpaca `StockDataStream` 1-min bars; computes RVOL = `cumulative_vol / (avg_daily × minutes_elapsed/390)`
- Signal gate: RVOL ≥ 1.2 AND |intraday change| ≥ 1.5%; 5-min per-symbol debounce
- Falls back to yfinance 90s polling when Alpaca WebSocket unavailable

**CryptoWatcherAgent** (`agents/crypto_watcher.py`)
- Streams Alpaca `CryptoDataStream` 1-min bars for 18 coins; runs 24/7
- Signal gate: RVOL ≥ 1.0 AND |intraday change| ≥ 2.0%; 30-min cooldown
- Session resets at UTC midnight

**ScreenerAgent** (`agents/screener.py`)
- Fetches 3mo price history via yfinance (stocks) or Alpaca (crypto)
- Stock checks: ≥20 days history, price ≥ $5, avg volume ≥ 100k
- Time-of-day gate: drops signals in first 15 min and last 15 min before 4 PM (REEVAL signals skip this)
- Regime-adjusted RVOL and price-move minimums (RISK_OFF ×1.3, PANIC ×1.6)
- Computes relative strength vs sector ETF (from MacroAgent)

**ResearchAgent** (`agents/research.py`)
- Subscribes to `market.signal` directly — runs in parallel with ScreenerAgent
- Fetches 60-day history via yfinance (stocks) or Alpaca (crypto)
- Computes RSI-14, Bollinger %B, MACD histogram, ATR%, volume trend
- Calls Haiku for 2-sentence technical synthesis
- Results cached 120s; accessible via `ResearchAgent.get_cached(sym)`

**PredictorAgent** (`agents/predictor.py`)
- Single Haiku call with a 4-step structured prompt: technical thesis → catalyst quality → risk factors → final score
- Returns score (−100 to 100), confidence (0–100), red_flag_severity (NONE/MINOR/MODERATE/FATAL)
- Post-LLM mechanical adjustments: MODERATE cap → 0, FATAL cap → −44; confidence < 35 → skip signal; PANIC regime → suppress BULLISH
- RSI comes from `ResearchAgent.get_cached(sym)` — no separate PriceFetcher call
- Injects learned lessons from `AgentMemory` into every LLM call

**CriticAgent** (`agents/critic.py`)
- Two-turn adversarial Haiku debate: Turn 1 raises top 3 concerns; Turn 2 returns `adjusted_score` (LLM owns magnitude)
- One mechanical safety net: if predictor score is NEUTRAL (−19 to +19), critic cannot make it worse
- CLOSE actions bypass the critic entirely — exit speed matters

**RiskAgent** (`agents/risk.py`)
- All trade decisions delegated to Haiku: time of day, duplicate prevention, position caps (soft target ≤8 total, ≤2 crypto), displacement choice, buying power, shortability, VIX/regime, sector concentration, drawdown
- LLM receives full context: minutes since open/close, displacement candidates with scores, buying power vs slot, shortable fact, VIX, regime, day P&L, sector breakdown
- LLM returns `{"decision": "APPROVE"|"BLOCK", "displace": "SYMBOL"|null, "reasoning": "..."}`
- Two mechanical gates remain: BEARISH on existing long → immediate CLOSE; cooldown block after stop-out
- LLM failure defaults to **BLOCK** (safe default)
- Handles `EARNINGS_REPORTED` alerts: fetches Polygon news, calls Haiku to HOLD or CLOSE

**ExecutorAgent** (`agents/executor.py`)
- Three concurrent loops: trade processing, stop audit (every 5 min), EOD close (3:30 PM ET)
- Startup audit: cancels prior-session open orders, backfills `held_cache` for live positions
- Trailing stop widths: 0.5% (big winner) → 1.5% (moderate winner) → 3.0% (default)
- Stocks: whole shares (`int(slot / price)`); Crypto: notional (`notional=slot`, GTC)
- EOD close uses `abs(float(pos.qty))` — handles fractional crypto positions

**LearningAgent** (`agents/learning.py`)
- Rolling buffer of last 100 closed trade outcomes
- Haiku reflection every 10 trades or daily at 4:05 PM ET; writes 3–5 lessons to `AgentMemory`

**MonitorAgent** (`agents/monitor.py`)
- Polls positions every 30s
- `REEVAL` alert: position not re-evaluated in 30+ min → re-triggers screener pipeline
- `EARNINGS_REPORTED` alert: actual EPS in last 48h → RiskAgent decides HOLD or CLOSE
- Tracks `_analyzed_earnings` to avoid duplicate alert firing

### Broker (`agents/broker.py`)
- `PaperBroker` wraps Alpaca TradingClient — paper or live mode from env vars
- `_place_market_buy()`: stocks = whole shares + DAY; crypto = notional + GTC
- `_place_market_short()`: blocked for crypto (`"/" in symbol` check)
- `_get_live_price()`: dispatches to stock or crypto latest trade API
- Stop placement retries up to 3× with wash-trade error handling

### Storage (Dual-Backend Pattern)

`stock_sentiment/history.py` abstracts local vs. cloud persistence:
- **Local dev**: SQLite at `~/.stock_screener/local_history.db`
- **Production** (`ENV=PROD`): DynamoDB tables `PROD_StockScreenerRuns`, `PROD_StockScreenerPredictions`, `PROD_StockScreenerStatus`

### Web Dashboard

`web.py` (FastAPI) + `templates/index.html` + `static/app.js`. The agent system starts automatically as a daemon thread when `web.py` starts. Auth: `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

### Cloud Infrastructure

ECS Fargate (1 vCPU, 4GB) behind an ALB. NLP runs entirely on AWS Bedrock Haiku — no large model files in the Docker image. Docker target is `linux/amd64`. Deployment scripted in `deploy/deploy.sh`.

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

# Optional — Polygon.io news
POLYGON_API_KEY=...

# Web dashboard auth
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# Production mode (switches SQLite → DynamoDB)
ENV=PROD
```

## Key Design Notes

- **All NLP runs on AWS Bedrock Haiku**: sentiment scoring, Predictor conviction, Critic adversarial review, RiskAgent approval, LearningAgent reflection. No local model files.
- **LLM owns all judgment, code owns hard facts**: score magnitude, risk approval, sector/regime decisions are LLM. Position caps arithmetic, shortability, and cooldowns are either LLM context or the two remaining mechanical gates.
- **No archetypes**: ScreenerAgent does not classify signals — LLM assesses signal type (breakout, dip-buy, momentum, volume event) from raw quantitative data directly.
- **No formula blending**: PredictorAgent is pure LLM — no weighted formula score. RSI from `ResearchAgent.get_cached()`, no separate PriceFetcher call.
- **Crypto is LONG only**: blocked at 3 levels — risk prompt rule, `_place_market_short()` code guard, Alpaca asset shortable=false.
- **Crypto uses notional orders**: fractional qty via `notional=slot` + `TimeInForce.GTC`. Stop audit allows fractional qty for crypto positions.
- **AgentMemory lessons** injected into every Predictor and Critic LLM call — self-improving over time. Persisted at `~/.stock_screener/agent_memory.json`.
- **RiskAgent LLM failure defaults to BLOCK**: if Bedrock is unavailable, trades are blocked.
- **Runtime state files** in `~/.stock_screener/`: `cooldowns.json`, `held_cache.json`, `last_execution.json`, `agent_memory.json`.
- **yfinance is used for**: VIX/macro data (`^VIX` not on Alpaca), earnings dates, stock screener/research history, WatcherAgent fallback. Alpaca is used for everything live/real-time.

## File Structure

```
stock_sentiment/
  agents/
    base.py           ← BaseAgent: safe_run, get_bedrock, logging
    event_bus.py      ← asyncio Queue-based pub/sub
    memory.py         ← AgentMemory: lesson storage + retrieval
    orchestrator.py   ← wires all agents, defines SCREEN_UNIVERSE + CRYPTO_UNIVERSE
    broker.py         ← PaperBroker: Alpaca order placement
    watcher.py        ← stock signal detection (Alpaca WebSocket)
    crypto_watcher.py ← crypto signal detection (Alpaca Crypto WebSocket, 24/7)
    scanner.py        ← proactive batch scanner (every 15 min)
    screener.py       ← signal qualification + regime gates
    research.py       ← technical indicators (RSI, BB, MACD, ATR)
    news.py           ← Polygon.io news + Haiku sentiment (Article, ScoredArticle inline)
    predictor.py      ← Haiku conviction scoring
    critic.py         ← adversarial Haiku review
    portfolio.py      ← sector tracking + get_sector()
    risk.py           ← LLM trade approval + earnings handling
    executor.py       ← order execution + stop management
    learning.py       ← Haiku reflection → AgentMemory
    monitor.py        ← position health polling (earnings + re-eval)
    macro.py          ← VIX + regime + sector ETF performance
    prompt_tuner.py   ← optimized prompt loading
  history.py          ← SQLite / DynamoDB dual backend
  config.py           ← settings
```
