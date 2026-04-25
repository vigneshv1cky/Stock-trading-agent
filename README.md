# Institutional-Grade Automated Swing Trading Platform

A quantitative, serverless algorithmic trading platform built in Python. This system operates autonomously in the cloud, utilizing a sophisticated pipeline that screens a curated universe of equities, analyzes financial news sentiment via NLP (FinBERT), calculates technical momentum indicators, and executes mathematically risk-managed trades through the Alpaca Brokerage API.

It also features a real-time Web Command Center built with FastAPI for performance tracking, manual overrides, and seamless cloud deployment.

---

## 1. Core Services (The Decision Stack)

### 1.1 The Screener (Gatekeeper Logic)
The `StockScreener` processes a static `SCREEN_UNIVERSE` of roughly 200+ high-alpha, liquid equities (e.g., TSLA, NVDA, PLTR, CRWD) and subjects them to rigorous institutional barricades:

*   **Filter 1: Volume Requirement:** Rejects any stock with a Relative Volume (RVOL) `< 1.0`. The current daily volume must exceed its 20-day average to guarantee institutional interest.
*   **Filter 2: Earnings Avoidance:** Rejects any stock reporting earnings within the next **3 days** to avoid overnight gap-risk and extreme volatility.
*   **Filter 3: Archetype Classification (The "OR" Gate):** A stock must fit into one of three specific swing-trading archetypes to proceed:
    *   **Breakout Star:** 1-week price change $\ge$ 10.0% OR 1-month change $\ge$ 15.0%.
    *   **Recovery Phoenix:** 3-month drawdown $\le$ -15.0% AND recent 3-day bounce $\ge$ 4.0% AND RVOL > 1.1.
    *   **Momentum King:** 3-month change $\ge$ 7.0%.

The top 40 stocks (ranked by RVOL and 1-week performance) are passed to the Brain.

### 1.2 The Predictor (The Brain)
The `StockPredictor` calculates a normalized conviction score (0 to 100) using a dynamically weighted formula. A score $\ge$ 60 yields a **BULLISH** rating, while $\le$ 40 yields **BEARISH**. 
The final score is composed of four pillars:

*   **Sentiment (25% Weight):** Utilizes `ProsusAI/finbert` (HuggingFace) to analyze recent news headlines. Scores are normalized to -1.0 to +1.0 and scaled. A bonus of +15 points is awarded if a stock has $\ge$ 3 highly bullish headlines.
*   **Technicals (25% Weight):** Context-aware RSI scoring. For example, a "Recovery" stock gets a 95% technical score if its 14-day RSI is < 35 (oversold), whereas a "Momentum" stock relies on RSI < 70 to confirm room to run.
*   **Volume (20% Weight):** Direct scaling based on RVOL. 
*   **Momentum (30% Weight):** Archetype-specific momentum grading (e.g., heavily weighting the 1-week change for Breakouts, and 3-month change for Momentum Kings).

### 1.3 The Broker (Execution Layer & Smart Swapping)
The `PaperBroker` integrates directly with the Alpaca API using `alpaca-py`.

*   **Portfolio Sizing:** Capped at exactly **10 active positions**. Each position is allocated a fixed **$1,000 slot**.
*   **Order Execution:** Executes Market Orders using *whole shares* (derived from the $1,000 budget) rather than fractional notionals to ensure compatibility with advanced order types.
*   **Risk Management:** Immediately wraps every new position in a **3.0% Trailing Stop Order** (GTC). 
*   **Forced Liquidation:** If the AI downgrades an existing holding to "BEARISH", all open orders (stops) are canceled and the position is liquidated at market price.
*   **Smart Conviction Swapping:** If the bot discovers a new BULLISH pick but the portfolio is full (or lacks cash), it checks the lowest-scoring asset currently held. If the new pick outscores the weakest link by **> 5 points**, the bot automatically sells the weak holding to fund the upgrade.

---

## 2. System Architecture & Operation

### 2.1 The Autonomous Scheduler
The `Scheduler` module wakes up every 30 minutes, checks the Alpaca Market Clock, and only executes its cycle if the market is Open (or in extended hours, depending on config). It coordinates the ingestion, analysis, prediction, and execution phases entirely autonomously.

### 2.2 Web Command Center (FastAPI)
The project includes `web.py`, a FastAPI dashboard providing real-time oversight:
*   **Background Execution:** The 30-minute scheduler runs continuously in a daemon background thread.
*   **Authentication:** Secured via an `ADMIN_USERNAME` and `ADMIN_PASSWORD` (defaults to `admin` / `changeme`).
*   **Capabilities:** View real-time Alpaca portfolio performance, equity curves, history, and trigger manual/forced screening cycles.
*   **Dual-Backend Persistence:** Uses SQLite for local development and Amazon DynamoDB for cloud production (auto-migrating schemas).

---

## 3. Cloud Infrastructure (The AWS Stack)

The entire platform is configured for zero-downtime, serverless deployment on AWS via `./deploy/deploy.sh`.

### 3.1 Amazon ECS Fargate (Compute)
*   **Deployment Model:** 1 vCPU and 4GB RAM to accommodate the FinBERT model's ~1.2GB memory footprint.
*   **Concurrency:** Employs a `ThreadPoolExecutor` to handle API requests and background processes without CPU saturation.

### 3.2 Networking (VPC & ALB)
*   **Application Load Balancer (ALB):** Maps public HTTP traffic (Port 80) to the Fargate container (Port 8080).
*   **Security Groups:** Strictly isolates the container, allowing inbound traffic *only* from the ALB security group.

### 3.3 Amazon DynamoDB & S3 (Persistence & Reporting)
*   **DynamoDB Tables:** Auto-provisioned tables including `PROD_StockScreenerRuns`, `PROD_StockScreenerPredictions`, and `PROD_StockScreenerStatus` (acting as a heartbeat monitor).
*   **S3 Archival:** Automatically buckets HTML snapshots of every execution cycle, grouped by date prefixes (`/YYYY/MM/DD/`).
*   **SES Alerts:** Sends critical trade executions and downgrades to verified email identities.

---

## 4. Local Development

### 4.1 Local Setup (DEV Mode)
In local mode, the bot operates at zero-cost using SQLite.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment (Create a .env file)
# ALPACA_API_KEY=your_key
# ALPACA_SECRET_KEY=your_secret
# ADMIN_PASSWORD=your_secure_password

# 3. Start the dashboard + bot locally
uvicorn web:app --reload --port 8000
```

### 4.2 Production Deployment (PROD Mode)
The entire AWS infrastructure is provisioned via the master deployment script.

**Workflow:**
1.  **Refresh Login:** `aws sso login --profile your-sso-profile`
2.  **Deploy:** `./run_aws_bot.sh`

*(The deployment script automatically reads your `.env` file, builds the Intel `linux/amd64` Docker image, pushes to ECR, and updates the ECS Service).*

---

## 5. Disclaimer
This tool is for educational and informational purposes only. It does NOT constitute financial advice. Algorithmic trading involves significant risk. Always use Paper Trading for testing before committing real capital.
