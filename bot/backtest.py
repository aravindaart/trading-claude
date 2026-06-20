"""
Backtester for all 3 strategies against 6 months of Alpaca historical data.

Features:
- Realistic 0.05% slippage per trade
- ATR-based position sizing (mirrors live risk_manager)
- Correlation filter (SPY+QQQ both long → block BTC/USD long)
- Per-instrument metrics: trades, win rate, avg win/loss, profit factor,
  max drawdown, Sharpe ratio, total return
- Combined portfolio equity curve → backtest_results.png
- Flags strategies with negative Sharpe ratio
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from tabulate import tabulate

# ── project imports ──────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    INSTRUMENTS, MEAN_REVERSION, MOMENTUM_BREAKOUT, TREND_FOLLOWING, RISK,
)
from bot.strategies import mean_reversion, momentum_breakout, trend_following

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/New_York")
SLIPPAGE = 0.0005          # 0.05% per side
LOOKBACK_MONTHS = 6
INITIAL_EQUITY = 100_000.0


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    direction: str        # "long" | "short"
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    qty: float
    pnl: float


@dataclass
class SimPosition:
    direction: str
    entry_price: float
    qty: float
    hard_stop: float
    trailing_stop: float | None
    trailing_distance: float | None
    entry_ts: pd.Timestamp


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def _build_api() -> tradeapi.REST:
    return tradeapi.REST(
        key_id=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        base_url=ALPACA_BASE_URL,
        api_version="v2",
    )


def _fetch_history(api: tradeapi.REST, symbol: str, timeframe: str) -> pd.DataFrame:
    end = datetime.now(TZ)
    start = end - timedelta(days=LOOKBACK_MONTHS * 31)
    is_crypto = INSTRUMENTS[symbol]["asset_class"] == "crypto"

    logger.info("Fetching %s %s history (%s → %s)…", symbol, timeframe, start.date(), end.date())

    def _get(tf, s, e) -> pd.DataFrame:
        if is_crypto:
            df = api.get_crypto_bars(symbol, tf, start=s.isoformat(), end=e.isoformat()).df
        else:
            df = api.get_bars(symbol, tf, start=s.isoformat(), end=e.isoformat(), adjustment="raw", feed="iex").df
        if df.empty:
            return df
        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
            else:
                return pd.DataFrame()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.drop(columns=["symbol"], errors="ignore")
        return df

    if timeframe == "15Min":
        raw = _get(tradeapi.TimeFrame.Minute, start, end)
        bars = raw.resample("15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    elif timeframe == "1Hour":
        bars = _get(tradeapi.TimeFrame.Hour, start, end)
    elif timeframe == "4Hour":
        raw = _get(tradeapi.TimeFrame.Hour, start, end)
        bars = raw.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    else:
        raise ValueError(f"Unknown timeframe: {timeframe}")

    bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(TZ)
    logger.info("  → %d bars", len(bars))
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Risk helpers (mirrors RiskManager but equity-state-aware)
# ─────────────────────────────────────────────────────────────────────────────

def _atr_series(bars: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = bars["high"].astype(float), bars["low"].astype(float), bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _position_size(equity: float, atr: float, price: float) -> float:
    if atr <= 0 or price <= 0:
        return 0.0
    risk_dollars = equity * RISK["risk_per_trade_pct"]
    atr_shares = risk_dollars / atr
    hard_stop_shares = (equity * RISK["hard_stop_pct"]) / price
    return max(1.0, min(atr_shares, hard_stop_shares))


def _hard_stop(direction: str, price: float) -> float:
    dist = price * RISK["hard_stop_pct"]
    return price - dist if direction == "long" else price + dist


def _apply_slippage(price: float, direction: str, is_entry: bool) -> float:
    """Entry long / exit short → buy at a slightly higher price. Opposite for sells."""
    buying = (direction == "long" and is_entry) or (direction == "short" and not is_entry)
    return price * (1 + SLIPPAGE) if buying else price * (1 - SLIPPAGE)


# ─────────────────────────────────────────────────────────────────────────────
# Single-instrument simulation
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_instrument(
    symbol: str,
    bars: pd.DataFrame,
    strategy: str,
    initial_equity: float,
) -> tuple[list[Trade], pd.Series]:
    """
    Walk bar-by-bar, generate signals, manage positions.
    Returns (trades, equity_curve).
    equity_curve is indexed by bar timestamp.
    """
    trades: list[Trade] = []
    equity = initial_equity
    equity_ts: list[tuple] = [(bars.index[0], equity)]
    pos: SimPosition | None = None

    atr_period = RISK["atr_period"]

    # Minimum warmup based on strategy
    warmup = {
        "mean_reversion": MEAN_REVERSION["lookback_periods"] + 2,
        "momentum_breakout": MOMENTUM_BREAKOUT["lookback_periods"] + MOMENTUM_BREAKOUT["atr_period"] + 2,
        "trend_following": TREND_FOLLOWING["slow_ema"] + 2,  # dynamic — reads current config
    }[strategy]

    for i in range(warmup, len(bars)):
        window = bars.iloc[: i + 1]
        bar = bars.iloc[i]
        ts = bars.index[i]
        price = float(bar["close"])
        atr_val = float(_atr_series(window, atr_period).iloc[-1])

        # ── manage open position ──────────────────────────────────────────
        if pos is not None:
            # update trailing stop
            if pos.trailing_distance is not None:
                if pos.direction == "long":
                    new_stop = max(pos.trailing_stop or -np.inf, price - pos.trailing_distance)
                else:
                    new_stop = min(pos.trailing_stop or np.inf, price + pos.trailing_distance)
                pos.trailing_stop = new_stop

            # check stops
            stop_hit = False
            if pos.direction == "long":
                stop_hit = price <= pos.hard_stop or (pos.trailing_stop is not None and price <= pos.trailing_stop)
            else:
                stop_hit = price >= pos.hard_stop or (pos.trailing_stop is not None and price >= pos.trailing_stop)

            # check strategy exit signal
            exit_signal = False
            if strategy == "mean_reversion" and not stop_hit:
                exit_signal = mean_reversion.check_exit(symbol, window, pos.direction)
            elif strategy == "trend_following" and not stop_hit:
                sig = trend_following.generate_signal(symbol, window)
                if sig and sig["direction"] != pos.direction:
                    exit_signal = True

            if stop_hit or exit_signal:
                fill = _apply_slippage(price, pos.direction, is_entry=False)
                if pos.direction == "long":
                    pnl = (fill - pos.entry_price) * pos.qty
                else:
                    pnl = (pos.entry_price - fill) * pos.qty
                equity += pnl
                trades.append(Trade(
                    symbol=symbol,
                    direction=pos.direction,
                    entry_ts=pos.entry_ts,
                    entry_price=pos.entry_price,
                    exit_ts=ts,
                    exit_price=fill,
                    qty=pos.qty,
                    pnl=pnl,
                ))
                pos = None
                equity_ts.append((ts, equity))

                # for trend-following, immediately re-enter on cross
                if exit_signal and strategy == "trend_following":
                    sig = trend_following.generate_signal(symbol, window)
                    if sig:
                        qty = _position_size(equity, atr_val, price)
                        if qty > 0:
                            fill_entry = _apply_slippage(price, sig["direction"], is_entry=True)
                            pos = SimPosition(
                                direction=sig["direction"],
                                entry_price=fill_entry,
                                qty=qty,
                                hard_stop=_hard_stop(sig["direction"], fill_entry),
                                trailing_stop=(
                                    fill_entry - sig["trailing_stop"]
                                    if sig["direction"] == "long"
                                    else fill_entry + sig["trailing_stop"]
                                ),
                                trailing_distance=sig.get("trailing_stop"),
                                entry_ts=ts,
                            )
            equity_ts.append((ts, equity))
            continue

        # ── no open position — look for signal ───────────────────────────
        signal = None
        trailing_dist = None

        if strategy == "mean_reversion":
            signal = mean_reversion.generate_signal(symbol, window)
        elif strategy == "momentum_breakout":
            signal = momentum_breakout.generate_signal(symbol, window)
            if signal:
                trailing_dist = signal.get("trailing_stop")
        elif strategy == "trend_following":
            signal = trend_following.generate_signal(symbol, window)
            if signal:
                trailing_dist = signal.get("trailing_stop")

        if signal is None:
            equity_ts.append((ts, equity))
            continue

        qty = _position_size(equity, atr_val, price)
        if qty <= 0:
            equity_ts.append((ts, equity))
            continue

        fill = _apply_slippage(price, signal["direction"], is_entry=True)
        initial_trailing = None
        if trailing_dist is not None:
            initial_trailing = (
                fill - trailing_dist if signal["direction"] == "long"
                else fill + trailing_dist
            )
        pos = SimPosition(
            direction=signal["direction"],
            entry_price=fill,
            qty=qty,
            hard_stop=_hard_stop(signal["direction"], fill),
            trailing_stop=initial_trailing,
            trailing_distance=trailing_dist,
            entry_ts=ts,
        )
        equity_ts.append((ts, equity))

    # close any open position at last bar
    if pos is not None:
        last_price = float(bars.iloc[-1]["close"])
        fill = _apply_slippage(last_price, pos.direction, is_entry=False)
        pnl = (fill - pos.entry_price) * pos.qty if pos.direction == "long" else (pos.entry_price - fill) * pos.qty
        equity += pnl
        trades.append(Trade(
            symbol=symbol, direction=pos.direction,
            entry_ts=pos.entry_ts, entry_price=pos.entry_price,
            exit_ts=bars.index[-1], exit_price=fill,
            qty=pos.qty, pnl=pnl,
        ))
        equity_ts.append((bars.index[-1], equity))

    idx, vals = zip(*equity_ts) if equity_ts else ([], [])
    curve = pd.Series(vals, index=idx, dtype=float).resample("1D").last().ffill()
    return trades, curve


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio simulation (correlation filter)
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_portfolio(
    all_bars: dict[str, pd.DataFrame],
    per_instrument_trades: dict[str, list[Trade]],
) -> pd.Series:
    """
    Replay all trades in chronological order with a shared equity pool
    and the correlation filter active.
    Returns combined daily equity curve.
    """
    equity = INITIAL_EQUITY
    open_positions: dict[str, str] = {}  # symbol → direction
    events: list[tuple] = []

    for sym, trades in per_instrument_trades.items():
        for t in trades:
            events.append(("entry", t.entry_ts, sym, t.direction, t))
            events.append(("exit",  t.exit_ts,  sym, t.direction, t))

    events.sort(key=lambda x: x[1])

    equity_ts: list[tuple] = [(events[0][1], equity)] if events else []

    for kind, ts, sym, direction, trade in events:
        if kind == "entry":
            # correlation filter
            if sym == "BTC/USD" and direction == "long":
                if open_positions.get("SPY") == "long" and open_positions.get("QQQ") == "long":
                    continue
            open_positions[sym] = direction
        else:
            if open_positions.get(sym) == direction:
                equity += trade.pnl
                del open_positions[sym]
            equity_ts.append((ts, equity))

    if not equity_ts:
        return pd.Series(dtype=float)

    idx, vals = zip(*equity_ts)
    return pd.Series(vals, index=idx, dtype=float).resample("1D").last().ffill()


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def _metrics(trades: list[Trade], equity_curve: pd.Series, label: str) -> dict:
    if not trades:
        return {"symbol": label, "trades": 0, "win_rate": 0, "avg_win": 0,
                "avg_loss": 0, "profit_factor": 0, "max_drawdown_pct": 0,
                "sharpe": 0, "total_return_pct": 0, "flag": "NO TRADES"}

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    # max drawdown
    roll_max = equity_curve.cummax()
    drawdown = (equity_curve - roll_max) / roll_max * 100
    max_dd = float(drawdown.min())

    # daily returns for Sharpe
    daily_ret = equity_curve.pct_change().dropna()
    sharpe = 0.0
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252))

    total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) * 100 if len(equity_curve) > 1 else 0

    flag = ""
    if sharpe < 0:
        flag = "⚠ NEGATIVE SHARPE — review parameters"

    return {
        "symbol": label,
        "trades": len(trades),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != np.inf else "∞",
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
        "total_return_pct": round(total_return, 2),
        "flag": flag,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Chart
# ─────────────────────────────────────────────────────────────────────────────

def _plot(
    per_curves: dict[str, pd.Series],
    portfolio_curve: pd.Series,
    out_path: str = "backtest_results.png",
):
    fig, axes = plt.subplots(3, 2, figsize=(16, 12))
    fig.suptitle("Backtest Results — 6-Month Historical Simulation", fontsize=14, fontweight="bold")

    colors = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#F44336"]
    symbols = list(per_curves.keys())

    for idx, (sym, curve) in enumerate(per_curves.items()):
        ax = axes[idx // 2][idx % 2]
        norm = curve / curve.iloc[0] * 100
        ax.plot(curve.index, norm, color=colors[idx], linewidth=1.5)
        ax.axhline(100, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
        ax.fill_between(curve.index, 100, norm,
                        where=(norm >= 100), alpha=0.15, color="green")
        ax.fill_between(curve.index, 100, norm,
                        where=(norm < 100), alpha=0.15, color="red")
        ax.set_title(f"{sym}", fontsize=11, fontweight="bold")
        ax.set_ylabel("Equity (rebased 100)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.tick_params(axis="x", rotation=30)
        ax.grid(True, alpha=0.3)

    # combined portfolio in last panel
    ax_port = axes[2][1]
    if not portfolio_curve.empty:
        norm_p = portfolio_curve / portfolio_curve.iloc[0] * 100
        ax_port.plot(portfolio_curve.index, norm_p, color="#212121", linewidth=2)
        ax_port.axhline(100, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
        ax_port.fill_between(portfolio_curve.index, 100, norm_p,
                             where=(norm_p >= 100), alpha=0.15, color="green")
        ax_port.fill_between(portfolio_curve.index, 100, norm_p,
                             where=(norm_p < 100), alpha=0.15, color="red")
    ax_port.set_title("Combined Portfolio", fontsize=11, fontweight="bold")
    ax_port.set_ylabel("Equity (rebased 100)")
    ax_port.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_port.xaxis.set_major_locator(mdates.MonthLocator())
    ax_port.tick_params(axis="x", rotation=30)
    ax_port.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info("Chart saved → %s", out_path)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL_TIMEFRAME = {
    "SPY":     "15Min",
    "QQQ":     "15Min",
    "BTC/USD": "1Hour",
    "GLD":     "4Hour",
    "USO":     "4Hour",
}

SYMBOL_STRATEGY = {
    "SPY":     "mean_reversion",
    "QQQ":     "mean_reversion",
    "BTC/USD": "momentum_breakout",
    "GLD":     "trend_following",
    "USO":     "trend_following",
}


def run():
    os.makedirs("logs", exist_ok=True)
    api = _build_api()

    # ── fetch data ────────────────────────────────────────────────────────
    all_bars: dict[str, pd.DataFrame] = {}
    for sym in INSTRUMENTS:
        tf = SYMBOL_TIMEFRAME[sym]
        try:
            all_bars[sym] = _fetch_history(api, sym, tf)
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", sym, exc)
            sys.exit(1)

    # ── simulate each instrument ──────────────────────────────────────────
    per_trades: dict[str, list[Trade]] = {}
    per_curves: dict[str, pd.Series] = {}

    for sym, bars in all_bars.items():
        strategy = SYMBOL_STRATEGY[sym]
        logger.info("Simulating %s (%s)…", sym, strategy)
        trades, curve = _simulate_instrument(sym, bars, strategy, INITIAL_EQUITY)
        per_trades[sym] = trades
        per_curves[sym] = curve

    # ── portfolio simulation ──────────────────────────────────────────────
    portfolio_curve = _simulate_portfolio(all_bars, per_trades)

    # ── metrics ───────────────────────────────────────────────────────────
    rows = []
    flags = []
    for sym in INSTRUMENTS:
        m = _metrics(per_trades[sym], per_curves[sym], sym)
        rows.append(m)
        if m["flag"]:
            flags.append(f"  {sym}: {m['flag']}")

    port_trades_all = [t for tlist in per_trades.values() for t in tlist]
    port_m = _metrics(port_trades_all, portfolio_curve, "PORTFOLIO")
    rows.append(port_m)
    if port_m["flag"]:
        flags.append(f"  PORTFOLIO: {port_m['flag']}")

    # ── print table ───────────────────────────────────────────────────────
    headers = {
        "symbol": "Symbol",
        "trades": "Trades",
        "win_rate": "Win %",
        "avg_win": "Avg Win $",
        "avg_loss": "Avg Loss $",
        "profit_factor": "Prof. Factor",
        "max_drawdown_pct": "Max DD %",
        "sharpe": "Sharpe",
        "total_return_pct": "Return %",
    }
    table_rows = [{headers[k]: v for k, v in r.items() if k in headers} for r in rows]

    print("\n" + "=" * 70)
    print("  BACKTEST SUMMARY — 6-Month Simulation (Slippage: 0.05%/trade)")
    print("=" * 70)
    print(tabulate(table_rows, headers="keys", tablefmt="rounded_outline", floatfmt=".2f"))

    if flags:
        print("\n⚠  STRATEGY FLAGS (Negative Sharpe — consider parameter tuning):")
        for f in flags:
            print(f)
    else:
        print("\n✓  All strategies have non-negative Sharpe ratios.")

    print(f"\nEquity curve chart → backtest_results.png\n")

    # ── chart ─────────────────────────────────────────────────────────────
    _plot(per_curves, portfolio_curve)


if __name__ == "__main__":
    # Install tabulate if missing
    try:
        from tabulate import tabulate  # noqa: F811
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "tabulate", "-q"])
        from tabulate import tabulate  # noqa: F811

    run()
