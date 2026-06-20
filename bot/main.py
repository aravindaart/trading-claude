"""
Main trading loop.

Responsibilities:
- Fetch bar data at the correct interval for each strategy
- Generate signals
- Apply risk management and correlation filter
- Open / close positions via Portfolio
- Handle API errors and market-closed states
- Log daily P&L at end of each trading day
"""
import logging
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi
import pandas as pd

from config import (
    ALPACA_API_KEY,
    ALPACA_BASE_URL,
    ALPACA_SECRET_KEY,
    INSTRUMENTS,
    MEAN_REVERSION,
    MOMENTUM_BREAKOUT,
    POLL_INTERVAL_SECONDS,
    RISK,
    TIMEZONE,
    TREND_FOLLOWING,
)
from bot.portfolio import Portfolio
from bot.risk_manager import RiskManager
from bot.strategies import mean_reversion, momentum_breakout, trend_following
from bot import briefing, telegram

import os
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

# Map timeframe strings to alpaca-trade-api TimeFrame values
TIMEFRAME_MAP = {
    "15Min": tradeapi.TimeFrame.Minute,   # we'll request 15-min bars below
    "1Hour": tradeapi.TimeFrame.Hour,
    "4Hour": tradeapi.TimeFrame.Hour,     # fetch hourly and resample to 4H
}

# Bars needed per strategy (with generous buffer for EMAs etc.)
BARS_NEEDED = {
    "mean_reversion": 50,
    "momentum_breakout": 60,
    "trend_following": 250,
}


def _build_api() -> tradeapi.REST:
    return tradeapi.REST(
        key_id=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        base_url=ALPACA_BASE_URL,
        api_version="v2",
    )


def _is_crypto(symbol: str) -> bool:
    return INSTRUMENTS[symbol]["asset_class"] == "crypto"


