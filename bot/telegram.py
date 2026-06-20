"""
Telegram notification utility.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment (.env).
"""
import logging
import os
import requests

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_message(text: str) -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False
