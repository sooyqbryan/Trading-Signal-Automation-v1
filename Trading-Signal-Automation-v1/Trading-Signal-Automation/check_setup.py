"""One-off setup check — confirm your keys work before running the service.

    python check_setup.py

Sends a test Telegram message and fetches the latest BTC candles from taapi, so you can see
both integrations working before starting the real loop.
"""
import time

import requests

import config


def check_telegram():
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("Telegram : MISSING token or chat id in .env")
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": config.TELEGRAM_CHAT_ID,
                                 "text": "✅ Setup check — Telegram delivery works!"}, timeout=10)
    ok = r.ok and r.json().get("ok")
    print("Telegram :", "OK — check your chat" if ok else f"FAILED — {r.text[:120]}")


def check_taapi():
    if not config.TAAPI_SECRET:
        print("taapi    : MISSING TAAPI_SECRET in .env")
        return
    inst = config.INSTRUMENTS[0]
    r = requests.get("https://api.taapi.io/candles", params={
        "secret": config.TAAPI_SECRET, "exchange": inst["exchange"],
        "symbol": inst["symbol"], "interval": config.TIMEFRAME, "period": 3}, timeout=15)
    if not r.ok:
        print("taapi    : FAILED —", r.text[:120])
        return
    data = r.json()
    rows = data if isinstance(data, list) else [data]
    print(f"taapi    : OK — latest {inst['name']} closes:", [c.get("close") for c in rows[-3:]])


if __name__ == "__main__":
    check_telegram()
    time.sleep(1)   # respect taapi's ~1-request-per-15s free tier isn't needed here, but be polite
    check_taapi()
