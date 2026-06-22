-- Trading bot Supabase schema.
-- Run once in the Supabase SQL Editor (Project → SQL Editor → New query).
-- Safe to re-run — all statements are idempotent.

-- Closed trades (one row per filled exit)
CREATE TABLE IF NOT EXISTS trades (
  id              BIGSERIAL PRIMARY KEY,
  timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
  instrument      TEXT NOT NULL,
  direction       TEXT NOT NULL,
  entry_price     NUMERIC(18,6),
  exit_price      NUMERIC(18,6),
  pnl             NUMERIC(12,2),
  position_size   NUMERIC(12,4)
);

-- Daily P&L summary (keyed on date; bot upserts once at market close)
CREATE TABLE IF NOT EXISTS daily_pnl (
  date            DATE PRIMARY KEY,
  starting_equity NUMERIC(12,2),
  ending_equity   NUMERIC(12,2),
  pnl             NUMERIC(12,2),
  pnl_pct         NUMERIC(8,4)
);

-- Live open positions (snapshot replaced on every bot loop)
CREATE TABLE IF NOT EXISTS positions (
  symbol            TEXT PRIMARY KEY,
  direction         TEXT,
  entry_price       NUMERIC(18,6),
  qty               NUMERIC(12,4),
  hard_stop         NUMERIC(18,6),
  trailing_stop     NUMERIC(18,6),
  trailing_distance NUMERIC(18,6),
  alpaca_order_id   TEXT,
  opened_at         TIMESTAMPTZ,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Live bot activity log (heartbeat every 5 min + trade events + warnings/errors)
CREATE TABLE IF NOT EXISTS bot_events (
  id      BIGSERIAL PRIMARY KEY,
  ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  level   TEXT NOT NULL,
  message TEXT NOT NULL
);

-- Enable Row Level Security on all tables
ALTER TABLE trades     ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_pnl  ENABLE ROW LEVEL SECURITY;
ALTER TABLE positions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_events ENABLE ROW LEVEL SECURITY;

-- Anon key (embedded in the browser dashboard) can only SELECT.
-- service_role key (GitHub Actions secret) bypasses RLS and can write.
DROP POLICY IF EXISTS "anon_read_trades"     ON trades;
DROP POLICY IF EXISTS "anon_read_daily_pnl"  ON daily_pnl;
DROP POLICY IF EXISTS "anon_read_positions"  ON positions;

DROP POLICY IF EXISTS "anon_read_bot_events" ON bot_events;

CREATE POLICY "anon_read_trades"     ON trades     FOR SELECT USING (true);
CREATE POLICY "anon_read_daily_pnl"  ON daily_pnl  FOR SELECT USING (true);
CREATE POLICY "anon_read_positions"  ON positions  FOR SELECT USING (true);
CREATE POLICY "anon_read_bot_events" ON bot_events FOR SELECT USING (true);
