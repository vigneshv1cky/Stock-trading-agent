# ETF Sentiment Analyzer

Stock screener and predictor powered by FinBERT NLP and technical analysis. Finds stocks under $100 with strong 3-month performance, fetches recent news, and predicts movement using sentiment analysis.

**DISCLAIMER: This tool is for educational purposes only. It does NOT constitute financial advice.**

## Installation

```bash
pip install etf-sentiment-analyzer
```

## Usage

```bash
# Run screener once
etf-sentiment

# Auto-run daily with alerts
etf-sentiment --schedule

# Auto-run every 12 hours
etf-sentiment --schedule --every 12

# Check past prediction accuracy
etf-sentiment --backtest

# Show recent alerts
etf-sentiment --alerts
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--max-price` | Maximum stock price | $100 |
| `--min-return` | Minimum 3-month return % | 10% |
| `--top` | Number of top stocks to show | 30 |
| `--schedule` | Auto-run on a schedule | off |
| `--every` | Schedule interval in hours | 24 |
| `--backtest` | Check past prediction accuracy | off |
| `--alerts` | Show recent alerts | off |
| `--cloud` | Save HTML report to S3 + email via SES | off |

## License

MIT
