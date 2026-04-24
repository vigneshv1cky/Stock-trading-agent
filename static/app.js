let lastScanTime = null;
let lastHeartbeatTime = null;
let nextRunTime = null;
let tickerInterval = null;

async function remoteLog(message, level = "INFO") {
    try { await fetch('/api/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ level, message }) });
    } catch (e) {}
}

window.onerror = (m, s, l, c, e) => { remoteLog(`JS Error: ${m} at ${s}:${l}:${c}`, "ERROR"); return false; };

function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    if (event && event.target) { event.target.classList.add('active'); }
    document.getElementById(tabId + '-tab').classList.add('active');
    if (tabId === 'performance') { loadPerformance(); } 
    else if (tabId === 'history') { loadHistory(); }
}

async function loadHistory() {
    const tableBody = document.getElementById('history-table-body');
    const spinner = document.getElementById('history-spinner');
    spinner.style.display = 'block';
    tableBody.innerHTML = '';
    try {
        const response = await fetch('/api/history');
        const data = await response.json();
        if (data.history && data.history.length > 0) {
            data.history.forEach(trade => {
                const plColor = trade.pl_dollars >= 0 ? "bullish" : "bearish";
                const plPrefix = trade.pl_dollars >= 0 ? "+" : "";
                tableBody.innerHTML += `<tr><td><strong>${trade.symbol}</strong></td><td>$${trade.entry_price.toFixed(2)}</td><td>$${trade.exit_price.toFixed(2)}</td><td>${trade.qty.toFixed(2)}</td><td class="${plColor}">${plPrefix}$${trade.pl_dollars.toFixed(2)}</td><td class="${plColor}">${plPrefix}${trade.pl_pct.toFixed(2)}%</td><td>${trade.exit_time}</td><td><span class="badge ${trade.status.toLowerCase()}">${trade.status}</span></td></tr>`;
            });
        } else { tableBody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#8b949e">No matched trade pairs found.</td></tr>'; }
    } catch (e) { remoteLog("Error loading history: " + e.message, "ERROR"); } 
    finally { spinner.style.display = 'none'; }
}

function startTicker() {
    if (tickerInterval) clearInterval(tickerInterval);
    tickerInterval = setInterval(updateTickers, 1000);
}

function updateTickers() {
    const now = new Date();
    
    if (lastScanTime) {
        const diff = Math.floor((now - lastScanTime) / 1000);
        let timeAgo = diff < 60 ? `${diff}s ago` : diff < 3600 ? `${Math.floor(diff / 60)}m ago` : `${Math.floor(diff / 3600)}h ago`;
        const displayTime = lastScanTime.toLocaleString("en-US", { timeZone: "America/New_York", hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
        document.getElementById('last-run-time').innerText = displayTime;
        document.getElementById('ticker-display').innerText = `(${timeAgo})`;
    }

    if (nextRunTime) {
        const diff = Math.floor((nextRunTime - now) / 1000);
        if (diff > 0) {
            let timeLeft = diff < 60 ? `${diff}s` : diff < 3600 ? `${Math.floor(diff / 60)}m ${diff % 60}s` : `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
            document.getElementById('bot-message').innerText = `Sleeping. Next scan in: ${timeLeft}`;
        } else {
            document.getElementById('bot-message').innerText = `Waking up for next scan...`;
        }
    } else if (lastHeartbeatTime) {
        const diff = Math.floor((now - lastHeartbeatTime) / 1000);
        const prevMsg = document.getElementById('bot-message').dataset.rawMessage || "System confirmed active";
        document.getElementById('bot-message').innerText = `${prevMsg} (${diff}s ago)`;
    }
}

async function loadPerformance() {
    try {
        const response = await fetch('/api/performance');
        const data = await response.json();
        
        if (data.bot_status) {
            document.getElementById('bot-status').innerText = data.bot_status.status || "Idle";
            if (data.bot_status.last_ping) {
                lastHeartbeatTime = new Date(data.bot_status.last_ping);
            }
            
            let rawMsg = data.bot_status.message || "No recent activity";
            if (data.bot_status.status === "Sleeping" && rawMsg.includes("|")) {
                const parts = rawMsg.split("|");
                rawMsg = parts[0];
                nextRunTime = new Date(parts[1]);
            } else {
                nextRunTime = null;
            }
            document.getElementById('bot-message').dataset.rawMessage = rawMsg;
            document.getElementById('bot-message').innerText = rawMsg;
        }
        
        if (data.latest_run && data.latest_run.at) {
            lastScanTime = new Date(data.latest_run.at);
            let sourceLabel = "Automatic Bot";
            if (data.latest_run.trigger === "FORCE_EXEC") sourceLabel = "Manual Override";
            if (data.latest_run.trigger === "MANUAL") sourceLabel = "Dashboard Scan";
            document.getElementById('last-run-id').innerText = `${sourceLabel} (Market NY)`;
        }

        const summary = data.summary;
        const plColor = summary.daily_pl >= 0 ? "#3fb950" : "#f85149";
        const unrealColor = summary.unrealized_pl >= 0 ? "#3fb950" : "#f85149";

        document.getElementById("perf-metrics").innerHTML = `
            <div class="card"><h3>Total Equity</h3><div class="value">$${summary.equity.toLocaleString(undefined, {minimumFractionDigits: 2})}</div><div class="sub-value">Cash: $${summary.cash.toLocaleString(undefined, {minimumFractionDigits: 2})}</div></div>
            <div class="card"><h3>Daily P/L</h3><div class="value" style="color: ${plColor}">${summary.daily_pl >= 0 ? "+" : ""}$${summary.daily_pl.toFixed(2)}</div><div class="sub-value">${summary.daily_pl_pct.toFixed(2)}% today</div></div>
            <div class="card"><h3>Unrealized P/L</h3><div class="value" style="color: ${unrealColor}">${summary.unrealized_pl >= 0 ? "+" : ""}$${summary.unrealized_pl.toFixed(2)}</div><div class="sub-value">${summary.unrealized_pl >= 0 ? "+" : ""}${summary.unrealized_pl_pct.toFixed(2)}% total return</div></div>
            <div class="card"><h3>Backtest Accuracy</h3><div class="value">${data.backtest.accuracy ? (data.backtest.accuracy * 100).toFixed(1) + "%" : "N/A"}</div><div class="sub-value">Based on ${data.backtest.total || 0} predictions</div></div>
        `;

        let posHtml = "";
        if (data.positions && data.positions.length > 0) {
            data.positions.forEach(p => {
                const color = p.unrealized_pl >= 0 ? "bullish" : "bearish";
                posHtml += `<div class="list-item"><strong>${p.symbol}</strong><span>${p.qty.toFixed(2)} @ $${p.avg_entry_price.toFixed(2)}</span><span class="${color}">${p.unrealized_pl >= 0 ? "+" : ""}${p.unrealized_plpc.toFixed(2)}%</span></div>`;
            });
        } else { posHtml = "<div class='list-item' style='color:#8b949e'>No active positions.</div>"; }
        document.getElementById("positions-list").innerHTML = posHtml;

        document.getElementById('perf-spinner').style.display = "none";
        document.getElementById('initial-load-msg').style.display = "none";
        document.getElementById('perf-metrics').style.display = "grid";
        document.getElementById('perf-positions').style.display = "block";
        updateTickers();
        startTicker();
    } catch (e) { remoteLog(`Error: ${e.message}`, "ERROR"); }
}

async function runScreener() {
    const btn = document.getElementById('run-btn');
    const spinner = document.getElementById('manual-spinner');
    btn.style.display = 'none';
    spinner.style.display = 'block';
    document.getElementById('result').innerHTML = '<p style="color: #8b949e; text-align: center;">Initializing Decision Engine...</p>';
    try {
        const response = await fetch('/api/screen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        document.getElementById('result').innerHTML = await response.text();
    } catch (e) { document.getElementById('result').innerHTML = `<p style="color: #f85149;">Error: ${e.message}</p>`; } 
    finally { btn.style.display = 'block'; spinner.style.display = 'none'; }
}

async function forceTrade() {
    if (!confirm("Are you sure? This will place real paper trades using autonomous logic.")) return;
    document.getElementById('last-run-time').innerText = "SCANNING NOW...";
    document.getElementById('ticker-display').innerText = "";
    document.getElementById('last-run-id').innerText = "Brain is working...";
    const btn = document.getElementById('force-btn');
    const spinner = document.getElementById('force-spinner');
    btn.style.display = 'none';
    spinner.style.display = 'block';
    document.getElementById('result').innerHTML = '<p style="color: #ff7b72; text-align: center;">Executing Full AI Cycle...</p>';
    try {
        const response = await fetch('/api/force-trade', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        document.getElementById('result').innerHTML = await response.text();
        await loadPerformance();
    } catch (e) { document.getElementById('result').innerHTML = `<p style="color: #f85149;">Error: ${e.message}</p>`; } 
    finally { btn.style.display = 'block'; spinner.style.display = 'none'; }
}

const resizer = document.getElementById('resizer');
const sidebar = document.getElementById('sidebar');
let isResizing = false;
resizer.addEventListener('mousedown', (e) => { isResizing = true; document.body.style.cursor = 'ew-resize'; resizer.classList.add('active'); e.preventDefault(); });
document.addEventListener('mousemove', (e) => { if (!isResizing) return; let newWidth = e.clientX; if (newWidth < 150) newWidth = 150; if (newWidth > 800) newWidth = 800; sidebar.style.width = newWidth + 'px'; });
document.addEventListener('mouseup', () => { if (isResizing) { isResizing = false; document.body.style.cursor = ''; resizer.classList.remove('active'); } });

window.onload = () => { if (document.getElementById('performance-tab').classList.contains('active')) { loadPerformance(); } };
