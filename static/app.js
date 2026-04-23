// Global error logging to server terminal
async function remoteLog(message, level = "INFO") {
    console.log(`[${level}] ${message}`);
    try {
        await fetch('/api/log', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level, message })
        });
    } catch (e) {
        console.error("Failed to send remote log", e);
    }
}

window.onerror = function(message, source, lineno, colno, error) {
    remoteLog(`JS Error: ${message} at ${source}:${lineno}:${colno}`, "ERROR");
    return false;
};

window.onunhandledrejection = function(event) {
    remoteLog(`Unhandled Promise Rejection: ${event.reason}`, "ERROR");
};

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
    const metrics = document.getElementById('perf-metrics');
    const positions = document.getElementById('perf-positions');
    const picks = document.getElementById('perf-picks');
    const initialMsg = document.getElementById('initial-load-msg');
    
    spinner.style.display = 'block';
    initialMsg.style.display = 'none';
    metrics.style.display = 'none';
    positions.style.display = 'none';
    picks.style.display = 'none';

    try {
        remoteLog("Fetching performance data...");
        const response = await fetch('/api/performance');
        if (!response.ok) throw new Error('Failed to fetch performance data: ' + response.statusText);
        
        const data = await response.json();
        remoteLog("Received performance data successfully.");
        
        // Format heartbeat / status
        let botStatusHtml = `<span style="color: #8b949e">Unknown</span>`;
        if (data.bot_status) {
            const statusColor = data.bot_status.status === "Active" ? "#3fb950" : (data.bot_status.status === "Sleeping" ? "#58a6ff" : "#8b949e");
            botStatusHtml = `<span style="color: ${statusColor}; font-weight: bold;">${data.bot_status.status}</span><br/><small style="color: #8b949e; font-size: 11px;">${data.bot_status.message}</small>`;
        }

        // Format last run time
        let lastRunStr = "Never";
        if (data.latest_run && data.latest_run.last_run_at) {
            try {
                const rawDate = data.latest_run.last_run_at;
                // If it doesn't have Z or a + offset, it's likely UTC from Python, so add Z
                const dateToParse = (rawDate.includes("Z") || rawDate.includes("+")) ? rawDate : rawDate + "Z";
                const lastRunDate = new Date(dateToParse);
                
                if (!isNaN(lastRunDate)) {
                    // Show Date and Time
                    const timeStr = lastRunDate.toLocaleDateString([], { month: 'short', day: 'numeric' }) + " " + 
                                   lastRunDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
                    
                    const trigger = data.latest_run.trigger || "UNKNOWN";
                    const triggerLabels = {
                        "SCHEDULED": "(Bot)",
                        "MANUAL": "(Manual)",
                        "FORCE_TRADE": "(Force)",
                        "CLI": "(CLI)",
                        "UNKNOWN": ""
                    };
                    const triggerStr = triggerLabels[trigger] || "";
                    
                    lastRunStr = `${timeStr} <small style="color: #8b949e; font-weight: normal;">${triggerStr}</small>`;
                } else {
                    remoteLog(`Failed to parse date: ${rawDate}`, "ERROR");
                }
            } catch (e) {
                remoteLog(`JS Date Error: ${e.message}`, "ERROR");
            }
        }

        // Metrics
        const metricsHtml = `
            <div class="card">
                <h3>Account Equity</h3>
                <div class="value">${data.alpaca.equity !== null ? "$" + parseFloat(data.alpaca.equity).toLocaleString(undefined, {minimumFractionDigits: 2}) : "N/A"}</div>
                <div class="sub-value">Buying Power: ${data.alpaca.buying_power !== null ? "$" + parseFloat(data.alpaca.buying_power).toLocaleString(undefined, {minimumFractionDigits: 2}) : "N/A"}</div>
            </div>
            <div class="card">
                <h3>Backtest Accuracy</h3>
                <div class="value">${data.backtest.accuracy !== null ? data.backtest.accuracy.toFixed(1) + "%" : "Calculating..."}</div>
                <div class="sub-value">Based on historical runs</div>
            </div>
            <div class="card">
                <h3>Current Bot Status</h3>
                <div class="value">${botStatusHtml}</div>
                <div class="sub-value">Last active: ${lastRunStr}</div>
            </div>
            <div class="card">
                <h3>Avg 10D Return (Backtest)</h3>
                <div class="value" style="color: ${data.backtest.total_return >= 0 ? "#3fb950" : (data.backtest.total_return < 0 ? "#f85149" : "")}">
                    ${data.backtest.total_return !== null ? (data.backtest.total_return >= 0 ? "+" : "") + data.backtest.total_return.toFixed(2) + "%" : "N/A"}
                </div>
            </div>
        `;
        document.getElementById("perf-metrics").innerHTML = metricsHtml;

        // Positions
        let positionsHtml = "";
        if (data.alpaca.positions && data.alpaca.positions.length > 0) {
            data.alpaca.positions.forEach(p => {
                const plColor = parseFloat(p.unrealized_plpc) >= 0 ? "bullish" : "bearish";
                const plPrefix = parseFloat(p.unrealized_plpc) >= 0 ? "+" : "";
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
            positionsHtml = "<div class=\"list-item\" style=\"color:#8b949e\">No active positions.</div>";
        }
        document.getElementById("positions-list").innerHTML = positionsHtml;

        // Top Picks
        document.getElementById("perf-picks").querySelector("h3").innerHTML = `Last Run's Top Picks (${lastRunStr})`;
        let picksHtml = "";
        if (data.latest_run.picks && data.latest_run.picks.length > 0) {
            data.latest_run.picks.forEach(pick => {
                const scoreColor = pick.prediction === "BULLISH" ? "bullish" : (pick.prediction === "BEARISH" ? "bearish" : "");
                picksHtml += `
                    <div class="list-item">
                        <strong>${pick.symbol}</strong>
                        <span class="${scoreColor}">${pick.prediction} (${pick.overall_score.toFixed(1)})</span>
                    </div>
                `;
            });
        } else {
            picksHtml = "<div class=\"list-item\" style=\"color:#8b949e\">No recent picks found.</div>";
        }
        document.getElementById("picks-list").innerHTML = picksHtml;

        spinner.style.display = "none";
        metrics.style.display = "grid";
        positions.style.display = "block";
        picks.style.display = "block";

    } catch (error) {
        remoteLog(`Error loading performance data: ${error.message}`, "ERROR");
        document.getElementById("perf-metrics").innerHTML = `<p style="color: #f85149">Error loading data: ${error.message}</p>`;
        spinner.style.display = "none";
        initialMsg.style.display = "block";
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
        remoteLog(`Manually running screener (min_return=${minReturn}, top_n=${topN})...`);
        const response = await fetch('/api/screen', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ min_return: minReturn, top_n: topN })
        });

        if (!response.ok) {
            throw new Error('Server error: ' + response.statusText);
        }

        const htmlReport = await response.text();
        resultDiv.innerHTML = htmlReport;
        remoteLog("Manual screener finished successfully.");
    } catch (error) {
        remoteLog(`Manual screener error: ${error.message}`, "ERROR");
        resultDiv.innerHTML = `<p style="color: #f85149;">Error: ${error.message}</p>`;
    } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
    }
}

