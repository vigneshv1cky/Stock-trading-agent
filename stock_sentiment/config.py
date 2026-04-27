"""Persistent bot settings stored at ~/.stock_screener/settings.json."""

import json
import os

_SETTINGS_FILE = os.path.expanduser("~/.stock_screener/settings.json")

_DEFAULTS: dict = {
    "news_provider": "rss",
    "alpaca_news_enabled": False,
    "alpaca_paper": True,
    "alpaca_live_api_key": "",
    "alpaca_live_secret_key": "",
}


def load_settings() -> dict:
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE) as f:
                data = json.load(f)
            settings = {**_DEFAULTS, **data}
        else:
            settings = dict(_DEFAULTS)
    except Exception:
        settings = dict(_DEFAULTS)
    return settings


def save_settings(updates: dict) -> dict:
    settings = load_settings()
    settings.update(updates)
    os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
    return settings
