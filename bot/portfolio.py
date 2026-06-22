"""
Portfolio manager

Tracks open positions, submits orders via the Alpaca API,
and writes trade records to trades.csv and daily_pnl.csv.
"""
import csv
import json
import logging
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi

from config import DAILY_PNL_CSV, INSTRUMENTS, LOG_DIR, TIMEZONE, TRADES_CSV
from bot import telegram

logger = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

TRADES_HEADERS = [
    "timestamp", "instrument", "direction", "entry_price",
    "exit_price", "pnl", "position_size",
]
DAILY_PNL_HEADERS = ["date", "starting_equity", "ending_equity", "pnl", "pnl_pct"]


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _append_csv(path: str, headers: list[str], row: dict):
    _ensure_log_dir()
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


class Portfolio:
    """
    open_positions structure:
    {
        "SPY": {
            "direction": "long" | "short",
            "entry_price": float,
            "qty": int,
            "hard_stop": float,
            "trailing_stop": float | None,  # used by momentum/trend strategies
            "trailing_distance": float | None,
            "alpaca_order_id": str,
        },
        ...
    }
    """

    def __init__(self, api: tradeapi.REST):
        self._api = api
        self.open_positions: dict[str, dict] = {}
        self._day_start_equity: float | None = None

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def open_position(
        self,
        symbol: str,
        direction: str,
        qty: int | float,
        entry_price: float,
        hard_stop: float,
        trailing_stop_distance: float | None = None,
    ) -> bool:
        """Submit a market order and record the position."""
        if symbol in self.open_positions:
            logger.info("Already have a position in %s, skipping", symbol)
            return False

        side = "buy" if direction == "long" else "sell"
        is_crypto = INSTRUMENTS[symbol]["asset_class"] == "crypto"
        # Crypto endpoint expects "BTC/USD"; equity endpoint expects "SPY"
        alpaca_symbol = symbol

        try:
            order = self._api.submit_order(
                symbol=alpaca_symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="gtc" if INSTRUMENTS[symbol]["asset_class"] == "crypto" else "day",
            )
            logger.info("Order submitted: %s %s %d @ market (id=%s)", side, symbol, qty, order.id)
        except Exception as exc:
            logger.error("Failed to submit order for %s: %s", symbol, exc)
            return False

        initial_trailing_stop = None
        if trailing_stop_distance is not None:
            initial_trailing_stop = (
                entry_price - trailing_stop_distance
                if direction == "long"
                else entry_price + trailing_stop_distance
            )

        self.open_positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "qty": qty,
            "hard_stop": hard_stop,
            "trailing_stop": initial_trailing_stop,
            "trailing_distance": trailing_stop_distance,
            "alpaca_order_id": order.id,
            "opened_at": datetime.now(TZ).isoformat(),
        }

        emoji = "🟢" if direction == "long" else "🔴"
        telegram.send_message(
            f"{emoji} *TRADE OPEN* — {symbol}\n"
            f"Direction: {direction.upper()}\n"
            f"Qty: {qty} @ ${entry_price:.4f}\n"
            f"Hard stop: ${hard_stop:.4f}"
        )
        return True

    def close_position(self, symbol: str, exit_price: float, reason: str = "") -> bool:
        """Close an existing position and log the trade."""
        pos = self.open_positions.get(symbol)
        if pos is None:
            logger.warning("No open position for %s to close", symbol)
            return False

        side = "sell" if pos["direction"] == "long" else "buy"
        is_crypto = INSTRUMENTS[symbol]["asset_class"] == "crypto"
        alpaca_symbol = symbol  # keep as-is; Alpaca crypto endpoint uses "BTC/USD"

        try:
            close_order = self._api.submit_order(
                symbol=alpaca_symbol,
                qty=pos["qty"],
                side=side,
                type="market",
                time_in_force="gtc" if is_crypto else "day",
            )
            logger.info("Close order submitted: %s %s %d (reason: %s)", side, symbol, pos["qty"], reason)
        except Exception as exc:
            logger.error("Failed to close position for %s: %s", symbol, exc)
            return False

        # Poll for fill confirmation (up to 5 s) so we record the actual fill price
        fill_price = exit_price
        deadline = time.time() + 5.0
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                status = self._api.get_order(close_order.id)
                if status.status == "filled":
                    if status.filled_avg_price:
                        fill_price = float(status.filled_avg_price)
                    break
                if status.status in ("canceled", "rejected", "expired"):
                    logger.error("Close order for %s was %s — position NOT removed", symbol, status.status)
                    return False
            except Exception:
                break  # can't check; proceed with estimated price

        if pos["direction"] == "long":
            pnl = (fill_price - pos["entry_price"]) * pos["qty"]
        else:
            pnl = (pos["entry_price"] - fill_price) * pos["qty"]

        _append_csv(TRADES_CSV, TRADES_HEADERS, {
            "timestamp": datetime.now(TZ).isoformat(),
            "instrument": symbol,
            "direction": pos["direction"],
            "entry_price": round(pos["entry_price"], 6),
            "exit_price": round(fill_price, 6),
            "pnl": round(pnl, 2),
            "position_size": pos["qty"],
        })
        logger.info("Closed %s direction=%s pnl=%.2f", symbol, pos["direction"], pnl)

        emoji = "✅" if pnl >= 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        telegram.send_message(
            f"{emoji} *TRADE CLOSED* — {symbol}\n"
            f"Direction: {pos['direction'].upper()}\n"
            f"Entry: ${pos['entry_price']:.4f} → Exit: ${fill_price:.4f}\n"
            f"P&L: *{sign}${pnl:.2f}*\n"
            f"Reason: {reason}"
        )
        del self.open_positions[symbol]
        return True

    # ------------------------------------------------------------------
    # Daily P&L tracking
    # ------------------------------------------------------------------

    def record_day_start(self, equity: float):
        self._day_start_equity = equity
        logger.info("Day start equity: %.2f", equity)

    def record_day_end(self, equity: float):
        if self._day_start_equity is None:
            logger.warning("No day start equity recorded; skipping daily P&L log")
            return

        pnl = equity - self._day_start_equity
        pnl_pct = pnl / self._day_start_equity * 100

        _append_csv(DAILY_PNL_CSV, DAILY_PNL_HEADERS, {
            "date": date.today().isoformat(),
            "starting_equity": round(self._day_start_equity, 2),
            "ending_equity": round(equity, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
        })
        logger.info("Daily P&L: %.2f (%.2f%%)", pnl, pnl_pct)
        self._day_start_equity = None

    # ------------------------------------------------------------------
    # Trailing stop management
    # ------------------------------------------------------------------

    def update_trailing_stop(self, symbol: str, current_price: float):
        pos = self.open_positions.get(symbol)
        if pos is None or pos.get("trailing_distance") is None:
            return

        direction = pos["direction"]
        distance = pos["trailing_distance"]
        old_stop = pos["trailing_stop"]

        if direction == "long":
            new_stop = max(old_stop, current_price - distance)
        else:
            new_stop = min(old_stop, current_price + distance)

        if new_stop != old_stop:
            logger.debug("%s trailing stop updated %.4f → %.4f", symbol, old_stop, new_stop)
            pos["trailing_stop"] = new_stop

    def is_any_stop_hit(self, symbol: str, current_price: float) -> tuple[bool, str]:
        """Returns (hit, reason) — checks hard stop then trailing stop."""
        pos = self.open_positions.get(symbol)
        if pos is None:
            return False, ""

        direction = pos["direction"]

        if direction == "long":
            if current_price <= pos["hard_stop"]:
                return True, f"hard stop {pos['hard_stop']:.4f}"
            if pos["trailing_stop"] is not None and current_price <= pos["trailing_stop"]:
                return True, f"trailing stop {pos['trailing_stop']:.4f}"
        else:
            if current_price >= pos["hard_stop"]:
                return True, f"hard stop {pos['hard_stop']:.4f}"
            if pos["trailing_stop"] is not None and current_price >= pos["trailing_stop"]:
                return True, f"trailing stop {pos['trailing_stop']:.4f}"

        return False, ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def has_position(self, symbol: str) -> bool:
        return symbol in self.open_positions

    def position_direction(self, symbol: str) -> str | None:
        return self.open_positions.get(symbol, {}).get("direction")

    def save_state(self, path: str = "logs/positions.json"):
        """Write open_positions to JSON so the next run can rehydrate."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.open_positions, f, indent=2)

    def load_state(self, path: str = "logs/positions.json"):
        """Populate open_positions from JSON. Call before sync_with_broker() so broker reconciliation can prune stale entries."""
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                saved = json.load(f)
            for symbol, pos in saved.items():
                if symbol not in self.open_positions:
                    self.open_positions[symbol] = pos
            logger.info("State restored from %s: %s", path, list(saved.keys()))
        except Exception as exc:
            logger.warning("Could not load state from %s: %s", path, exc)

    def sync_with_broker(self):
        """Reconcile local state against Alpaca positions (called on startup)."""
        try:
            broker_positions = {p.symbol: p for p in self._api.list_positions()}
        except Exception as exc:
            logger.error("Could not fetch broker positions: %s", exc)
            return

        for symbol in list(self.open_positions.keys()):
            # Alpaca returns crypto positions as "BTCUSD" (no slash) even though
            # the crypto bars API expects "BTC/USD". Check both forms.
            alpaca_symbol = symbol.replace("/", "")
            if symbol not in broker_positions and alpaca_symbol not in broker_positions:
                logger.warning("Local position %s not found at broker — removing", symbol)
                del self.open_positions[symbol]

        logger.info("Broker sync complete. Open positions: %s", list(self.open_positions.keys()))
