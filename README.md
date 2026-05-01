# Stock Trading Agent

A real-time multi-agent algorithmic trading system built in Python. Fifteen specialized agents communicate via an async event bus, collaborating to screen, analyze, critique, risk-gate, and execute trades autonomously. Trades both **stocks** (market hours) and **crypto** (24/7) simultaneously. All NLP runs on AWS Bedrock Haiku — no local model files.

---

## How It Works — Full Flow

```
Alpaca WebSocket (1-min bars)          Alpaca Crypto WebSocket (24/7)
         │                                        │
    WatcherAgent (487 stocks)           CryptoWatcherAgent (18 coins)
    ScannerAgent (every 15 min)                   │
         │                                        │
         └──────────── market.signal ─────────────┘
                               │
                    ┌──────────┴──────────┐
               ScreenerAgent         ResearchAgent  ← runs in parallel
               (qualifies symbol)    (RSI, BB, MACD, ATR)
                    │ symbol.screened
               NewsAgent             ← Polygon.io news + Haiku sentiment
                    │ symbol.analysed
               PredictorAgent  ←──── AgentMemory (learned lessons)
                    │ symbol.predicted
               CriticAgent     ←──── AgentMemory (adversarial review)
                    │ symbol.reviewed
               RiskAgent             ← LLM decision: APPROVE / BLOCK
                    │ trade.approved
               ExecutorAgent         ← Alpaca orders, stops, EOD close
                    │ trade.closed
               LearningAgent         ← reflects on outcomes → AgentMemory

    PortfolioAgent    ← tracks sector concentration
    MonitorAgent      ← polls positions every 30s → earnings + re-eval alerts
    MacroAgent        ← VIX + SPY + QQQ + breadth every 5 min
    PromptTunerAgent  ← loads optimized prompts from disk
```

All agents run as concurrent `asyncio` tasks. Each has a crash-restart loop with 5s backoff — a single failure never stops the system.

---

## Quick Start

```bash
pip install -r requirements.txt

# Run full system (stocks + crypto simultaneously)
python run_agents.py

# Dry-run — full pipeline, no orders placed
python run_agents.py --dry-run

# Web dashboard (separate terminal)
uvicorn web:app --reload --port 8000
```

---

## Agent Breakdown

### MacroAgent
Fetches VIX, SPY, QQQ, sector ETFs, and market breadth every 5 minutes via yfinance. Publishes market regime (`BULL` / `NEUTRAL` / `RISK_OFF` / `PANIC`) used by every downstream agent.

### WatcherAgent
Streams live 1-minute bars from Alpaca WebSocket for 487 stocks. Fires `market.signal` when:
- **RVOL ≥ 1.2×** (relative volume vs. 20-day average, adjusted for time of day)
- **|intraday price change| ≥ 1.5%**
- 5-minute per-symbol debounce

Falls back to yfinance 90s polling if Alpaca WebSocket is unavailable.

### CryptoWatcherAgent
Streams live 1-minute bars from Alpaca Crypto WebSocket for 18 coins (BTC, ETH, SOL, DOGE, AVAX, LINK, LTC, XRP, BCH, UNI, AAVE, DOT, MATIC, MKR, CRV, GRT, BAT, SHIB). Runs 24/7. Thresholds: RVOL ≥ 1.0, price move ≥ 2.0%, 30-minute cooldown.

### ScannerAgent
Proactive batch scanner. Every 15 minutes, downloads all 487 stocks via Alpaca, finds top movers by RVOL and price change, fires `market.signal` for each. Catches moves the reactive WatcherAgent may have missed.

### ScreenerAgent
Receives every `market.signal`. Applies sequential filters:
- **Time gate** (stocks only): blocks first 15 min and last 15 min before 4 PM
- **Quality**: price ≥ $5, avg daily volume ≥ 100k shares
- **Regime-adjusted thresholds**: RVOL and price move minimums tighten in RISK_OFF (×1.3) and PANIC (×1.6)
- **REEVAL signals** skip entry gates — position already held, just re-checking

Fetches 3mo history (yfinance for stocks, Alpaca for crypto) for momentum metrics. Publishes `symbol.screened` on pass.

### ResearchAgent
Runs in parallel with ScreenerAgent — both subscribe to `market.signal`. Fetches 60-day history and computes: RSI-14, Bollinger %B, MACD histogram, ATR%, volume trend. Calls Haiku for a 2-sentence technical synthesis. Results cached 120s per symbol. PredictorAgent reads the cache directly without a separate event.

### NewsAgent
Fetches last 1 hour of news from Polygon.io. Sends all headlines to Haiku in one batch call, gets back sentiment scores (−1.0 to +1.0) per headline. Publishes `symbol.analysed` with scored articles.

### PredictorAgent
Calls Haiku with a structured 4-step prompt:
1. Technical thesis (RVOL, RSI, BB, MACD)
2. Catalyst quality (news headlines and sentiment)
3. Risk factors (earnings proximity, macro regime)
4. Final score (−100 to +100)

Injects learned lessons from AgentMemory into every call. Applies mechanical caps: `MODERATE` severity → cap score at 0, `FATAL` → cap at −44. Drops signal if confidence < 35. Suppresses BULLISH signals in PANIC regime.

