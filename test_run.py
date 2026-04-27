from stock_sentiment.screener_app import ScreenerApp
app = ScreenerApp(top_n=40)
app.run(trigger="FORCE_EXEC")
