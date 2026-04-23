# Institutional-Grade Automated Swing Trading Platform

A quantitative, serverless algorithmic trading platform built in Python. This system operates autonomously in the cloud, screening equities, analyzing financial news via NLP, computing technical indicators, and executing risk-managed trades through the Alpaca Brokerage API.

---

## 1. Core Services (The Decision Stack)

### 1.1 HuggingFace & FinBERT (Artificial Intelligence)
*   **Inference Engine:** Utilizes the `transformers` library to load the `ProsusAI/finbert` model—a BERT architecture fine-tuned specifically for financial sentiment.
*   **Sentiment Logic:** Classifies news headlines into three labels (Positive, Negative, Neutral) and produces a normalized score between `-1.0` and `+1.0`.
*   **Exponential Time Decay:** Implements a recency-bias algorithm for news analysis:
    *   **< 12h old:** 1.0x weight multiplier.
    *   **12h - 24h:** 0.8x weight multiplier.
    *   **1d - 3d:** 0.5x weight multiplier.
    *   **3d - 7d:** 0.2x weight multiplier.
*   **Consensus Scaling:** Final sentiment scores are scaled based on consensus; a directional alignment of >80% across multiple sources triggers a conviction boost.

### 1.2 Yahoo Finance API (Market Ingestion)
*   **Data Fetching:** Uses `yfinance` to retrieve 90-day OHLCV (Open, High, Low, Close, Volume) data packets in batch mode for performance.
*   **Gatekeeper Screening:** An algorithmic "culling" process that instantly discards any symbol failing the **10.0% 3-month return** threshold, conserving compute resources for momentum leaders.
*   **Volatility Avoidance:** Queries the earnings calendar. If a reporting date is detected within a **3-day window**, the engine executes a hard override, forcing a BEARISH rating to avoid overnight gap-risk.

### 1.3 Alpaca Trading API (Execution Layer)
*   **SDK:** Built on the `alpaca-py` library utilizing the `TradingClient` and `MarketOrderRequest` classes.
*   **Standardized Sizing:** Implements a "Slot-Based" portfolio model:
    *   **Capacity:** Strictly capped at **10 active positions**.
    *   **Notional Value:** Executes fractional share orders for exactly **$1,000 per slot**.
    *   **Total Exposure:** Mathematically constrained to a maximum of $10,000.
*   **Risk Management:** Continuous monitoring of held assets. If a conviction score is downgraded to "Neutral" or "Bearish," the broker triggers an immediate market liquidation.

---

## 2. Cloud Infrastructure (The AWS Stack)

### 2.1 Amazon ECS Fargate (Compute)
*   **Deployment Model:** Managed Service (Desired Count: 1).
*   **Resources:** Provisioned at **1 vCPU and 4GB RAM** to accommodate the FinBERT model's memory footprint (~1.2GB).
*   **Lifecycle:** The container is "Always On" and self-healing. AWS Elastic Network Interfaces (ENI) ensure high-speed connectivity for real-time API calls.
*   **Concurrency:** Employs a `ThreadPoolExecutor(max_workers=2)` to manage parallel AI inference while preventing CPU saturation.

### 2.2 Application Load Balancer (ALB)
*   **Routing:** Maps public Port 80 traffic to the container's Port 8080.
*   **Health Checks:** Automatically pings the `/health` endpoint every 30 seconds.
*   **Network Security:** Deployed with a "Surgical" Security Group model:
    *   **ALB-SG:** Accepts inbound traffic from `0.0.0.0/0`.
    *   **Task-SG:** Accepts inbound traffic **only** from the ALB-SG.

### 2.3 Amazon DynamoDB (Persistence)
*   **Architecture:** Dual-Backend system that defaults to **Managed NoSQL** in production.
*   **Tables:**
    *   `PROD_StockScreenerRuns`: Metadata for every 30-minute cycle.
    *   `PROD_StockScreenerPredictions`: Detailed scoring metrics for every stock screened.
    *   `PROD_StockScreenerStatus`: A single-item table tracking the real-time **Bot Heartbeat**.
*   **Migration Logic:** The application handles its own schema evolution, automatically adding columns (like `trigger_type`) if they are missing from existing tables.

### 2.4 Amazon S3 & SES (Reporting)
*   **S3 Archival:** Generates high-fidelity HTML reports for every cycle, saved with a `/YYYY/MM/DD/` prefix.
*   **SES (Simple Email Service):** For verified identities, the bot sends automated trade alerts and daily performance summaries.

### 2.5 Amazon ECR & CloudWatch (Ops)
*   **Image Hosting:** Private ECR repository stores Intel-compatible (linux/amd64) Docker images.
*   **Centralized Logging:** Verbose bot activity is streamed to `/ecs/stock-screener`. This includes the "Brain" logs (`[StockPredictor]`) and "Hands" logs (`[PaperBroker]`).

---

## 3. Local Development

### 3.1 Local Setup (DEV Mode)
In local mode, the bot is zero-cost and uses a local file for history.
```bash
# Set Alpaca keys in .env
pip install -r requirements.txt

# Start the dashboard + bot locally (uses SQLite)
uvicorn web:app --reload --port 8000
```

### 3.2 Production Deployment (PROD Mode)
The entire AWS infrastructure is provisioned via the master deployment script.
```bash
# 1. Update .env with fresh AWS SSO credentials
# 2. Deploy/Update the Managed Service
./run_aws_bot.sh
```

---

## 4. Technical Architecture Flow
1.  **Poll:** Scheduler wakes up every 30 minutes and checks the **Alpaca Market Clock**.
2.  **Screen:** Fetch technicals for 300 stocks; filter for top performers.
3.  **Analyze:** Fetch news; run FinBERT inference on 200+ articles.
4.  **Score:** Calculate weighted average of Sentiment (30%), Technicals (30%), and Momentum (40%).
5.  **Trade:** Submit API orders if score >= 75 and slot is available.
6.  **Persist:** Write full state to DynamoDB and update Heartbeat.

---

## 5. Disclaimer
This tool is for educational and informational purposes only. It does NOT constitute financial advice. Algorithmic trading involves significant risk. Always use Paper Trading for testing before committing real capital.