### CriticAgent
Adversarial two-turn Haiku debate:
- Turn 1: "What are the top 3 reasons this trade fails?"
- Turn 2: Returns `adjusted_score` — LLM owns the magnitude

One safety net: if predictor score is neutral (−19 to +19), critic cannot make it more negative. CLOSE actions bypass the Critic entirely — exit speed matters.

### PortfolioAgent
Tracks sector classification of all open positions. Seeds from `held_cache.json` on startup. RiskAgent uses this for sector concentration decisions.

### RiskAgent
The final gate before any order. All trade decisions delegated to Haiku. LLM receives full context:
- Minutes since market open/close
- All current positions with scores and sectors
- Buying power vs slot size
- Whether asset is shortable
- VIX, regime, day P&L
- Displacement candidates (if at position cap)

LLM returns `{"decision": "APPROVE/BLOCK", "displace": "SYMBOL/null", "reasoning": "..."}`.

Two mechanical gates (no LLM):
- BEARISH signal on an existing long → immediate CLOSE
- Cooldown block after a stop-out

LLM failure defaults to **BLOCK** — never trade on unavailable judgment.

Also handles `position.alert` from MonitorAgent for earnings analysis.

### ExecutorAgent
Places Alpaca orders and manages stops. Three concurrent loops:

**Trade loop** — market orders via PaperBroker:
- Stocks: whole shares only (`int(slot / price)`)
- Crypto: notional ordering (`notional=slot`) — fractional, GTC

**Stop audit** (every 5 min) — adaptive trailing stops:
| Unrealized P&L | Trail |
|---|---|
| Big winner locked in | 0.5% |
| Moderate winner | 1.5% |
| Default | 3.0% |

**EOD close** — 3:30 PM ET, closes all stock positions. Crypto stays open.

On startup: cancels prior-session open orders, backfills `held_cache` for live positions.

### LearningAgent
Rolling buffer of last 100 closed trade outcomes. Every 10 trades or daily at 4:05 PM ET, calls Haiku to reflect on patterns. Writes 3–5 lessons to AgentMemory. These lessons are injected into every future Predictor and Critic call — the system improves over time.

### MonitorAgent
Polls all open positions every 30 seconds. Fires two alert types:
- `REEVAL` — position not re-evaluated in 30+ min → re-triggers full pipeline
- `EARNINGS_REPORTED` — actual EPS landed in last 48h → RiskAgent calls Haiku to HOLD or CLOSE

### PromptTunerAgent
Loads optimized system prompts from disk if available, otherwise uses hardcoded defaults. Allows prompt experimentation without code changes.

---

## Position Sizing

Each position = **7–12% of portfolio value** (scaled by news urgency), minimum $50:
- High-urgency news (strong sentiment × many articles): up to 12%
- No news: flat 9%
- Stocks: whole shares (`int(slot / price)`)
- Crypto: notional/fractional (`notional=slot`)

---

## Self-Improvement Loop

```
Trade closed → LearningAgent → Haiku reflection → AgentMemory
                                                       ↓
                              PredictorAgent ← lessons injected into every LLM call
                              CriticAgent   ←
```

Lessons are accumulated across sessions and persist in `~/.stock_screener/agent_memory.json`.

---

## Crypto vs Stocks

| | Stocks | Crypto |
|---|---|---|
| Market hours | 9:30 AM – 4:00 PM ET | 24/7 |
| Data stream | Alpaca IEX WebSocket | Alpaca Crypto WebSocket |
| Price move threshold | 1.5% | 2.0% |
| RVOL threshold | 1.2× | 1.0× |
| Signal cooldown | 5 min | 30 min |
| Order type | Whole shares | Notional (fractional) |
| Short selling | Yes (if shortable) | Never |
| EOD close | 3:30 PM ET | No (stays open) |
| Max positions | 8 total, soft cap | 2 crypto max |

---

## Environment Variables

```ini
# Required (paper trading)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...

# Live trading (set ALPACA_PAPER=false to activate)
ALPACA_PAPER=false
ALPACA_LIVE_API_KEY=...
ALPACA_LIVE_SECRET_KEY=...

# AWS (for Bedrock NLP)
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

---

## Runtime State Files

All stored in `~/.stock_screener/`:

| File | Purpose |
|---|---|
| `held_cache.json` | Current positions — synced to Alpaca on every startup |
| `cooldowns.json` | Per-symbol re-entry blocks after stop-outs |
| `agent_memory.json` | Lessons from LearningAgent reflections |
| `last_execution.json` | Most recent trade execution log |
| `local_history.db` | SQLite trade history (local dev) |

---

## Cloud Deployment (AWS)

```bash
aws sso login --profile vignesh-sso-profile
./run_aws_bot.sh   # builds Docker, pushes to ECR, updates ECS

aws logs tail /ecs/stock-screener --region us-east-1 --follow --profile vignesh-sso-profile
```

Runs on ECS Fargate (1 vCPU, 4 GB). All NLP on AWS Bedrock Haiku — no large model files in the Docker image.

---

## Disclaimer

For educational and informational purposes only. Not financial advice. Algorithmic trading carries significant risk of loss. Always validate with paper trading before using real capital.
