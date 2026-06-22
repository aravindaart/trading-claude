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
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
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

# After a failed order, suppress re-entry for one full bar period
_COOLDOWN_MINUTES = {
    "mean_reversion": 15,
    "momentum_breakout": 60,
    "trend_following": 240,
}

# Max bars a mean-reversion position can be held before forced exit (8 × 15 min = 2 h)
_MEAN_REVERSION_MAX_HOLD_BARS = 8


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

    def _get(tf, start) -> pd.DataFrame:
        if is_crypto:
            df = api.get_crypto_bars(alpaca_symbol, tf, start=start.isoformat(), end=end.isoformat()).df
        else:
            df = api.get_bars(
                alpaca_symbol, tf,
                start=start.isoformat(), end=end.isoformat(),
                adjustment="raw", feed="iex",
            ).df

        if df.empty:
            return df

        # Alpaca returns a RangeIndex when no data or a symbol column exists.
        # Ensure we always have a DatetimeIndex so resample works correctly.
        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
            else:
                logger.warning("%s: bars have no timestamp index — skipping", symbol)
                return pd.DataFrame()

        df.index = pd.to_datetime(df.index, utc=True)
        # Drop the symbol column if present (crypto bars include it)
        df = df.drop(columns=["symbol"], errors="ignore")
        return df

    if timeframe == "15Min":
        start = end - timedelta(minutes=15 * limit * 2)
        bars = _get(tradeapi.TimeFrame.Minute, start)
        if bars.empty:
            return bars
        bars = bars.resample("15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    elif timeframe == "1Hour":
        start = end - timedelta(hours=limit * 2)
        bars = _get(tradeapi.TimeFrame.Hour, start)
    elif timeframe == "4Hour":
        start = end - timedelta(hours=4 * limit * 2)
        bars = _get(tradeapi.TimeFrame.Hour, start)
        if bars.empty:
            return bars
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
    symbol: str, api: tradeapi.REST, portfolio: Portfolio, risk: RiskManager,
    entry_blocked_until: dict, equity: float, close_only: bool = False,
):
    cfg = MEAN_REVERSION
    bars = _fetch_bars(api, symbol, cfg["timeframe"], BARS_NEEDED["mean_reversion"])
    if bars.empty:
        return

    current_price = float(bars["close"].iloc[-1])

    if portfolio.has_position(symbol):
        portfolio.update_trailing_stop(symbol, current_price)
        hit, reason = portfolio.is_any_stop_hit(symbol, current_price)
        if hit:
            if portfolio.close_position(symbol, current_price, reason=reason):
                portfolio.save_state()
            return

        # Force-exit after max holding period to avoid riding a trend indefinitely
        pos = portfolio.open_positions[symbol]
        opened_at = datetime.fromisoformat(pos["opened_at"])
        bars_held = (datetime.now(TZ) - opened_at).total_seconds() / (15 * 60)
        if bars_held >= _MEAN_REVERSION_MAX_HOLD_BARS:
            if portfolio.close_position(
                symbol, current_price,
                reason=f"max hold period ({_MEAN_REVERSION_MAX_HOLD_BARS} bars) reached",
            ):
                portfolio.save_state()
            return

        direction = portfolio.position_direction(symbol)
        if mean_reversion.check_exit(symbol, bars, direction):
            if portfolio.close_position(symbol, current_price, reason="reverted to mean"):
                portfolio.save_state()
        return

    if close_only:
        return

    # Cooldown check — skip entry if a recent order failed
    now = datetime.now(TZ)
    blocked_until = entry_blocked_until.get(symbol)
    if blocked_until and now < blocked_until:
        logger.debug("%s: entry cooldown active until %s", symbol, blocked_until.isoformat())
        return

    signal = mean_reversion.generate_signal(symbol, bars)
    if signal is None:
        return

    # ATR for position sizing
    atr = float(bars["close"].std())
    if "high" in bars.columns and "low" in bars.columns:
        from bot.strategies.trend_following import _atr as compute_atr
        atr = compute_atr(bars, RISK["atr_period"])

    if not risk.correlation_filter_allows(symbol, signal["direction"], portfolio.open_positions):
        return

    qty = risk.calc_position_size(atr, current_price, equity=equity, fractional=_is_crypto(symbol))
    if qty <= 0:
        return

    hard_stop = risk.calc_hard_stop(signal["direction"], current_price, qty, equity=equity)
    opened = portfolio.open_position(symbol, signal["direction"], qty, current_price, hard_stop)
    if opened:
        portfolio.save_state()
        logger.info("SIGNAL %s %s: %s", symbol, signal["direction"].upper(), signal["reason"])
    else:
        entry_blocked_until[symbol] = now + timedelta(minutes=_COOLDOWN_MINUTES["mean_reversion"])
        logger.warning("%s: open_position failed, cooling down for %d min", symbol, _COOLDOWN_MINUTES["mean_reversion"])


