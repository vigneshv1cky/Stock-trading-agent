# Institutional-Grade Automated Swing Trading Platform

A quantitative, serverless algorithmic trading platform built in Python. This system operates autonomously in the cloud, screening equities, analyzing financial news via NLP, computing technical indicators, and executing risk-managed trades through the Alpaca Brokerage API.

---

## 1. System Architecture & Concurrency Model

The platform is engineered as a hybrid application, exposing both a synchronous web dashboard and an asynchronous background daemon within a single Docker container.

### 1.1 Application Entry Points
*   **The Web Dashboard (Interactive Mode):** Built on **FastAPI** and served via **Uvicorn** (ASGI). It provides a responsive, dark-themed UI using vanilla HTML/JS. The dashboard asynchronously fetches reports, rendering inline SVG sparklines for 3-month trend visualization. 
*   **The Automated Trading Bot (Scheduled Mode):** A background daemon thread initialized via FastAPI's `@app.on_event("startup")`. It runs an infinite loop managed by the `Scheduler` class.
*   **Concurrency Constraints:** The NLP model (FinBERT) requires significant memory overhead (~1.2GB per active thread). To prevent Out-Of-Memory (OOM) fatal errors on a 4GB RAM container, concurrent manual requests are throttled using a `ThreadPoolExecutor` strictly limited to `max_workers=2`.

### 1.2 Cloud Infrastructure (AWS)
The application is deployed using Infrastructure as Code (IaC) principles and managed cloud services:
*   **Compute (AWS ECS Fargate):** The Docker image is hosted on Amazon ECR and deployed to Elastic Container Service using the Fargate serverless compute engine (provisioned at 1 vCPU, 4GB RAM).
*   **Persistence (Amazon DynamoDB):** The application utilizes `boto3` to auto-provision NoSQL tables (`StockScreenerRuns`, `StockScreenerPredictions`, `StockScreenerAlerts`) on startup. It uses `PAY_PER_REQUEST` billing. This ensures historical backtesting data survives ephemeral container restarts.
*   **Networking (ALB & ACM):** An Application Load Balancer routes traffic to the container via port 8080. Because NLP inference and historical data fetching can take up to 180 seconds, the ALB idle timeout is explicitly configured to 300 seconds to prevent 504 Gateway Timeout exceptions.

---

## 2. The Decision Engine (The Brain)

The core logic is a deterministic, multi-factor weighted scoring model that outputs a conviction score from 0 to 100. A composite score >= 75 triggers a high-conviction BULLISH rating and subsequent execution.

### 2.1 The Universe & Gatekeeper Filtering
*   **Curated Universe:** The system monitors ~350 highly liquid equities. The universe is heavily weighted toward sectors highly reactive to macroeconomic and geopolitical catalysts (Defense, Energy, Maritime Shipping, Cybersecurity, and Global Finance).
*   **Performance Gatekeeper:** The `PriceFetcher` downloads 90 days of OHLCV data via the `yfinance` API. Equities failing to achieve a minimum 10.0 percent return over the 3-month lookback period are instantly discarded. This algorithmic culling conserves NLP inference compute for proven momentum leaders.

### 2.2 Momentum Scoring (40 Percent Weight)
Rewards "Cascading Momentum" across three specific timeframes, penalizing sudden trend reversals:
*   **3-Month Return (Macro Trend):** > 50 percent yields maximum base points.
*   **1-Month Return (Stability):** Filters out long-term winners that are currently stagnating.
*   **1-Week Return (Breakout Velocity):** Heavily weights the last 5 trading days to capture immediate breakout energy.

### 2.3 Time-Weighted NLP Sentiment (30 Percent Weight)
*   **Data Ingestion:** Scrapes the Google News RSS feed for the top 10 articles per ticker over the trailing 7 days.
*   **Inference:** Utilizes `ProsusAI/finbert` (a BERT model fine-tuned on financial corpora) to classify text and output a normalized score between -1.0 and +1.0.
*   **Exponential Time Decay:** Applies a recency bias weighting mechanism. "Breaking news" (< 12 hours old) receives a 1.0 weight multiplier. News aged 1 to 3 days receives a 0.5 multiplier, and news 3 to 7 days old decays to a 0.2 multiplier.
*   **Consensus Scaling:** If > 80 percent of articles align directionally, a mathematical boost is applied to the final sentiment integer. Conversely, if volume is low (< 5 articles), a muffling factor pulls the score toward neutral (50.0).

