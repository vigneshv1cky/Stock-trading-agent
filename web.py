from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from stock_sentiment.screener_app import ScreenerApp
from stock_sentiment.cloud_output import generate_html_report
from stock_sentiment.history import History
from stock_sentiment.market.broker import PaperBroker

app = FastAPI(title="Stock Screener Web App")

# Create a thread pool to run the screener
executor = ThreadPoolExecutor(max_workers=2)

html_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stock Screener</title>
    <style>
        :root {
            --bg-color: #0d1117;
            --text-color: #e6edf3;
            --accent-color: #58a6ff;
            --button-bg: #238636;
            --button-hover: #2ea043;
            --tab-bg: #161b22;
            --tab-active: #21262d;
            --border-color: #30363d;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        .container {
            max-width: 800px;
            width: 100%;
            text-align: center;
            background: #161b22;
            padding: 2rem;
            border-radius: 12px;
            border: 1px solid var(--border-color);
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
            margin-bottom: 2rem;
        }
        h1 { margin-top: 0; color: var(--accent-color); }
        
        .tabs {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 10px;
        }
        .tab-btn {
            background: var(--tab-bg);
            color: var(--text-color);
            border: 1px solid var(--border-color);
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
            font-weight: bold;
        }
        .tab-btn.active {
            background: var(--tab-active);
            border-color: var(--accent-color);
            color: var(--accent-color);
        }
        .tab-btn:hover:not(.active) {
            background: #1f2428;
        }
        
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }

        button.action-btn {
            background-color: var(--button-bg);
            color: white;
            border: none;
            padding: 12px 24px;
            font-size: 16px;
            font-weight: bold;
            border-radius: 6px;
            cursor: pointer;
            transition: background-color 0.2s;
        }
        button.action-btn:hover { background-color: var(--button-hover); }
        button.action-btn:disabled { background-color: #555; cursor: not-allowed; }
        
        .form-group {
            margin-bottom: 1.5rem;
            display: flex;
            justify-content: center;
            gap: 1rem;
        }
        
        .form-group label {
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        input {
            background: #0d1117;
            border: 1px solid var(--border-color);
            color: #e6edf3;
            padding: 8px;
            border-radius: 4px;
            width: 80px;
        }

        #result { margin-top: 2rem; width: 100%; max-width: 1200px; }
        
        .metric-cards {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
            text-align: left;
        }
        .card {
            background: #0d1117;
            border: 1px solid var(--border-color);
            padding: 1.5rem;
            border-radius: 8px;
        }
        .card h3 {
            margin: 0 0 0.5rem 0;
            font-size: 14px;
            color: #8b949e;
        }
        .card .value {
            font-size: 24px;
            font-weight: bold;
            color: var(--text-color);
        }
        .card .sub-value {
            font-size: 12px;
            color: #8b949e;
            margin-top: 4px;
        }

        .list-container {
            text-align: left;
            background: #0d1117;
            border: 1px solid var(--border-color);
            padding: 1rem;
            border-radius: 8px;
            margin-top: 1rem;
        }
        .list-container h3 {
            margin-top: 0;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 0.5rem;
        }
        .list-item {
            padding: 0.5rem 0;
            border-bottom: 1px solid #21262d;
            display: flex;
            justify-content: space-between;
        }
        .list-item:last-child {
            border-bottom: none;
        }
        .bullish { color: #3fb950; }
        .bearish { color: #f85149; }

        /* Spinner */
        .spinner {
            display: none;
            width: 40px;
            height: 40px;
            margin: 20px auto;
            border: 4px solid rgba(255, 255, 255, 0.1);
            border-radius: 50%;
            border-top-color: var(--accent-color);
            animation: spin 1s ease-in-out infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="container" id="main-container">
        <h1>📊 Stock Screener & Bot</h1>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('performance')">Bot Performance</button>
            <button class="tab-btn" onclick="switchTab('manual')">Manual Screener</button>
        </div>

        <div id="performance-tab" class="tab-content active">
            <p>Recent performance metrics from Alpaca and local backtests.</p>
            <div class="spinner" id="perf-spinner" style="display: block;"></div>
            <div id="perf-content" style="display: none;">
                <div class="metric-cards" id="perf-metrics">
                    <!-- Metrics will be injected here -->
                </div>
                <div id="perf-positions" class="list-container">
                    <h3>Active Positions</h3>
                    <div id="positions-list"></div>
                </div>
                <div id="perf-picks" class="list-container">
                    <h3>Last Run's Top Picks</h3>
                    <div id="picks-list"></div>
                </div>
            </div>
            <button class="action-btn" style="margin-top: 1rem;" onclick="loadPerformance()">Refresh Data</button>
        </div>

        <div id="manual-tab" class="tab-content">
            <p>Run the analysis manually to find top performing stocks.</p>
            <div class="form-group">
                <label>
                    Min 3-Month Return (%):
                    <input type="number" id="min_return" value="10.0" step="0.1">
                </label>
                <label>
                    Top N:
                    <input type="number" id="top_n" value="30">
                </label>
            </div>

            <button class="action-btn" id="run-btn" onclick="runScreener()">Run Screener</button>
            <div class="spinner" id="manual-spinner"></div>
        </div>
    </div>
    
    <div id="result"></div>

    <script>
        function switchTab(tabId) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            
            event.target.classList.add('active');
            document.getElementById(tabId + '-tab').classList.add('active');
            
            if (tabId === 'performance') {
                loadPerformance();
            }
        }

        async function loadPerformance() {
            const spinner = document.getElementById('perf-spinner');
            const content = document.getElementById('perf-content');
            
            spinner.style.display = 'block';
            content.style.display = 'none';

            try {
                const response = await fetch('/api/performance');
                if (!response.ok) throw new Error('Failed to fetch performance data');
                
                const data = await response.json();
                
                // Metrics
                const metricsHtml = `
                    <div class="card">
                        <h3>Account Equity</h3>
                        <div class="value">${data.alpaca.equity !== null ? '$' + parseFloat(data.alpaca.equity).toLocaleString(undefined, {minimumFractionDigits: 2}) : 'N/A'}</div>
                        <div class="sub-value">Buying Power: ${data.alpaca.buying_power !== null ? '$' + parseFloat(data.alpaca.buying_power).toLocaleString(undefined, {minimumFractionDigits: 2}) : 'N/A'}</div>
                    </div>
                    <div class="card">
                        <h3>Backtest Accuracy</h3>
                        <div class="value">${data.backtest.accuracy !== null ? data.backtest.accuracy.toFixed(1) + '%' : 'N/A'}</div>
                        <div class="sub-value">Based on historical runs</div>
                    </div>
                    <div class="card">
                        <h3>Avg 10D Return (Backtest)</h3>
                        <div class="value" style="color: ${data.backtest.total_return >= 0 ? '#3fb950' : (data.backtest.total_return < 0 ? '#f85149' : '')}">
                            ${data.backtest.total_return !== null ? (data.backtest.total_return >= 0 ? '+' : '') + data.backtest.total_return.toFixed(2) + '%' : 'N/A'}
                        </div>
                    </div>
                `;
                document.getElementById('perf-metrics').innerHTML = metricsHtml;

                // Positions
                let positionsHtml = '';
                if (data.alpaca.positions && data.alpaca.positions.length > 0) {
                    data.alpaca.positions.forEach(p => {
                        const plColor = parseFloat(p.unrealized_plpc) >= 0 ? 'bullish' : 'bearish';
                        const plPrefix = parseFloat(p.unrealized_plpc) >= 0 ? '+' : '';
                        positionsHtml += `
                            <div class="list-item">
                                <strong>${p.symbol}</strong>
                                <span>${p.qty} shares @ $${parseFloat(p.avg_entry_price).toFixed(2)}</span>
                                <span class="${plColor}">${plPrefix}${(parseFloat(p.unrealized_plpc) * 100).toFixed(2)}%</span>
                            </div>
                        `;
                    });
                } else if (data.alpaca.error) {
                    positionsHtml = `<div class="list-item" style="color:#8b949e">${data.alpaca.error}</div>`;
                } else {
                    positionsHtml = '<div class="list-item" style="color:#8b949e">No active positions.</div>';
                }
                document.getElementById('positions-list').innerHTML = positionsHtml;

                // Top Picks
                let picksHtml = '';
                if (data.latest_run.picks && data.latest_run.picks.length > 0) {
                    data.latest_run.picks.forEach(pick => {
                        const scoreColor = pick.prediction === 'BULLISH' ? 'bullish' : (pick.prediction === 'BEARISH' ? 'bearish' : '');
                        picksHtml += `
                            <div class="list-item">
                                <strong>${pick.symbol}</strong>
                                <span class="${scoreColor}">${pick.prediction} (${pick.overall_score.toFixed(1)})</span>
                            </div>
                        `;
                    });
                } else {
                    picksHtml = '<div class="list-item" style="color:#8b949e">No recent picks found.</div>';
                }
                document.getElementById('picks-list').innerHTML = picksHtml;

                spinner.style.display = 'none';
                content.style.display = 'block';

            } catch (error) {
                console.error(error);
                document.getElementById('perf-metrics').innerHTML = `<p style="color: #f85149">Error loading data: ${error.message}</p>`;
                spinner.style.display = 'none';
                content.style.display = 'block';
            }
        }

        async function runScreener() {
            const btn = document.getElementById('run-btn');
            const spinner = document.getElementById('manual-spinner');
            const resultDiv = document.getElementById('result');
            const minReturn = parseFloat(document.getElementById('min_return').value);
            const topN = parseInt(document.getElementById('top_n').value);

            btn.disabled = true;
            spinner.style.display = 'block';
            resultDiv.innerHTML = '';
            
            try {
                const response = await fetch('/api/screen', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ min_return: minReturn, top_n: topN })
                });

                if (!response.ok) {
                    throw new Error('Server error: ' + response.statusText);
                }

                const htmlReport = await response.text();
                // Replace the entire document to show the report completely
                document.open();
                document.write(htmlReport);
                document.close();
            } catch (error) {
                resultDiv.innerHTML = `<p style="color: #f85149;">Error: ${error.message}</p>`;
                btn.disabled = false;
                spinner.style.display = 'none';
            }
        }

        // Load performance data initially if it's the active tab
        window.onload = () => {
            if (document.getElementById('performance-tab').classList.contains('active')) {
                loadPerformance();
            }
        };
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return html_template

@app.get("/health")
def health_check():
    return {"status": "ok"}

class ScreenRequest(BaseModel):
    min_return: float = 10.0
    top_n: int = 30

@app.post("/api/screen", response_class=HTMLResponse)
async def screen_stocks(req: ScreenRequest):
    def _run_screener():
        screener_app = ScreenerApp(min_return=req.min_return, top_n=req.top_n)
        predictions, count, alerts = screener_app.run(cloud_mode=False)
        return generate_html_report(predictions, count)

    loop = asyncio.get_running_loop()
    html_report = await loop.run_in_executor(executor, _run_screener)
    return html_report

@app.get("/api/performance", response_class=JSONResponse)
def get_performance():
    # 1. Fetch Backtest Stats & Latest Run from Local History
    history = History()
    try:
        backtest_stats = history.get_backtest_stats()
        if backtest_stats and "accuracy" in backtest_stats and backtest_stats["accuracy"] is not None:
            accuracy_pct = backtest_stats["accuracy"] * 100
        else:
            accuracy_pct = None
        total_return = backtest_stats.get("avg_return_10d")
    except Exception as e:
        accuracy_pct = None
        total_return = None

    try:
        latest_run = history.get_latest_run()
        if latest_run:
            picks = history.get_predictions_for_run(latest_run["id"])
            # limit to top 5
            picks = picks[:5]
        else:
            picks = []
    except Exception as e:
        latest_run = None
        picks = []

    # 2. Fetch Alpaca data
    broker = PaperBroker()
    alpaca_data = {"equity": None, "buying_power": None, "positions": [], "error": None}
    if broker.client:
        try:
            account = broker.client.get_account()
            alpaca_data["equity"] = float(account.equity)
            alpaca_data["buying_power"] = float(account.buying_power)
            
            positions = broker.client.get_all_positions()
            alpaca_data["positions"] = [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "unrealized_plpc": float(p.unrealized_plpc)
                } for p in positions
            ]
        except Exception as e:
            alpaca_data["error"] = f"Failed to fetch Alpaca data: {e}"
    else:
        alpaca_data["error"] = "Alpaca integration disabled or keys missing."

    return {
        "backtest": {
            "accuracy": accuracy_pct,
            "total_return": total_return
        },
        "latest_run": {
            "picks": picks
        },
        "alpaca": alpaca_data
    }
