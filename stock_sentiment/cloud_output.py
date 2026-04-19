"""Cloud output: generates HTML report, uploads to S3, sends email via SES.

Environment variables:
    S3_BUCKET       - S3 bucket name for reports
    SES_FROM_EMAIL  - Verified SES sender email
    SES_TO_EMAIL    - Recipient email
    AWS_REGION      - AWS region (default: us-east-1)
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional


def generate_html_report(predictions: list, screened_count: int) -> str:
    """Generate a styled HTML email report from predictions."""
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

    # Build stock rows
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

    # Top picks detail
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

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Stock Screener Report - {now}</title></head>
<body style="background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;max-width:1200px;margin:0 auto;padding:20px">

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

</body>
</html>"""

    return html


def upload_to_s3(html: str, bucket: str, region: str = "us-east-1") -> Optional[str]:
    """Upload HTML report to S3. Returns the S3 URL."""
    try:
        import boto3

        s3 = boto3.client("s3", region_name=region)
        now = datetime.now(timezone.utc)
        key = f"reports/{now.strftime('%Y/%m/%d')}/screener-{now.strftime('%Y%m%d-%H%M%S')}.html"

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html",
        )

        url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        return url

    except Exception as e:
        print(f"S3 upload failed: {e}")
        return None


def send_email(
    subject: str,
    html_body: str,
    from_email: str,
    to_email: str,
    region: str = "us-east-1",
    s3_url: str = None,
):
    """Send email via AWS SES."""
    try:
        import boto3

        ses = boto3.client("ses", region_name=region)

        # Add S3 link to email if available
        if s3_url:
            link_section = f"""
            <div style="text-align:center;margin:20px 0">
                <a href="{s3_url}" style="background:#58a6ff;color:#fff;padding:12px 24px;
                   text-decoration:none;border-radius:6px;font-weight:bold">
                    View Full Report
                </a>
            </div>"""
            html_body = html_body.replace("</body>", f"{link_section}</body>")

        ses.send_email(
            Source=from_email,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
        return True

    except Exception as e:
        print(f"Email send failed: {e}")
        return False


def run_cloud_mode(predictions: list, screened_count: int, alerts: list = None):
    """Full cloud output: generate report, upload to S3, send email."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    bucket = os.environ.get("S3_BUCKET")
    from_email = os.environ.get("SES_FROM_EMAIL")
    to_email = os.environ.get("SES_TO_EMAIL")

    now = datetime.now(timezone.utc)
    print(f"[Cloud] Generating report at {now.isoformat()}")

    # Generate HTML
    html = generate_html_report(predictions, screened_count)

    # Save locally as backup
    local_path = f"/tmp/screener-{now.strftime('%Y%m%d-%H%M%S')}.html"
    with open(local_path, "w") as f:
        f.write(html)
    print(f"[Cloud] Report saved locally: {local_path}")

    # Upload to S3
    s3_url = None
    if bucket:
        s3_url = upload_to_s3(html, bucket, region)
        if s3_url:
            print(f"[Cloud] Uploaded to S3: {s3_url}")
    else:
        print("[Cloud] S3_BUCKET not set, skipping S3 upload")

    # Send email
    if from_email and to_email:
        bullish_count = sum(1 for p in predictions if p.prediction == "BULLISH")
        subject = f"📊 Stock Screener: {bullish_count} BULLISH, {screened_count} screened — {now.strftime('%Y-%m-%d')}"

        if alerts:
            alert_symbols = ", ".join(set(a["symbol"] for a in alerts[:5]))
            subject += f" | Alerts: {alert_symbols}"

        sent = send_email(subject, html, from_email, to_email, region, s3_url)
        if sent:
            print(f"[Cloud] Email sent to {to_email}")
    else:
        print("[Cloud] SES_FROM_EMAIL / SES_TO_EMAIL not set, skipping email")

    # Save results as JSON too
    results_json = {
        "run_at": now.isoformat(),
        "screened_count": screened_count,
        "predictions": [
            {
                "symbol": p.symbol,
                "price": p.current_price,
                "prediction": p.prediction,
                "confidence": p.confidence,
                "overall_score": p.overall_score,
                "change_3m_pct": p.change_3m_pct,
                "change_1m_pct": p.change_1m_pct,
                "change_1w_pct": p.change_1w_pct,
                "predicted_move": p.predicted_move,
            }
            for p in predictions
        ],
        "alerts": alerts or [],
    }

    json_path = f"/tmp/screener-{now.strftime('%Y%m%d-%H%M%S')}.json"
    with open(json_path, "w") as f:
        json.dump(results_json, f, indent=2)

    if bucket:
        try:
            import boto3
            s3 = boto3.client("s3", region_name=region)
            json_key = f"reports/{now.strftime('%Y/%m/%d')}/screener-{now.strftime('%Y%m%d-%H%M%S')}.json"
            s3.put_object(
                Bucket=bucket, Key=json_key,
                Body=json.dumps(results_json, indent=2).encode(),
                ContentType="application/json",
            )
            print(f"[Cloud] JSON uploaded to S3: {json_key}")
        except Exception:
            pass

    print("[Cloud] Done!")
