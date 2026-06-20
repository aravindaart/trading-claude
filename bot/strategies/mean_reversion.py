"""
Mean Reversion Strategy for SPY and QQQ.
Timeframe: 15-minute candles
Signal: price deviates > N std deviations from 20-period SMA → expect reversion
"""
import logging
import numpy as np
import pandas as pd

from config import MEAN_REVERSION

logger = logging.getLogger(__name__)


def _sma_std(closes: pd.Series, period: int) -> tuple[float, float]:
    window = closes.iloc[-period:]
    return float(window.mean()), float(window.std(ddof=1))


def generate_signal(symbol: str, bars: pd.DataFrame) -> dict | None:
    """
    Returns a signal dict or None.

    Signal dict keys:
        direction  : "long" | "short" | "exit"
        reason     : human-readable string
        entry_price: float (latest close)
    """
    cfg = MEAN_REVERSION
    period = cfg["lookback_periods"]
    multiplier = cfg["std_multiplier"].get(symbol)

    if multiplier is None:
        logger.warning("No std_multiplier configured for %s", symbol)
        return None

    if len(bars) < period:
        logger.debug("%s: not enough bars (%d < %d)", symbol, len(bars), period)
        return None

    closes = bars["close"].astype(float)
    sma, std = _sma_std(closes, period)
    price = closes.iloc[-1]

    if std == 0:
        return None

    z_score = (price - sma) / std
    logger.debug("%s price=%.4f sma=%.4f std=%.4f z=%.3f", symbol, price, sma, std, z_score)

    if z_score < -multiplier:
        return {"direction": "long", "reason": f"z={z_score:.2f} < -{multiplier}", "entry_price": price, "sma": sma}

    if z_score > multiplier:
        return {"direction": "short", "reason": f"z={z_score:.2f} > +{multiplier}", "entry_price": price, "sma": sma}

    return None


def check_exit(symbol: str, bars: pd.DataFrame, position_direction: str) -> bool:
    """Return True when price has reverted to the SMA — time to exit."""
    cfg = MEAN_REVERSION
    period = cfg["lookback_periods"]

    if len(bars) < period:
        return False

    closes = bars["close"].astype(float)
    sma, _ = _sma_std(closes, period)
    price = closes.iloc[-1]

    if position_direction == "long" and price >= sma:
        return True
    if position_direction == "short" and price <= sma:
        return True
    return False
