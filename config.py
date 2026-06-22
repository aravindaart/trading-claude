import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

INSTRUMENTS = {
    "SPY":      {"strategy": "mean_reversion",    "asset_class": "us_equity"},
    "QQQ":      {"strategy": "mean_reversion",    "asset_class": "us_equity"},
    "BTC/USD":  {"strategy": "momentum_breakout", "asset_class": "crypto"},
    "ETH/USD":  {"strategy": "momentum_breakout", "asset_class": "crypto"},
    "SOL/USD":  {"strategy": "momentum_breakout", "asset_class": "crypto"},
    "DOGE/USD": {"strategy": "momentum_breakout", "asset_class": "crypto"},
    "GLD":      {"strategy": "trend_following",   "asset_class": "us_equity"},
    "USO":      {"strategy": "trend_following",   "asset_class": "us_equity"},
}

MEAN_REVERSION = {
    "timeframe": "15Min",
    "lookback_periods": 20,
    # Raised from 1.5/1.8 — require stronger deviation before entry so avg
    # loss no longer swamps avg win; filters the frequent small-noise signals
    # that were killing the win/loss ratio.
    "std_multiplier": {"SPY": 2.2, "QQQ": 2.5},
}

MOMENTUM_BREAKOUT = {
    "timeframe": "1Hour",
    "lookback_periods": 20,
    # Volume filter raised 1.5→2.0 to eliminate low-conviction breakouts
    # (was causing 27% win rate and >15% drawdown on BTC).
    "volume_multiplier": 2.0,
    # Trailing stop tightened 2.0→1.5 ATR to lock in gains faster on crypto.
    "atr_trailing_stop": 1.5,
    "atr_period": 14,
}

TREND_FOLLOWING = {
    "timeframe": "4Hour",
    # Reduced from 50/200 — the 200-period EMA needs 800h of 4h bars to warm
    # up, leaving almost no cross signals in a 6-month window. 21/100 is a
    # well-known responsive EMA pair that still filters noise on 4h candles.
    "fast_ema": 21,
    "slow_ema": 100,
    # Tightened trailing stop 3.0→2.0 ATR to protect equity on GLD/USO.
    "atr_trailing_stop": 2.0,
    "atr_period": 14,
}

RISK = {
    "atr_period": 14,
    "risk_per_trade_pct": 0.01,   # 1% of equity per ATR move
    # Halved from 1% — cuts mean-reversion losers faster before they compound.
    "hard_stop_pct": 0.005,
    # Circuit breaker: halt new entries for the rest of the day once daily loss hits 3%.
    "max_daily_loss_pct": 0.03,
}

LOG_DIR = "logs"
TRADES_CSV = f"{LOG_DIR}/trades.csv"
DAILY_PNL_CSV = f"{LOG_DIR}/daily_pnl.csv"

EQUITY_MARKET_OPEN = "09:30"
EQUITY_MARKET_CLOSE = "16:00"
TIMEZONE = "America/New_York"

POLL_INTERVAL_SECONDS = 60
