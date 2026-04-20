from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn
import asyncio
from concurrent.futures import ThreadPoolExecutor

from stock_sentiment.screener_app import ScreenerApp
from stock_sentiment.cloud_output import generate_html_report

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
            border: 1px solid #30363d;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        }
        h1 { margin-top: 0; color: var(--accent-color); }
        button {
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
        button:hover { background-color: var(--button-hover); }
        button:disabled { background-color: #555; cursor: not-allowed; }
        
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
            border: 1px solid #30363d;
            color: #e6edf3;
            padding: 8px;
            border-radius: 4px;
            width: 80px;
        }

        #result { margin-top: 2rem; width: 100%; max-width: 1200px; }
        
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
        <h1>📊 Stock Screener</h1>
        <p>Run the analysis to find top performing stocks with recent news sentiment and predictions.</p>
        
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

        <button id="run-btn" onclick="runScreener()">Run Screener</button>
        <div class="spinner" id="spinner"></div>
    </div>
    
    <div id="result"></div>

    <script>
        async function runScreener() {
            const btn = document.getElementById('run-btn');
            const spinner = document.getElementById('spinner');
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
                resultDiv.innerHTML = `<p style="color: #ef4444;">Error: ${error.message}</p>`;
                btn.disabled = false;
                spinner.style.display = 'none';
            }
        }
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
