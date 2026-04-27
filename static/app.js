let lastScanTime = null;
let lastHeartbeatTime = null;
let nextRunTime = null;
let tickerInterval = null;

async function remoteLog(message, level = "INFO") {
  try {
    await fetch("/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, message }),
    });
  } catch (e) {}
}

window.onerror = (m, s, l, c, e) => {
  remoteLog(`JS Error: ${m} at ${s}:${l}:${c}`, "ERROR");
  return false;
};

function switchTab(tabId) {
  document
    .querySelectorAll(".tab-btn")
    .forEach((btn) => btn.classList.remove("active"));
  document
    .querySelectorAll(".tab-content")
    .forEach((content) => content.classList.remove("active"));
  if (event && event.target) {
    event.target.classList.add("active");
  }
  document.getElementById(tabId + "-tab").classList.add("active");
  if (tabId === "performance") {
    loadPerformance();
  } else if (tabId === "history") {
    loadHistory();
  }
}

async function loadHistory() {
  const tableBody = document.getElementById("history-table-body");
  const spinner = document.getElementById("history-spinner");
  spinner.style.display = "block";
  tableBody.innerHTML = "";
  try {
    const response = await fetch("/api/history");
    const data = await response.json();
    if (data.history && data.history.length > 0) {
      data.history.forEach((trade) => {
        const plColor = trade.pl_dollars >= 0 ? "bullish" : "bearish";
        const plPrefix = trade.pl_dollars >= 0 ? "+" : "";
        tableBody.innerHTML += `<tr><td><strong>${trade.symbol}</strong></td><td>$${trade.entry_price.toFixed(2)}</td><td>$${trade.exit_price.toFixed(2)}</td><td>${trade.qty.toFixed(2)}</td><td class="${plColor}">${plPrefix}$${trade.pl_dollars.toFixed(2)}</td><td class="${plColor}">${plPrefix}${trade.pl_pct.toFixed(2)}%</td><td>${trade.exit_time}</td><td><span class="badge ${trade.status.toLowerCase()}">${trade.status}</span></td></tr>`;
      });
    } else {
      tableBody.innerHTML =
        '<tr><td colspan="8" style="text-align:center; color:#8b949e">No matched trade pairs found.</td></tr>';
    }
  } catch (e) {
    remoteLog("Error loading history: " + e.message, "ERROR");
  } finally {
    spinner.style.display = "none";
  }
}

function startTicker() {
  if (tickerInterval) clearInterval(tickerInterval);
  tickerInterval = setInterval(updateTickers, 1000);
}

