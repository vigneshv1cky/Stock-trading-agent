from stock_sentiment.screener_app import ScreenerApp
app = ScreenerApp(top_n=40)
app.run(cloud_mode=False, trigger="FORCE_EXEC")
