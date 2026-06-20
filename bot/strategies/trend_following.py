"""
Trend Following Strategy for GLD and USO.
Timeframe: 4-hour candles
Signal: 50 EMA crosses 200 EMA
Stop  : 3x ATR trailing stop
"""
import logging
import pandas as pd

from config import TREND_FOLLOWING

logger = logging.getLogger(__name__)


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(bars: pd.DataFrame, period: int) -> float:
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return float(tr.iloc[-period:].mean())


def generate_signal(symbol: str, bars: pd.DataFrame) -> dict | None:
    """
    Returns a signal dict or None.

    Signal dict keys:
        direction    : "long" | "short"
        reason       : human-readable string
        entry_price  : float (latest close)
        trailing_stop: float (absolute distance from entry)
        atr          : float
    """
    cfg = TREND_FOLLOWING
    fast_period = cfg["fast_ema"]
    slow_period = cfg["slow_ema"]
    atr_period = cfg["atr_period"]
    atr_mult = cfg["atr_trailing_stop"]

    min_bars = slow_period + 1
    if len(bars) < min_bars:
        logger.debug("%s: not enough bars (%d < %d)", symbol, len(bars), min_bars)
        return None

    closes = bars["close"].astype(float)

    fast = _ema(closes, fast_period)
    slow = _ema(closes, slow_period)

    fast_prev, fast_curr = float(fast.iloc[-2]), float(fast.iloc[-1])
    slow_prev, slow_curr = float(slow.iloc[-2]), float(slow.iloc[-1])

    atr = _atr(bars, atr_period)
    trailing_stop = atr_mult * atr
    price = float(closes.iloc[-1])

    logger.debug(
        "%s price=%.4f fast_ema=%.4f slow_ema=%.4f atr=%.4f",
        symbol, price, fast_curr, slow_curr, atr,
    )

    # Golden cross: fast crosses above slow
    if fast_prev <= slow_prev and fast_curr > slow_curr:
        return {
            "direction": "long",
            "reason": f"golden cross fast={fast_curr:.4f} > slow={slow_curr:.4f}",
            "entry_price": price,
            "trailing_stop": trailing_stop,
            "atr": atr,
        }

    # Death cross: fast crosses below slow
    if fast_prev >= slow_prev and fast_curr < slow_curr:
        return {
            "direction": "short",
            "reason": f"death cross fast={fast_curr:.4f} < slow={slow_curr:.4f}",
            "entry_price": price,
            "trailing_stop": trailing_stop,
            "atr": atr,
        }

    return None


def update_trailing_stop(
    position_direction: str,
    current_price: float,
    current_stop: float,
    trailing_distance: float,
) -> float:
    """Ratchet trailing stop; never move against the position."""
    if position_direction == "long":
        return max(current_stop, current_price - trailing_distance)
    return min(current_stop, current_price + trailing_distance)


def check_stop_hit(position_direction: str, current_price: float, stop_price: float) -> bool:
    if position_direction == "long":
        return current_price <= stop_price
    return current_price >= stop_price
