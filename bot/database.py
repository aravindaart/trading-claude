"""Supabase REST client for persisting trades, P&L, and positions.

All public functions return bool and swallow exceptions — a DB failure
must never crash the bot. Configure via env vars:
  SUPABASE_URL          e.g. https://xyzxyz.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (write access, GitHub secret only)
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

_URL = os.environ.get("SUPABASE_URL", "")
_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _enabled() -> bool:
    return bool(_URL and _KEY)


def _headers() -> dict:
    return {
        "apikey": _KEY,
        "Authorization": f"Bearer {_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def insert_trade(row: dict) -> bool:
    if not _enabled():
        return False
    try:
        r = requests.post(f"{_URL}/rest/v1/trades", json=row, headers=_headers(), timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("DB insert_trade failed: %s", exc)
        return False


def upsert_daily_pnl(row: dict) -> bool:
    if not _enabled():
        return False
    try:
        h = {**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
        r = requests.post(f"{_URL}/rest/v1/daily_pnl", json=row, headers=h, timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("DB upsert_daily_pnl failed: %s", exc)
        return False


def upsert_positions(positions: dict) -> bool:
    """Replace the entire positions snapshot (delete-then-insert).

    Positions is a small table (<10 rows) written exclusively by one bot
    process, so delete-then-insert is safe and keeps the logic simple.
    """
    if not _enabled():
        return False
    try:
        requests.delete(
            f"{_URL}/rest/v1/positions?symbol=not.is.null",
            headers=_headers(),
            timeout=10,
        ).raise_for_status()
        if positions:
            rows = [{"symbol": sym, **pos} for sym, pos in positions.items()]
            requests.post(
                f"{_URL}/rest/v1/positions",
                json=rows,
                headers=_headers(),
                timeout=10,
            ).raise_for_status()
        return True
    except Exception as exc:
        logger.warning("DB upsert_positions failed: %s", exc)
        return False


def insert_event(level: str, message: str) -> bool:
    if not _enabled():
        return False
    try:
        r = requests.post(
            f"{_URL}/rest/v1/bot_events",
            json={"level": level, "message": message},
            headers=_headers(),
            timeout=5,
        )
        r.raise_for_status()
        return True
    except Exception:
        return False


class SupabaseLogHandler(logging.Handler):
    """Forwards WARNING+ log records to the Supabase bot_events table."""

    def emit(self, record: logging.LogRecord):
        try:
            insert_event(record.levelname, self.format(record))
        except Exception:
            pass
