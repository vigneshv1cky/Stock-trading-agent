# Stock Trading Agent

A real-time multi-agent algorithmic trading system built in Python. Fourteen specialized agents communicate via an async event bus, collaborating to screen, analyze, critique, risk-gate, and execute trades autonomously. Supports stocks and crypto. All NLP runs on AWS Bedrock — no local model files.

---

## How It Works — Full Flow

```
Alpaca WebSocket (1-min bars)          Alpaca Crypto API (60s poll)
         │                                        │
    WatcherAgent                        CryptoWatcherAgent
    ScannerAgent (every 15 min)                   │
         │                                        │
         └──────────── market.signal ─────────────┘
                               │
                         ScreenerAgent       ← qualifies symbol, assigns archetype
                               │ symbol.screened
                         ResearchAgent       ← RSI, Bollinger Bands, put/call ratio
                               │ symbol.researched
                         NewsAgent           ← Polygon.io news + Bedrock sentiment
                               │ symbol.analysed
                         PredictorAgent ←── AgentMemory (learned lessons)
                               │ symbol.predicted
                         CriticAgent    ←── AgentMemory (adversarial review)
                               │ symbol.reviewed
                         RiskAgent           ← VIX gate, caps, sector, cooldown
                               │ trade.approved
                         ExecutorAgent       ← Alpaca orders, stops, EOD close
                               │ trade.closed
                         LearningAgent       ← reflects on outcomes → AgentMemory

    PortfolioAgent    ← tracks sector concentration
    MonitorAgent      ← polls positions every 30s → position.alert → RiskAgent
    MacroAgent        ← VIX + SPY + QQQ + breadth every 5 min
```

All agents run as concurrent `asyncio` tasks. Each has a crash-restart loop with 5s backoff so a single failure never stops the system.

---

## Trading Modes

```bash
python run_agents.py                  # stocks only (default)
python run_agents.py --mode crypto    # crypto only
python run_agents.py --mode both      # stocks + crypto simultaneously
python run_agents.py --dry-run        # full pipeline, no orders placed
```

On startup, `ExecutorAgent` closes any positions that belong to a different mode (e.g. switching from `both` to `stocks` closes crypto positions).

---

## Agent Breakdown

### WatcherAgent
Streams 1-minute bars from Alpaca WebSocket for all 489 watched symbols. Fires `market.signal` when:
- **RVOL ≥ 1.5×** (relative volume vs. 20-day average, adjusted for time of day)
- **|intraday price change| ≥ 2%**
- 5-minute per-symbol debounce

Falls back to yfinance polling if the WebSocket is unavailable.

### CryptoWatcherAgent
Polls Alpaca's crypto API every 60 seconds for BTC/USD, ETH/USD, SOL/USD, AVAX/USD, LINK/USD, DOGE/USD, LTC/USD. Same RVOL + price-change gate as WatcherAgent. Runs 24/7 (no market-hours gate).

### ScannerAgent
Independently scans all 489 symbols every 15 minutes using yfinance snapshots. Catches moves the Watcher may have missed between bar intervals.

### ScreenerAgent
Receives signals from Watcher, Scanner, and CryptoWatcher. For stocks, fetches 3-month price history and assigns one of four archetypes:

| Archetype | Criteria |
|---|---|
| **FRESH_BREAKOUT** | \|change today\| ≥ 3% AND RVOL ≥ 2.0 |
| **BREAKOUT** | 1-week return ≥ 10% OR 1-month return ≥ 15% |
| **RECOVERY** | Drawdown ≤ −15% AND bounce ≥ 4% AND RVOL > 1.1 |
| **MOMENTUM** | 3-month return ≥ 7% |

Crypto signals skip the archetype gates and pass through directly with `asset_class=crypto`.

### NewsAgent
Fetches recent articles from **Polygon.io** (primary) with Google News RSS as fallback. Scores sentiment via a Bedrock Nova Micro → Nova Lite → Haiku fallback chain. Applies recency decay (weight halves every 48h) and source quality tiers (Reuters/Bloomberg weighted 1.5×).

### ResearchAgent
Fetches technical indicators: RSI, Bollinger Band position (0–1), and put/call ratio. These feed directly into the Predictor's formula score.

### PredictorAgent
Generates a conviction score (0–100) using a **50/50 formula + LLM blend**.

**Formula score (50%):**

```
formula_score = momentum × w[0] + volume × w[1] + technical × w[2] + sentiment × w[3]
```

Weights are **per-archetype and learned** from trade outcomes via the Weight Optimizer.

**LLM score (50%):** Single Claude Haiku call with learned lessons injected. Returns a qualitative conviction score plus a `red_flag` boolean. If `red_flag=true`, score is hard-capped at 35.

**Output thresholds:**
- **BULLISH** → score ≥ VIX-adjusted threshold (see RiskAgent)
- **BEARISH** → score ≤ 40
- **NEUTRAL** → between thresholds

### CriticAgent
A second, adversarial Claude Haiku call that actively looks for reasons the trade will **fail**. Two-turn conversation: turn 1 raises concerns, turn 2 delivers a verdict.

| Verdict | Effect |
|---|---|
| **UPGRADE** | Predictor too conservative — score +8 pts (max 85) |
| **CONFIRM** | Thesis solid — score unchanged |
| **CAUTION** | Real but non-fatal concerns — score −8 pts |
| **DOWNGRADE** | Notable risks — Critic sets exact adjusted score |
| **REJECT** | Trade-blocking concern — score capped at 32 |

CLOSE actions bypass the Critic entirely — exits are never second-guessed.

### RiskAgent
The final gate before any order is placed.