async function forceTrade() {
    const btn = document.getElementById('force-btn');
    const spinner = document.getElementById('force-spinner');
    const resultDiv = document.getElementById('result');
    
    if (!confirm("Are you sure you want to force a trade cycle? This will place real paper trades.")) {
        return;
    }

    btn.disabled = true;
    spinner.style.display = 'block';
    resultDiv.innerHTML = '';
    
    try {
        remoteLog("Force Bot Execution triggered...");
        const response = await fetch('/api/force-trade', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ min_return: 10.0, top_n: 30 })
        });

        if (!response.ok) {
            throw new Error('Server error: ' + response.statusText);
        }

        const htmlFragment = await response.text();
        resultDiv.innerHTML = htmlFragment;
        remoteLog("Force Bot Execution finished.");
        
        // Refresh performance data after trade
        loadPerformance();
    } catch (error) {
        remoteLog(`Force trade error: ${error.message}`, "ERROR");
        resultDiv.innerHTML = `<p style="color: #f85149;">Error: ${error.message}</p>`;
    } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
    }
}

// Sidebar Resizer Logic
const resizer = document.getElementById('resizer');
const sidebar = document.getElementById('sidebar');
let isResizing = false;

resizer.addEventListener('mousedown', (e) => {
    isResizing = true;
    document.body.style.cursor = 'ew-resize';
    resizer.classList.add('active');
    e.preventDefault();
});

document.addEventListener('mousemove', (e) => {
    if (!isResizing) return;
    let newWidth = e.clientX;
    if (newWidth < 150) newWidth = 150;
    if (newWidth > 800) newWidth = 800;
    sidebar.style.width = newWidth + 'px';
});

document.addEventListener('mouseup', () => {
    if (isResizing) {
        isResizing = false;
        document.body.style.cursor = '';
        resizer.classList.remove('active');
    }
});

// Auto-load data when the page finishes loading
window.onload = () => {
    remoteLog("Page loaded, triggering initial data fetch...");
    if (document.getElementById('performance-tab').classList.contains('active')) {
        loadPerformance();
    }
};