function updateTickers() {
  const now = new Date();

  if (lastScanTime) {
    const diff = Math.floor((now - lastScanTime) / 1000);
    let timeAgo =
      diff < 60
        ? `${diff}s ago`
        : diff < 3600
          ? `${Math.floor(diff / 60)}m ago`
          : `${Math.floor(diff / 3600)}h ago`;
    const displayTime = lastScanTime.toLocaleString("en-US", {
      timeZone: "America/New_York",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      hour12: true,
    });
    document.getElementById("last-run-time").innerText = displayTime;
    document.getElementById("ticker-display").innerText = `(${timeAgo})`;
  }

  if (nextRunTime) {
    const diff = Math.floor((nextRunTime - now) / 1000);
    if (diff > 0) {
      let timeLeft =
        diff < 60
          ? `${diff}s`
          : diff < 3600
            ? `${Math.floor(diff / 60)}m ${diff % 60}s`
            : `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
      document.getElementById("bot-message").innerText =
        `Sleeping. Next scan in: ${timeLeft}`;
    } else {
      document.getElementById("bot-message").innerText =
        `Waking up for next scan...`;
    }
  } else if (lastHeartbeatTime) {
    const diff = Math.floor((now - lastHeartbeatTime) / 1000);
    const prevMsg =
      document.getElementById("bot-message").dataset.rawMessage ||
      "System confirmed active";
    document.getElementById("bot-message").innerText =
      `${prevMsg} (${diff}s ago)`;
  }
}

async function loadPerformance() {
  try {
    const response = await fetch("/api/performance");
    const data = await response.json();

    if (data.bot_status) {
      document.getElementById("bot-status").innerText =
        data.bot_status.status || "Idle";
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
      document.getElementById("bot-message").dataset.rawMessage = rawMsg;
      document.getElementById("bot-message").innerText = rawMsg;
    }

    if (data.latest_run && data.latest_run.at) {
      lastScanTime = new Date(data.latest_run.at);
      let sourceLabel = "Automatic Bot";
      if (data.latest_run.trigger === "FORCE_EXEC")
        sourceLabel = "Manual Override";
      if (data.latest_run.trigger === "MANUAL") sourceLabel = "Dashboard Scan";
      document.getElementById("last-run-id").innerText =
        `${sourceLabel} (Market NY)`;
    }

    const summary = data.summary;
    const plColor = summary.daily_pl >= 0 ? "#3fb950" : "#f85149";
    const unrealColor = summary.unrealized_pl >= 0 ? "#3fb950" : "#f85149";

    document.getElementById("perf-metrics").innerHTML = `
            <div class="card"><h3>Total Equity</h3><div class="value">$${summary.equity.toLocaleString(undefined, { minimumFractionDigits: 2 })}</div><div class="sub-value">Cash: $${summary.cash.toLocaleString(undefined, { minimumFractionDigits: 2 })}</div></div>
            <div class="card"><h3>Daily P/L</h3><div class="value" style="color: ${plColor}">${summary.daily_pl >= 0 ? "+" : ""}$${summary.daily_pl.toFixed(2)}</div><div class="sub-value">${summary.daily_pl_pct.toFixed(2)}% today</div></div>
            <div class="card"><h3>Unrealized P/L</h3><div class="value" style="color: ${unrealColor}">${summary.unrealized_pl >= 0 ? "+" : ""}$${summary.unrealized_pl.toFixed(2)}</div><div class="sub-value">${summary.unrealized_pl >= 0 ? "+" : ""}${summary.unrealized_pl_pct.toFixed(2)}% total return</div></div>
            <div class="card"><h3>Backtest Accuracy</h3><div class="value">${data.backtest.accuracy ? (data.backtest.accuracy * 100).toFixed(1) + "%" : "N/A"}</div><div class="sub-value">Based on ${data.backtest.total || 0} predictions</div></div>
        `;

    let posHtml = "";
    if (data.positions && data.positions.length > 0) {
      data.positions.forEach((p) => {
        const color = p.unrealized_pl >= 0 ? "bullish" : "bearish";
        posHtml += `<div class="list-item"><strong>${p.symbol}</strong><span>${p.qty.toFixed(2)} @ $${p.avg_entry_price.toFixed(2)}</span><span class="${color}">${p.unrealized_pl >= 0 ? "+" : ""}${p.unrealized_plpc.toFixed(2)}%</span></div>`;
      });
    } else {
      posHtml =
        "<div class='list-item' style='color:#8b949e'>No active positions.</div>";
    }
    document.getElementById("positions-list").innerHTML = posHtml;

    document.getElementById("perf-spinner").style.display = "none";
    document.getElementById("initial-load-msg").style.display = "none";
    document.getElementById("perf-metrics").style.display = "grid";
    document.getElementById("perf-positions").style.display = "block";
    updateTickers();
    startTicker();
    loadLastExecution();
  } catch (e) {
    remoteLog(`Error: ${e.message}`, "ERROR");
  }
}

async function loadLastExecution() {
  try {
    const res = await fetch("/api/last-execution");
    const d = await res.json();
    const section = document.getElementById("last-execution-section");
    if (!d || !d.timestamp) { section.style.display = "none"; return; }
    section.style.display = "block";

    const ts = new Date(d.timestamp).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true });
    const triggerLabel = { "FORCE_EXEC": "Manual Override", "BOT SCAN": "Scheduled Bot", "MANUAL": "Screen Only" }[d.trigger] || d.trigger;
    document.getElementById("exec-meta").textContent = `${ts} NY · ${triggerLabel}`;
    document.getElementById("exec-regime").textContent = d.regime || "";

    const hasBuys = d.bought && d.bought.length > 0;
    const hasSells = d.sold && d.sold.length > 0;
    const hasSwaps = d.swapped && d.swapped.length > 0;
    document.getElementById("exec-empty").style.display = (!hasBuys && !hasSells && !hasSwaps) ? "block" : "none";

    // Bought
    let bHtml = "";
    if (hasBuys) {
      bHtml = `<div style="margin-bottom:1.25rem;">
        <div style="font-size:12px; font-weight:600; color:#3fb950; text-transform:uppercase; letter-spacing:.05em; margin-bottom:.5rem;">▲ Bought</div>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
          <thead><tr style="color:#8b949e; border-bottom:1px solid var(--border-color);">
            <th style="text-align:left;padding:4px 8px">Symbol</th><th style="text-align:left;padding:4px 8px">Archetype</th>
            <th style="text-align:right;padding:4px 8px">Score</th><th style="text-align:right;padding:4px 8px">Qty</th>
            <th style="text-align:right;padding:4px 8px">Price</th><th style="text-align:right;padding:4px 8px">Cost</th>
            <th style="text-align:left;padding:4px 8px">AI Reason</th>
          </tr></thead><tbody>`;
      d.bought.forEach(b => {
        const reason = (b.reasons || []).find(r => r.length > 20 && !r.startsWith("Archetype") && !r.startsWith("RVOL") && !r.startsWith("Sent:") && !r.startsWith("Earnings")) || (b.reasons || [])[0] || "—";
        bHtml += `<tr style="border-bottom:1px solid #21262d;">
          <td style="padding:6px 8px;font-weight:700;color:#e6edf3">${b.symbol}</td>
          <td style="padding:6px 8px;color:#8b949e">${b.archetype || "—"}</td>
          <td style="padding:6px 8px;text-align:right;color:#3fb950">${b.score}</td>
          <td style="padding:6px 8px;text-align:right">${b.qty}</td>
          <td style="padding:6px 8px;text-align:right">$${(b.price||0).toFixed(2)}</td>
          <td style="padding:6px 8px;text-align:right">$${(b.cost||0).toFixed(2)}</td>
          <td style="padding:6px 8px;color:#8b949e;font-size:12px">${reason}</td>
        </tr>`;
      });
      bHtml += "</tbody></table></div>";
    }
    document.getElementById("exec-bought").innerHTML = bHtml;

    // Sold
    let sHtml = "";
    if (hasSells) {
      sHtml = `<div style="margin-bottom:1.25rem;">
        <div style="font-size:12px; font-weight:600; color:#f85149; text-transform:uppercase; letter-spacing:.05em; margin-bottom:.5rem;">▼ Sold / Exited</div>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
          <thead><tr style="color:#8b949e; border-bottom:1px solid var(--border-color);">
            <th style="text-align:left;padding:4px 8px">Symbol</th>
            <th style="text-align:left;padding:4px 8px">Reason</th>
            <th style="text-align:left;padding:4px 8px">Detail</th>
          </tr></thead><tbody>`;
      d.sold.forEach(s => {
        sHtml += `<tr style="border-bottom:1px solid #21262d;">
          <td style="padding:6px 8px;font-weight:700;color:#e6edf3">${s.symbol}</td>
          <td style="padding:6px 8px;color:#f85149">${s.reason}</td>
          <td style="padding:6px 8px;color:#8b949e;font-size:12px">${s.detail || "—"}</td>
        </tr>`;
      });
      sHtml += "</tbody></table></div>";
    }
    document.getElementById("exec-sold").innerHTML = sHtml;

    // Swapped
    let swHtml = "";
    if (hasSwaps) {
      swHtml = `<div style="margin-bottom:1.25rem;">
        <div style="font-size:12px; font-weight:600; color:#d29922; text-transform:uppercase; letter-spacing:.05em; margin-bottom:.5rem;">⇄ Swapped</div>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
          <thead><tr style="color:#8b949e; border-bottom:1px solid var(--border-color);">
            <th style="text-align:left;padding:4px 8px">Out</th><th style="text-align:right;padding:4px 8px">Score</th>
            <th style="text-align:left;padding:4px 8px">In</th><th style="text-align:right;padding:4px 8px">Score</th>
            <th style="text-align:left;padding:4px 8px">Why</th><th style="text-align:left;padding:4px 8px">AI Reason</th>
          </tr></thead><tbody>`;
      d.swapped.forEach(sw => {
        const reason = (sw.in_reasons || []).find(r => r.length > 20 && !r.startsWith("Archetype") && !r.startsWith("RVOL") && !r.startsWith("Sent:") && !r.startsWith("Earnings")) || "—";
        swHtml += `<tr style="border-bottom:1px solid #21262d;">
          <td style="padding:6px 8px;color:#f85149;font-weight:700">${sw.out}</td>
          <td style="padding:6px 8px;text-align:right;color:#8b949e">${sw.out_score}</td>
          <td style="padding:6px 8px;color:#3fb950;font-weight:700">${sw.in}</td>
          <td style="padding:6px 8px;text-align:right;color:#3fb950">${sw.in_score}</td>
          <td style="padding:6px 8px;color:#8b949e">${sw.reason || "—"}</td>
          <td style="padding:6px 8px;color:#8b949e;font-size:12px">${reason}</td>
        </tr>`;
      });
      swHtml += "</tbody></table></div>";
    }
    document.getElementById("exec-swapped").innerHTML = swHtml;
  } catch (e) {
    console.error("Failed to load last execution:", e);
  }
}

async function runScreener() {
  const btn = document.getElementById("run-btn");
  const spinner = document.getElementById("manual-spinner");
  btn.style.display = "none";
  spinner.style.display = "block";
  document.getElementById("screener-result").innerHTML =
    '<p style="color: #8b949e; text-align: center;">Initializing Decision Engine...</p>';
  try {
    const response = await fetch("/api/screen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    document.getElementById("screener-result").innerHTML =
      await response.text();
  } catch (e) {
    document.getElementById("screener-result").innerHTML =
      `<p style="color: #f85149;">Error: ${e.message}</p>`;
  } finally {
    btn.style.display = "block";
    spinner.style.display = "none";
  }
}

async function forceTrade() {
  if (
    !confirm(
      "Are you sure? This will place real paper trades using autonomous logic.",
    )
  )
    return;
  document.getElementById("last-run-time").innerText = "SCANNING NOW...";
  document.getElementById("ticker-display").innerText = "";
  document.getElementById("last-run-id").innerText = "Brain is working...";
  const btn = document.getElementById("force-btn");
  const spinner = document.getElementById("force-spinner");
  btn.style.display = "none";
  spinner.style.display = "block";
  document.getElementById("trade-result").innerHTML =
    '<p style="color: #ff7b72; text-align: center;">Executing Full AI Cycle...</p>';
  try {
    const response = await fetch("/api/force-trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    document.getElementById("trade-result").innerHTML = await response.text();
    await loadPerformance();
    await loadLastExecution();
  } catch (e) {
    document.getElementById("trade-result").innerHTML =
      `<p style="color: #f85149;">Error: ${e.message}</p>`;
  } finally {
    btn.style.display = "block";
    spinner.style.display = "none";
  }
}

const resizer = document.getElementById("resizer");
const sidebar = document.getElementById("sidebar");
let isResizing = false;
resizer.addEventListener("mousedown", (e) => {
  isResizing = true;
  document.body.style.cursor = "ew-resize";
  resizer.classList.add("active");
  e.preventDefault();
});
document.addEventListener("mousemove", (e) => {
  if (!isResizing) return;
  let newWidth = e.clientX;
  if (newWidth < 150) newWidth = 150;
  if (newWidth > 800) newWidth = 800;
  sidebar.style.width = newWidth + "px";
});
document.addEventListener("mouseup", () => {
  if (isResizing) {
    isResizing = false;
    document.body.style.cursor = "";
    resizer.classList.remove("active");
  }
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    const newsSel = document.getElementById("news-provider-select");
    if (newsSel) newsSel.value = data.news_provider || "rss";
    updateProviderUI(data.news_provider || "rss");
    const modeSel = document.getElementById("trading-mode-select");
    if (modeSel) {
      modeSel.value = data.alpaca_paper === false ? "live" : "paper";
      updateTradingModeUI(modeSel.value, data.alpaca_live_key_hint || "");
    }
  } catch (e) {
    console.error("Failed to load settings:", e);
  }
}

function updateProviderUI(provider) {
  const a = document.getElementById("alpaca-info-section");
  if (a) a.style.display = provider === "alpaca" ? "block" : "none";
}

function updateTradingModeUI(mode, liveKeyHint) {
  const isLive = mode === "live";
  const w = document.getElementById("live-trading-warning");
  if (w) w.style.display = isLive ? "block" : "none";
  const k = document.getElementById("live-keys-section");
  if (k) k.style.display = isLive ? "block" : "none";
  if (isLive && liveKeyHint) {
    const apiInput = document.getElementById("live-api-key-input");
    if (apiInput && !apiInput.value) apiInput.placeholder = `Currently: ${liveKeyHint}`;
  }
}

async function saveSettings() {
  const provider = document.getElementById("news-provider-select").value;
  const modeSel = document.getElementById("trading-mode-select");
  const alpacaPaper = modeSel ? modeSel.value !== "live" : true;
  const statusEl = document.getElementById("settings-save-status");
  const btn = document.getElementById("settings-save-btn");

  const payload = { news_provider: provider, alpaca_paper: alpacaPaper };

  if (!alpacaPaper) {
    const apiKey = document.getElementById("live-api-key-input").value.trim();
    const secretKey = document.getElementById("live-secret-key-input").value.trim();
    if (apiKey) payload.alpaca_live_api_key = apiKey;
    if (secretKey) payload.alpaca_live_secret_key = secretKey;
  }

  btn.disabled = true;
  statusEl.textContent = "Saving…";
  statusEl.style.color = "#8b949e";
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      statusEl.textContent = "✓ Saved — takes effect on next cycle";
      statusEl.style.color = "#3fb950";
      document.getElementById("live-secret-key-input").value = "";
      await loadSettings();
    } else {
      statusEl.textContent = `Error: ${data.error || "unknown"}`;
      statusEl.style.color = "#f85149";
    }
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    statusEl.style.color = "#f85149";
  } finally {
    btn.disabled = false;
  }
}

window.onload = () => {
  if (document.getElementById("performance-tab").classList.contains("active")) {
    loadPerformance();
  }
  const newsSel = document.getElementById("news-provider-select");
  if (newsSel)
    newsSel.addEventListener("change", (e) => updateProviderUI(e.target.value));
  const modeSel = document.getElementById("trading-mode-select");
  if (modeSel)
    modeSel.addEventListener("change", (e) => updateTradingModeUI(e.target.value, ""));
  loadSettings();
};