def _process_momentum_breakout(
    symbol: str, api: tradeapi.REST, portfolio: Portfolio, risk: RiskManager,
    entry_blocked_until: dict, pending_signals: dict, equity: float, close_only: bool = False,
):
    cfg = MOMENTUM_BREAKOUT
    bars = _fetch_bars(api, symbol, cfg["timeframe"], BARS_NEEDED["momentum_breakout"])
    if bars.empty:
        return

    current_price = float(bars["close"].iloc[-1])

    if portfolio.has_position(symbol):
        pending_signals.pop(symbol, None)
        portfolio.update_trailing_stop(symbol, current_price)
        hit, reason = portfolio.is_any_stop_hit(symbol, current_price)
        if hit:
            if portfolio.close_position(symbol, current_price, reason=reason):
                portfolio.save_state()
        return

    signal = momentum_breakout.generate_signal(symbol, bars)

    # Alpaca does not support crypto short selling — skip short signals for crypto symbols
    if signal is not None and _is_crypto(symbol) and signal["direction"] == "short":
        logger.debug("%s: skipping short signal — crypto short selling not supported", symbol)
        signal = None

    if signal is None:
        pending_signals.pop(symbol, None)
        return

    # 1-bar confirmation: only enter if the same signal direction was seen last cycle
    prev = pending_signals.get(symbol)
    pending_signals[symbol] = signal
    if prev is None or prev["direction"] != signal["direction"]:
        logger.debug("%s: new %s signal — waiting for 1-bar confirmation", symbol, signal["direction"])
        return

    # Cooldown check
    now = datetime.now(TZ)
    blocked_until = entry_blocked_until.get(symbol)
    if blocked_until and now < blocked_until:
        logger.debug("%s: entry cooldown active until %s", symbol, blocked_until.isoformat())
        return

    if close_only:
        return

    # Alpaca does not support crypto short selling — skip short signals for crypto symbols
    if _is_crypto(symbol) and signal["direction"] == "short":
        logger.debug("%s: skipping short signal — crypto short selling not supported", symbol)
        return

    if not risk.correlation_filter_allows(symbol, signal["direction"], portfolio.open_positions):
        return

    atr = signal.get("atr", current_price * 0.01)
    qty = risk.calc_position_size(atr, current_price, equity=equity, fractional=_is_crypto(symbol))
    if qty <= 0:
        return

    hard_stop = risk.calc_hard_stop(signal["direction"], current_price, qty, equity=equity)
    opened = portfolio.open_position(
        symbol, signal["direction"], qty, current_price,
        hard_stop, trailing_stop_distance=signal["trailing_stop"],
    )
    if opened:
        pending_signals.pop(symbol, None)
        portfolio.save_state()
        logger.info("SIGNAL %s %s: %s", symbol, signal["direction"].upper(), signal["reason"])
    else:
        entry_blocked_until[symbol] = now + timedelta(minutes=_COOLDOWN_MINUTES["momentum_breakout"])
        logger.warning("%s: open_position failed, cooling down for %d min", symbol, _COOLDOWN_MINUTES["momentum_breakout"])


