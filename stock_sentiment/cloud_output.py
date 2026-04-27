"""Generates an HTML report from screener predictions."""

from datetime import datetime, timezone


def generate_html_report(predictions: list, screened_count: int, fragment: bool = False) -> str:
    """Generate a styled HTML report from predictions."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    bullish = [p for p in predictions if p.prediction == "BULLISH"]
    neutral = [p for p in predictions if p.prediction == "NEUTRAL"]
    bearish = [p for p in predictions if p.prediction == "BEARISH"]

    def color(pred):
        if pred == "BULLISH":
            return "#22c55e"
        elif pred == "BEARISH":
            return "#ef4444"
        return "#eab308"

    def pct_color(val):
        return "#22c55e" if val >= 0 else "#ef4444"

    def pct(val):
        c = pct_color(val)
        return f'<span style="color:{c}">{val:+.1f}%</span>'

    rows = ""
    for i, p in enumerate(predictions, 1):
        pred_c = color(p.prediction)
        score_c = "#22c55e" if p.overall_score >= 65 else "#ef4444" if p.overall_score < 40 else "#eab308"
        reasoning = " | ".join(p.reasoning[:3]) if p.reasoning else ""

        rows += f"""
        <tr style="border-bottom:1px solid #333">
            <td style="padding:8px">{i}</td>
            <td style="padding:8px;font-weight:bold">{p.symbol}</td>
            <td style="padding:8px;text-align:right">${p.current_price:.2f}</td>
            <td style="padding:8px;text-align:right">{pct(p.change_3m_pct)}</td>
            <td style="padding:8px;text-align:right">{pct(p.change_1m_pct)}</td>
            <td style="padding:8px;text-align:right">{pct(p.change_1w_pct)}</td>
            <td style="padding:8px;text-align:center;color:{pred_c};font-weight:bold">{p.prediction}</td>
            <td style="padding:8px;text-align:center">{p.confidence:.0f}%</td>
            <td style="padding:8px;text-align:center;color:{score_c};font-weight:bold">{p.overall_score:.0f}</td>
            <td style="padding:8px;text-align:center">{f'{p.rsi:.0f}' if p.rsi else '--'}</td>
            <td style="padding:8px;font-size:12px;color:#999">{reasoning[:60]}</td>
        </tr>"""

    top_detail = ""
    for p in bullish[:8]:
        headlines_html = ""
        for title, score, source, url in p.top_headlines[:3]:
            s_color = "#22c55e" if score > 0.1 else "#ef4444" if score < -0.1 else "#eab308"
            headlines_html += f'<li style="margin:2px 0"><span style="color:{s_color}">{score:+.2f}</span> <a href="{url}" style="color:#e6edf3;text-decoration:none" target="_blank">{title[:80]}</a> <span style="color:#666">— {source}</span></li>'

        top_detail += f"""
        <div style="background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:16px;margin:8px 0">
            <div style="font-size:18px;font-weight:bold">{p.symbol}
                <span style="color:{color(p.prediction)};margin-left:8px">{p.prediction}</span>
                <span style="color:#999;font-size:14px;margin-left:8px">{p.confidence:.0f}% conf</span>
                <span style="color:#999;font-size:14px;margin-left:8px">${p.current_price:.2f}</span>
            </div>
            <div style="color:#aaa;margin:8px 0">
                3M: {pct(p.change_3m_pct)} &nbsp;|&nbsp;
                1M: {pct(p.change_1m_pct)} &nbsp;|&nbsp;
                1W: {pct(p.change_1w_pct)} &nbsp;|&nbsp;
                Range: ${p.low_3m:.2f}-${p.high_3m:.2f}
            </div>
            <div style="color:#eab308;margin:4px 0">Predicted: {p.predicted_move}</div>
            <ul style="color:#aaa;margin:4px 0;padding-left:20px">
                {"".join(f'<li>{r}</li>' for r in p.reasoning)}
            </ul>
            {f'<div style="margin-top:8px;font-size:13px"><b>Headlines:</b><ul style="padding-left:16px">{headlines_html}</ul></div>' if headlines_html else ''}
        </div>"""

    content_html = f"""
<div style="text-align:center;padding:20px;background:#161b22;border-radius:12px;margin-bottom:20px">
    <h1 style="margin:0;color:#58a6ff">📊 Stock Screener Report</h1>
    <p style="color:#8b949e;margin:8px 0">Generated: {now}</p>
    <p style="color:#8b949e">
        Screened: {screened_count} stocks &nbsp;|&nbsp;
        Bullish: <span style="color:#22c55e">{len(bullish)}</span> &nbsp;|&nbsp;
        Neutral: <span style="color:#eab308">{len(neutral)}</span> &nbsp;|&nbsp;
        Bearish: <span style="color:#ef4444">{len(bearish)}</span>
    </p>
</div>

<h2 style="color:#58a6ff;border-bottom:1px solid #333;padding-bottom:8px">All Predictions</h2>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:14px">
    <thead>
        <tr style="background:#161b22;color:#8b949e;text-align:left">
            <th style="padding:10px">#</th>
            <th style="padding:10px">Symbol</th>
            <th style="padding:10px;text-align:right">Price</th>
            <th style="padding:10px;text-align:right">3M%</th>
            <th style="padding:10px;text-align:right">1M%</th>
            <th style="padding:10px;text-align:right">1W%</th>
            <th style="padding:10px;text-align:center">Prediction</th>
            <th style="padding:10px;text-align:center">Conf</th>
            <th style="padding:10px;text-align:center">Score</th>
            <th style="padding:10px;text-align:center">RSI</th>
            <th style="padding:10px">Reasoning</th>
        </tr>
    </thead>
    <tbody>{rows}</tbody>
</table>
</div>

<h2 style="color:#22c55e;border-bottom:1px solid #333;padding-bottom:8px;margin-top:30px">Top Bullish Picks</h2>
{top_detail if top_detail else '<p style="color:#666">No bullish predictions this run.</p>'}

<div style="margin-top:30px;padding:16px;background:#2d1b1b;border:1px solid #5a2d2d;border-radius:8px;text-align:center">
    <p style="color:#ef4444;font-weight:bold;margin:0">⚠ NOT FINANCIAL ADVICE</p>
    <p style="color:#999;font-size:13px;margin:8px 0 0 0">
        This report is for educational purposes only. Past performance does not guarantee future results.
        Always do your own research before investing.
    </p>
</div>
"""

    if fragment:
        return content_html

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Stock Screener Report - {now}</title></head>
<body style="background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;max-width:1200px;margin:0 auto;padding:20px">
{content_html}
</body>
</html>"""

    return html