- **Market hours gate** — no new entries in first 15 min (9:30–9:45 ET) or after 3:00 PM ET
- **Drawdown circuit breaker** — halt new longs if day P&L < −2%
- **Position cap** — max 10 open positions
- **Short cap** — max 8 short positions (stocks only; crypto never shorted)
- **Sector concentration** — hard block if one sector > 50% of portfolio
- **Earnings soft penalty** — +5 pts required on threshold if earnings 4–7 days away
- **Same-sector penalty** — +5 pts required if already holding in same sector
- **Macro overlay** — RISK_OFF: threshold +5; PANIC: threshold +10
- **Cooldown** — 1-hour re-entry block after a stop-out
- **Crypto** — lower buy threshold (≤ 50); no short selling

**VIX-adjusted buy thresholds:**

| VIX | Threshold |
|---|---|
| < 15 (Calm) | 55 |
| 15–22 (Normal) | 60 |
| 22–30 (Volatile) | 70 |
| > 30 (Panic) | 85 |

**Score-based displacement:** When at position cap, a new high-conviction signal can evict the weakest same-direction position (by score) if it beats it by ≥ 5 points.

### ExecutorAgent
Places Alpaca orders and manages stops. On startup:
1. Cancels all open orders from previous session
2. Syncs `held_cache.json` to exactly match live Alpaca positions (removes stale entries)
3. Closes positions already down > 0.2%
4. Sets trailing stops on remaining positions (sized to current P&L)

After every new entry, places a hard stop immediately. Stop audit runs every 5 minutes. **EOD close fires at 3:30 PM ET** for all stock positions (crypto runs 24/7 and is never force-closed).

All buys use **notional (fractional) ordering** — every position spends exactly 9% of portfolio regardless of share price. No stock is ever skipped due to high price.

### LearningAgent
After every 10 closed trades (or daily at 4:05 PM ET), sends outcomes to **Claude Sonnet** for reflection. Extracted lessons are written to `AgentMemory` and injected into every future Predictor and Critic call.

### PortfolioAgent
Tracks sector concentration across all open positions. Seeds from `held_cache.json` on startup. Warns when a sector exceeds 40%; RiskAgent hard-blocks at 50%.

### MonitorAgent
Polls all open positions every 30 seconds. Fires `position.alert` for earnings approaching within 2 days (triggers pre-emptive close) or positions needing re-evaluation.

### MacroAgent
Fetches VIX, SPY, QQQ, and market breadth every 5 minutes. Publishes a regime (`NEUTRAL`, `RISK_OFF`, `PANIC`) that RiskAgent uses to adjust buy thresholds.

---

## Self-Improvement Loop

```
Trade closed → LearningAgent → Haiku reflection → AgentMemory
                                                       ↓
                              PredictorAgent ← lessons injected into every LLM call
                              CriticAgent   ←
```

After ≥ 50 closed outcomes, the **Weight Optimizer** runs Nelder-Mead to find optimal `[momentum, volume, technical, sentiment]` weights per archetype. Weights saved to `~/.stock_screener/weights.json`.

---

## Position Sizing & Risk

- Each position = **9% of current portfolio value** (minimum $50)
- **Fractional (notional) ordering** — spends exactly the slot size regardless of share price
- Hard trailing stop on entry: long −1.5%, short +0.8%
- Stop tiers based on unrealized P&L at startup:

| P&L | Trailing Stop |
|---|---|
| ≥ +20% | 0.5% (lock in gains) |
| ≥ +10% | 0.8% |
| ≥ +5% | 1.5% |
| 0–5% | 1.5% |
| −0.5% to 0% | 0.5% (tight — near break-even) |
| < −0.5% | Close immediately |

- All stock positions closed EOD at **3:30 PM ET**
- No new entries after **3:00 PM ET**

---

## Trade History

Trade history persists across restarts in `~/.stock_screener/trade_history.json` (last 100 trades). Each CLOSE entry records price, quantity, and P&L. The web dashboard displays this with green/red P&L indicators, refreshing every 5 seconds.

---

## Web Dashboard

```bash
uvicorn web:app --reload --port 8000
```

Shows live portfolio equity, positions with P&L, trade history, VIX/SPY macro state, and AgentMemory lessons. Auto-refreshes every 5 seconds.

---

## Quick Start

```bash
pip install -r requirements.txt

python run_agents.py                  # stocks
python run_agents.py --mode crypto    # crypto
python run_agents.py --mode both      # both
python run_agents.py --dry-run        # no orders

uvicorn web:app --reload --port 8000  # dashboard (separate terminal)
```

---

## Environment Variables

```ini
# Required (paper trading)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...

# Live trading
ALPACA_PAPER=false
ALPACA_LIVE_API_KEY=...
ALPACA_LIVE_SECRET_KEY=...

# AWS (for Bedrock NLP)
AWS_PROFILE=vignesh-sso-profile
AWS_REGION=us-east-1

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
| `weights.json` | Learned scoring weights per archetype |
| `agent_memory.json` | Lessons from LearningAgent reflections |
| `trade_history.json` | Rolling last 100 closed trades with P&L |
| `last_execution.json` | Most recent trade execution log |

---

## Cloud Deployment (AWS)

```bash
aws sso login --profile vignesh-sso-profile
./run_aws_bot.sh   # builds Docker, pushes to ECR, updates ECS

aws logs tail /ecs/stock-screener --region us-east-1 --follow --profile vignesh-sso-profile
```

Runs on ECS Fargate (1 vCPU, 4GB). NLP on Bedrock — no large model files in the Docker image.

---

## Disclaimer

For educational and informational purposes only. Not financial advice. Algorithmic trading carries significant risk of loss. Always validate with paper trading before using real capital.