def _fetch_bars(api: tradeapi.REST, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """Fetch historical bars from Alpaca and return a DataFrame."""
    # Crypto symbols keep the slash for the crypto endpoint (e.g. "BTC/USD")
    # Equity symbols are plain tickers (e.g. "SPY")
    alpaca_symbol = symbol  # crypto endpoint expects "BTC/USD"
    is_crypto = _is_crypto(symbol)
    end = datetime.now(TZ)

    def _get(tf, start):
        if is_crypto:
            return api.get_crypto_bars(alpaca_symbol, tf, start=start.isoformat(), end=end.isoformat()).df
        return api.get_bars(
            alpaca_symbol, tf,
            start=start.isoformat(), end=end.isoformat(),
            adjustment="raw", feed="iex",
        ).df

    if timeframe == "15Min":
        start = end - timedelta(minutes=15 * limit * 2)
        bars = _get(tradeapi.TimeFrame.Minute, start)
        bars = bars.resample("15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    elif timeframe == "1Hour":
        start = end - timedelta(hours=limit * 2)
        bars = _get(tradeapi.TimeFrame.Hour, start)
    elif timeframe == "4Hour":
        start = end - timedelta(hours=4 * limit * 2)
        bars = _get(tradeapi.TimeFrame.Hour, start)
        bars = bars.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    else:
        raise ValueError(f"Unknown timeframe: {timeframe}")

    return bars.tail(limit)


def _is_equity_market_open(api: tradeapi.REST) -> bool:
    try:
        clock = api.get_clock()
        return clock.is_open
    except Exception as exc:
        logger.warning("Could not fetch market clock: %s", exc)
        return False


def _process_mean_reversion(
    symbol: str, api: tradeapi.REST, portfolio: Portfolio, risk: RiskManager
):
    cfg = MEAN_REVERSION
    bars = _fetch_bars(api, symbol, cfg["timeframe"], BARS_NEEDED["mean_reversion"])
    if bars.empty:
        return

    current_price = float(bars["close"].iloc[-1])

    if portfolio.has_position(symbol):
        # Check stops
        portfolio.update_trailing_stop(symbol, current_price)
        hit, reason = portfolio.is_any_stop_hit(symbol, current_price)
        if hit:
            portfolio.close_position(symbol, current_price, reason=reason)
            return

        # Check mean-reversion exit
        direction = portfolio.position_direction(symbol)
        if mean_reversion.check_exit(symbol, bars, direction):
            portfolio.close_position(symbol, current_price, reason="reverted to mean")
        return

    signal = mean_reversion.generate_signal(symbol, bars)
    if signal is None:
        return

    # ATR for position sizing — use std * sqrt(bars/day) as a proxy; or compute directly
    atr = float(bars["close"].std())  # simple proxy; real ATR requires high/low
    if "high" in bars.columns and "low" in bars.columns:
        from bot.strategies.trend_following import _atr as compute_atr
        atr = compute_atr(bars, RISK["atr_period"])

    if not risk.correlation_filter_allows(symbol, signal["direction"], portfolio.open_positions):
        return

    qty = risk.calc_position_size(atr, current_price)
    if qty <= 0:
        return

    hard_stop = risk.calc_hard_stop(signal["direction"], current_price)
    portfolio.open_position(symbol, signal["direction"], qty, current_price, hard_stop)
    logger.info("SIGNAL %s %s: %s", symbol, signal["direction"].upper(), signal["reason"])


def _process_momentum_breakout(
    symbol: str, api: tradeapi.REST, portfolio: Portfolio, risk: RiskManager
):
    cfg = MOMENTUM_BREAKOUT
    bars = _fetch_bars(api, symbol, cfg["timeframe"], BARS_NEEDED["momentum_breakout"])
    if bars.empty:
        return

    current_price = float(bars["close"].iloc[-1])

    if portfolio.has_position(symbol):
        portfolio.update_trailing_stop(symbol, current_price)
        hit, reason = portfolio.is_any_stop_hit(symbol, current_price)
        if hit:
            portfolio.close_position(symbol, current_price, reason=reason)
        return

    signal = momentum_breakout.generate_signal(symbol, bars)
    if signal is None:
        return

    if not risk.correlation_filter_allows(symbol, signal["direction"], portfolio.open_positions):
        return

    atr = signal.get("atr", current_price * 0.01)
    qty = risk.calc_position_size(atr, current_price)
    if qty <= 0:
        return

    hard_stop = risk.calc_hard_stop(signal["direction"], current_price)
    portfolio.open_position(
        symbol, signal["direction"], qty, current_price,
        hard_stop, trailing_stop_distance=signal["trailing_stop"],
    )
    logger.info("SIGNAL %s %s: %s", symbol, signal["direction"].upper(), signal["reason"])


def _process_trend_following(
    symbol: str, api: tradeapi.REST, portfolio: Portfolio, risk: RiskManager
):
    cfg = TREND_FOLLOWING
    bars = _fetch_bars(api, symbol, cfg["timeframe"], BARS_NEEDED["trend_following"])
    if bars.empty:
        return

    current_price = float(bars["close"].iloc[-1])

    if portfolio.has_position(symbol):
        portfolio.update_trailing_stop(symbol, current_price)
        hit, reason = portfolio.is_any_stop_hit(symbol, current_price)
        if hit:
            portfolio.close_position(symbol, current_price, reason=reason)
            return

        # Also close on death cross while long, or golden cross while short
        signal = trend_following.generate_signal(symbol, bars)
        if signal:
            current_dir = portfolio.position_direction(symbol)
            if signal["direction"] != current_dir:
                portfolio.close_position(symbol, current_price, reason=f"cross signal reversal: {signal['reason']}")
                # Immediately re-enter in new direction
                atr = signal.get("atr", current_price * 0.01)
                qty = risk.calc_position_size(atr, current_price)
                if qty > 0:
                    hard_stop = risk.calc_hard_stop(signal["direction"], current_price)
                    portfolio.open_position(
                        symbol, signal["direction"], qty, current_price,
                        hard_stop, trailing_stop_distance=signal["trailing_stop"],
                    )
        return

    signal = trend_following.generate_signal(symbol, bars)
    if signal is None:
        return

    if not risk.correlation_filter_allows(symbol, signal["direction"], portfolio.open_positions):
        return

    atr = signal.get("atr", current_price * 0.01)
    qty = risk.calc_position_size(atr, current_price)
    if qty <= 0:
        return

    hard_stop = risk.calc_hard_stop(signal["direction"], current_price)
    portfolio.open_position(
        symbol, signal["direction"], qty, current_price,
        hard_stop, trailing_stop_distance=signal["trailing_stop"],
    )
    logger.info("SIGNAL %s %s: %s", symbol, signal["direction"].upper(), signal["reason"])


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def run():
    logger.info("Starting trading bot")
    api = _build_api()
    portfolio = Portfolio(api)
    risk = RiskManager(api)

    # Reconcile with broker on start
    portfolio.sync_with_broker()

    equity_day_start_recorded = False
    # Persist the last briefing date to a file so restarts don't re-send it
    _BRIEFING_MARKER = "logs/.last_briefing_date"

    def _already_briefed_today() -> bool:
        try:
            with open(_BRIEFING_MARKER) as f:
                return f.read().strip() == datetime.now(TZ).date().isoformat()
        except FileNotFoundError:
            return False

    def _mark_briefed_today():
        os.makedirs("logs", exist_ok=True)
        with open(_BRIEFING_MARKER, "w") as f:
            f.write(datetime.now(TZ).date().isoformat())

    last_day: int | None = None

    EQUITY_SYMBOLS = [s for s, cfg in INSTRUMENTS.items() if cfg["asset_class"] == "us_equity"]
    CRYPTO_SYMBOLS = [s for s, cfg in INSTRUMENTS.items() if cfg["asset_class"] == "crypto"]

    while True:
        now = datetime.now(TZ)
        today = now.date().day

        # Daily P&L bookkeeping
        if today != last_day:
            if last_day is not None:
                try:
                    portfolio.record_day_end(risk.get_equity())
                except Exception as exc:
                    logger.error("Error recording day end: %s", exc)
            try:
                equity = risk.get_equity()
                portfolio.record_day_start(equity)
                equity_day_start_recorded = True

                # Send morning briefing once per calendar day only
                if not _already_briefed_today():
                    current_prices: dict[str, float] = {}
                    for sym in portfolio.open_positions:
                        try:
                            quote = api.get_latest_trade(sym.replace("/", ""))
                            current_prices[sym] = float(quote.price)
                        except Exception:
                            pass
                    msg = briefing.compose(portfolio.open_positions, current_prices, equity)
                    telegram.send_message(msg)
                    _mark_briefed_today()
            except Exception as exc:
                logger.error("Error recording day start: %s", exc)
            last_day = today

        equity_market_open = _is_equity_market_open(api)

        for symbol, cfg in INSTRUMENTS.items():
            strategy = cfg["strategy"]
            is_equity = cfg["asset_class"] == "us_equity"

            # Equities only trade during market hours; crypto runs 24/7
            if is_equity and not equity_market_open:
                continue

            try:
                if strategy == "mean_reversion":
                    _process_mean_reversion(symbol, api, portfolio, risk)
                elif strategy == "momentum_breakout":
                    _process_momentum_breakout(symbol, api, portfolio, risk)
                elif strategy == "trend_following":
                    _process_trend_following(symbol, api, portfolio, risk)
            except tradeapi.rest.APIError as exc:
                logger.error("Alpaca API error for %s: %s", symbol, exc)
            except Exception as exc:
                logger.exception("Unexpected error processing %s: %s", symbol, exc)

        logger.debug("Sleeping %ds", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
