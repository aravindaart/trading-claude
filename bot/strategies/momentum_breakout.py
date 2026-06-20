"""
Momentum Breakout Strategy for BTC/USD.
Timeframe: 1-hour candles
Signal: price breaks 20-period high/low with volume >= 1.5x 20-period avg volume
Stop  : 2x ATR trailing stop
"""
import logging
import numpy as np
import pandas as pd

from config import MOMENTUM_BREAKOUT

logger = logging.getLogger(__name__)


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
    """
    cfg = MOMENTUM_BREAKOUT
    period = cfg["lookback_periods"]
    vol_mult = cfg["volume_multiplier"]
    atr_period = cfg["atr_period"]
    atr_mult = cfg["atr_trailing_stop"]

    min_bars = max(period, atr_period) + 1
    if len(bars) < min_bars:
        logger.debug("%s: not enough bars (%d < %d)", symbol, len(bars), min_bars)
        return None

    closes = bars["close"].astype(float)
    highs = bars["high"].astype(float)
    lows = bars["low"].astype(float)
    volumes = bars["volume"].astype(float)

    # Use bars[-period-1:-1] as the lookback window (exclude current bar)
    lookback_highs = highs.iloc[-(period + 1):-1]
    lookback_lows = lows.iloc[-(period + 1):-1]
    lookback_vols = volumes.iloc[-(period + 1):-1]

    period_high = float(lookback_highs.max())
    period_low = float(lookback_lows.min())
    avg_volume = float(lookback_vols.mean())

    current_price = float(closes.iloc[-1])
    current_volume = float(volumes.iloc[-1])

    volume_confirmed = avg_volume > 0 and current_volume >= vol_mult * avg_volume
    atr = _atr(bars, atr_period)
    trailing_stop = atr_mult * atr

    logger.debug(
        "%s price=%.2f high=%.2f low=%.2f vol=%.0f avg_vol=%.0f atr=%.4f",
        symbol, current_price, period_high, period_low, current_volume, avg_volume, atr,
    )

    if current_price > period_high and volume_confirmed:
        return {
            "direction": "long",
            "reason": f"breakout above {period_high:.2f} vol_ratio={current_volume/avg_volume:.2f}",
            "entry_price": current_price,
            "trailing_stop": trailing_stop,
            "atr": atr,
        }

    if current_price < period_low and volume_confirmed:
        return {
            "direction": "short",
            "reason": f"breakdown below {period_low:.2f} vol_ratio={current_volume/avg_volume:.2f}",
            "entry_price": current_price,
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
    """Ratchet the trailing stop upward (long) or downward (short)."""
    if position_direction == "long":
        new_stop = current_price - trailing_distance
        return max(current_stop, new_stop)
    else:
        new_stop = current_price + trailing_distance
        return min(current_stop, new_stop)


def check_stop_hit(position_direction: str, current_price: float, stop_price: float) -> bool:
    if position_direction == "long":
        return current_price <= stop_price
    return current_price >= stop_price