### 2.4 Technical Health & Liquidity (30 Percent Weight)
*   **Relative Volume (RVOL):** Compares the previous full trading day's volume against a 20-day Simple Moving Average (SMA) baseline. 
    *   Volume > 2.0x adds a 15-point reward (indicating institutional accumulation). 
    *   Volume < 0.7x subtracts 10 points (indicating retail noise or low conviction).
*   **RSI Constraints:** Calculates a 14-day Relative Strength Index.
    *   RSI < 30 adds a 30-point mean-reversion reward. 
    *   RSI > 70 triggers a strict 20-point punishment, mathematically blocking the system from executing buy orders at the peak of a hype cycle.
*   **Trend Confirmation:** Evaluates MACD crossovers (fast vs. slow moving averages) and Price vs. SMA-20 positioning.

### 2.5 Volatility Avoidance (Earnings Kill-Switch)
*   Queries the `yfinance` calendar API for the next reporting date. If an earnings report is scheduled <= 3 days away, the engine executes a Hard Override: the rating is forced to BEARISH, confidence is halved, and buy execution is blocked to avoid overnight gap-down risk.

---

## 3. The Executor (Risk Management)

The `PaperBroker` module interfaces with the Alpaca Trading API to manage capital based strictly on the Decision Engine's output.

### 3.1 Market Synchronization & Volatility Buffers
*   The scheduler queries the official Alpaca Market Clock.
*   **Sleep Optimization:** If the market is closed, it calculates the exact delta in seconds to the next open and initiates a thread sleep, conserving compute cycles.
*   **Witching Hour Avoidance:** The executor intentionally skips the first 30 minutes (09:30 to 10:00 EST) and the last 30 minutes (15:30 to 16:00 EST) of the trading session to avoid institutional block-trading volatility and opening gap-fakeouts.

### 3.2 Slot-Based Portfolio Management
*   **Capacity Limit:** The portfolio is strictly capped at 10 active positions to enforce structural diversification.
*   **Position Sizing:** Executes Market Orders (`TimeInForce.DAY`) to purchase exactly 1000.00 of fractional shares per open slot. This mathematical constraint caps maximum total exposure at exactly 10,000.00.

### 3.3 Dynamic Capital Preservation (Soft Stop-Loss)
*   At each hourly interval, the bot cross-references the current Alpaca portfolio holdings against the latest AI predictions.
*   If a currently held asset is downgraded to a BEARISH rating (due to shifting NLP sentiment, technical chart breakdown, or an approaching earnings date), the Executor instantly submits a Market Sell Order to liquidate the entire position, cutting losses autonomously.

---

## 4. Deployment & Operations

### 4.1 Local Development Environment
Requires a `.env` file in the root directory containing `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. The `python-dotenv` library injects these into the environment on boot.

```bash
# Activate virtual environment
source .venv/bin/activate

# Start the FastAPI web dashboard and background trading bot
uvicorn web:app --reload --port 8000
```

### 4.2 AWS Production Deployment
The deployment shell script handles Docker compilation, Amazon ECR authentication, layer pushing, and triggering the ECS service update.

```bash
# Ensure AWS CLI is authenticated
aws sso login --profile AdministratorAccess-707421297730
export AWS_PROFILE=AdministratorAccess-707421297730

# Execute deployment and rolling update
./deploy/deploy_ecs_express.sh
```

### 4.3 Analytics & CLI Mode
The core logic can be executed via the command line for rapid testing and historical verification without spinning up the ASGI server.

```bash
# Calculate historical win-rate and average returns from DynamoDB
python run.py --backtest

# View recent momentum flips and high-conviction alerts
python run.py --alerts

# Run a manual headless screen overriding the default return threshold
python run.py --min-return 25 --top 10
```