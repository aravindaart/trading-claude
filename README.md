# Alpaca Multi-Strategy Trading Bot

Trades SPY, QQQ (mean reversion), BTC/USD (momentum breakout), GLD, USO (trend following) simultaneously.

## Setup

```bash
pip install -r requirements.txt
cp .env .env.local   # edit with your Alpaca keys
```

Edit `.env`:
```
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # paper trading
# ALPACA_BASE_URL=https://api.alpaca.markets       # live trading
```

## Run

```bash
python -m bot.main
```

Logs are written to `logs/`:
- `bot.log` — live activity
- `trades.csv` — every completed trade
- `daily_pnl.csv` — end-of-day equity summary

## File Structure

```
config.py                          – all tuneable parameters
bot/
  main.py                          – main loop, wires everything together
  risk_manager.py                  – ATR position sizing, hard stop, correlation filter
  portfolio.py                     – order submission, position tracking, CSV logging
  strategies/
    mean_reversion.py              – SPY / QQQ (15-min candles, Bollinger-style bands)
    momentum_breakout.py           – BTC/USD (1-hour candles, 20-period high/low breakout)
    trend_following.py             – GLD / USO (4-hour candles, 50/200 EMA crossover)
logs/
  trades.csv
  daily_pnl.csv
  bot.log
```

## Risk Rules

| Rule | Value |
|------|-------|
| ATR period | 14 |
| Risk per trade | 1% of equity per 1 ATR move |
| Hard stop | 1% of account equity |
| Correlation filter | No BTC/USD long if SPY + QQQ both long |
| Equities trading hours | NYSE market hours only |
| Crypto trading hours | 24/7 |