def _process_trend_following(
    symbol: str, api: tradeapi.REST, portfolio: Portfolio, risk: RiskManager,
    pending_signals: dict, equity: float, close_only: bool = False,
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
            if portfolio.close_position(symbol, current_price, reason=reason):
                portfolio.save_state()
            pending_signals.pop(symbol, None)
            return

        # Close on cross reversal; re-entry handled via pending_signals on the next cycle
        signal = trend_following.generate_signal(symbol, bars)
        if signal:
            current_dir = portfolio.position_direction(symbol)
            if signal["direction"] != current_dir:
                closed = portfolio.close_position(
                    symbol, current_price,
                    reason=f"cross signal reversal: {signal['reason']}",
                )
                if closed:
                    portfolio.save_state()
                    # Stage the new direction for 1-bar confirmation on next poll
                    pending_signals[symbol] = signal
        return

    signal = trend_following.generate_signal(symbol, bars)
    if signal is None:
        pending_signals.pop(symbol, None)
        return

    # 1-bar confirmation: only enter if the same signal direction was seen last cycle
    prev = pending_signals.get(symbol)
    pending_signals[symbol] = signal
    if prev is None or prev["direction"] != signal["direction"]:
        logger.debug("%s: new %s signal — waiting for 1-bar confirmation", symbol, signal["direction"])
        return

    if close_only:
        return

    if not risk.correlation_filter_allows(symbol, signal["direction"], portfolio.open_positions):
        return

    atr = signal.get("atr", current_price * 0.01)
    qty = risk.calc_position_size(atr, current_price, equity=equity, fractional=_is_crypto(symbol))
    if qty <= 0:
        return

    hard_stop = risk.calc_hard_stop(signal["direction"], current_price, qty, equity=equity)
    opened = portfolio.open_position(
        symbol, signal["direction"], qty, current_price,
        hard_stop, trailing_stop_distance=signal["trailing_stop"],
    )
    if opened:
        pending_signals.pop(symbol, None)
        portfolio.save_state()
        logger.info("SIGNAL %s %s: %s", symbol, signal["direction"].upper(), signal["reason"])


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def run():
    logger.info("Starting trading bot")
    api = _build_api()
    portfolio = Portfolio(api)
    risk = RiskManager(api)

    # Rehydrate position metadata from the last run, then reconcile with broker
    portfolio.load_state()
    portfolio.sync_with_broker()

    equity_day_start_recorded = False
    last_day: date | None = None
    _BRIEFING_MARKER = "logs/.last_briefing_date"

    # Cooldown tracking: symbol → datetime after which re-entry is allowed
    _entry_blocked_until: dict[str, datetime] = {}
    # Signal confirmation: symbol → last seen signal dict (for 1-bar confirmation)
    _pending_signals: dict[str, dict] = {}
    # Periodic Gist push: push logs every 30 min so the dashboard stays fresh mid-run
    _last_gist_push: float = 0.0
    _GIST_PUSH_INTERVAL = 1800  # seconds

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

    EQUITY_SYMBOLS = [s for s, cfg in INSTRUMENTS.items() if cfg["asset_class"] == "us_equity"]
    CRYPTO_SYMBOLS = [s for s, cfg in INSTRUMENTS.items() if cfg["asset_class"] == "crypto"]

    while True:
        now = datetime.now(TZ)
        today = now.date()

        # Cache equity once per loop — passed to sizing/stop functions to avoid repeat API calls
        try:
            cached_equity = risk.get_equity()
        except Exception as exc:
            logger.warning("Could not fetch equity this loop: %s — skipping iteration", exc)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Daily P&L bookkeeping
        if today != last_day:
            if last_day is not None:
                try:
                    portfolio.record_day_end(cached_equity)
                except Exception as exc:
                    logger.error("Error recording day end: %s", exc)
            try:
                portfolio.record_day_start(cached_equity)
                equity_day_start_recorded = True

                # Send morning briefing once per calendar day only
                if not _already_briefed_today() and now.hour >= 7:
                    current_prices: dict[str, float] = {}
                    for sym in portfolio.open_positions:
                        try:
                            quote = api.get_latest_trade(sym.replace("/", ""))
                            current_prices[sym] = float(quote.price)
                        except Exception:
                            pass
                    msg = briefing.compose(portfolio.open_positions, current_prices, cached_equity)
                    telegram.send_message(msg)
                    _mark_briefed_today()
            except Exception as exc:
                logger.error("Error recording day start: %s", exc)
            last_day = today
            _entry_blocked_until.clear()
            logger.info("New trading day — entry cooldowns cleared")

        # Daily max-loss circuit breaker: go close-only for the rest of the day once
        # cumulative loss exceeds max_daily_loss_pct of day-start equity.
        _close_only = False
        if portfolio._day_start_equity is not None:
            day_loss_pct = (cached_equity - portfolio._day_start_equity) / portfolio._day_start_equity
            if day_loss_pct <= -RISK.get("max_daily_loss_pct", 0.03):
                _close_only = True
                logger.warning(
                    "Daily max-loss triggered (%.2f%% loss) — close-only mode active for remainder of day",
                    day_loss_pct * 100,
                )

        equity_market_open = _is_equity_market_open(api)

        for symbol, cfg in INSTRUMENTS.items():
            strategy = cfg["strategy"]
            is_equity = cfg["asset_class"] == "us_equity"

            # Equities only trade during market hours; crypto runs 24/7
            if is_equity and not equity_market_open:
                continue

            try:
                if strategy == "mean_reversion":
                    _process_mean_reversion(symbol, api, portfolio, risk, _entry_blocked_until, cached_equity, close_only=_close_only)
                elif strategy == "momentum_breakout":
                    _process_momentum_breakout(symbol, api, portfolio, risk, _entry_blocked_until, _pending_signals, cached_equity, close_only=_close_only)
                elif strategy == "trend_following":
                    _process_trend_following(symbol, api, portfolio, risk, _pending_signals, cached_equity, close_only=_close_only)
            except tradeapi.rest.APIError as exc:
                logger.error("Alpaca API error for %s: %s", symbol, exc)
            except Exception as exc:
                logger.exception("Unexpected error processing %s: %s", symbol, exc)

        # Persist trailing-stop movements and any other mid-loop state changes
        portfolio.save_state()

        # Push logs to Gist every 30 min so the dashboard reflects current state mid-run
        if os.environ.get("GH_GIST_TOKEN") and time.time() - _last_gist_push >= _GIST_PUSH_INTERVAL:
            try:
                subprocess.run(
                    [sys.executable, "scripts/push_logs_to_gist.py"],
                    check=False, timeout=60,
                )
                _last_gist_push = time.time()
                logger.info("Periodic Gist push completed")
            except Exception as exc:
                logger.warning("Periodic Gist push failed: %s", exc)

        logger.debug("Sleeping %ds", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
