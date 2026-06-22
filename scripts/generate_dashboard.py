#!/usr/bin/env python3
"""Generate a static monitoring dashboard from trading bot log files.

Reads logs/*.csv, logs/positions.json, and logs/bot.log, then writes
docs/index.html with all data embedded as JSON for instant load.

If SUPABASE_URL + SUPABASE_ANON_KEY env vars are set, the page JS will
also fetch live data from Supabase every 5 min and re-render automatically.
"""

import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

LOG_DIR = "logs"
OUT_DIR = "docs"
TZ = ZoneInfo("America/New_York")


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_log_tail(path: str, lines: int = 150) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path) as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception:
        return ""


def _compute_stats(trades: list[dict]) -> dict:
    pnls = []
    for t in trades:
        try:
            pnls.append(float(t["pnl"]))
        except (KeyError, ValueError):
            pass

    if not pnls:
        return {
            "total_trades": 0, "win_count": 0, "loss_count": 0,
            "win_rate": 0, "avg_win": 0, "avg_loss": 0,
            "profit_factor": 0, "total_pnl": 0,
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "total_trades": len(pnls),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
        "total_pnl": round(sum(pnls), 2),
    }


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trading Bot Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    .pnl-pos { color: #4ade80; font-weight: 600; }
    .pnl-neg { color: #f87171; font-weight: 600; }
    .live-badge { font-size: 9px; background: #166534; color: #4ade80; border-radius: 4px; padding: 1px 4px; vertical-align: middle; }
  </style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen font-mono text-sm">
<script>window.__DATA__ = DATA_JSON_PLACEHOLDER;</script>

<div class="max-w-7xl mx-auto p-4 space-y-4">

  <!-- Header -->
  <div class="flex items-center justify-between">
    <h1 class="text-xl font-bold">Trading Bot <span class="text-green-400">Monitor</span></h1>
    <div class="text-right text-xs text-gray-400">
      <div>Data: <span id="gen-at"></span></div>
      <div>Prices: <span id="price-at" class="text-green-400">fetching…</span></div>
    </div>
  </div>

  <!-- Summary Cards -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Account Equity</div>
      <div class="text-xl font-bold text-white" id="c-equity">—</div>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Today's P&amp;L</div>
      <div class="text-xl font-bold" id="c-today-pnl">—</div>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Open Positions</div>
      <div class="text-xl font-bold text-white" id="c-open">—</div>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Total Trades</div>
      <div class="text-xl font-bold text-white" id="c-trades">—</div>
    </div>
  </div>

  <!-- Charts -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div class="bg-gray-800 rounded-xl p-4">
      <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">Equity Curve</h2>
      <div id="eq-empty" class="text-gray-500 text-xs">No data yet.</div>
      <canvas id="equity-chart" style="display:none"></canvas>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">Daily P&amp;L</h2>
      <div id="pnl-empty" class="text-gray-500 text-xs">No data yet.</div>
      <canvas id="pnl-chart" style="display:none"></canvas>
    </div>
  </div>

  <!-- Stats -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Win Rate</div>
      <div class="text-lg font-bold text-white" id="s-winrate">—</div>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Avg Win</div>
      <div class="text-lg font-bold pnl-pos" id="s-avgwin">—</div>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Avg Loss</div>
      <div class="text-lg font-bold pnl-neg" id="s-avgloss">—</div>
    </div>
    <div class="bg-gray-800 rounded-xl p-4">
      <div class="text-xs text-gray-400 mb-1">Profit Factor</div>
      <div class="text-lg font-bold text-white" id="s-pf">—</div>
    </div>
  </div>

  <!-- Open Positions -->
  <div class="bg-gray-800 rounded-xl p-4">
    <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
      Open Positions <span class="live-badge">LIVE PRICES</span>
    </h2>
    <p id="pos-empty" class="text-gray-500 text-xs">No open positions.</p>
    <div class="overflow-x-auto" id="pos-wrap" style="display:none">
      <table class="w-full text-xs">
        <thead><tr class="text-gray-500 border-b border-gray-700">
          <th class="text-left pb-2 pr-4">Symbol</th>
          <th class="text-left pb-2 pr-4">Dir</th>
          <th class="text-right pb-2 pr-4">Qty</th>
          <th class="text-right pb-2 pr-4">Entry</th>
          <th class="text-right pb-2 pr-4">Live Price</th>
          <th class="text-right pb-2 pr-4">Unrealised P&amp;L</th>
          <th class="text-right pb-2 pr-4">Hard Stop</th>
          <th class="text-right pb-2 pr-4">Trail Stop</th>
          <th class="text-right pb-2">Held</th>
        </tr></thead>
        <tbody id="pos-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Recent Trades -->
  <div class="bg-gray-800 rounded-xl p-4">
    <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">Recent Trades</h2>
    <p id="tr-empty" class="text-gray-500 text-xs">No closed trades yet.</p>
    <div class="overflow-x-auto" id="tr-wrap" style="display:none">
      <table class="w-full text-xs">
        <thead><tr class="text-gray-500 border-b border-gray-700">
          <th class="text-left pb-2 pr-4">Time</th>
          <th class="text-left pb-2 pr-4">Symbol</th>
          <th class="text-left pb-2 pr-4">Dir</th>
          <th class="text-right pb-2 pr-4">Qty</th>
          <th class="text-right pb-2 pr-4">Entry</th>
          <th class="text-right pb-2 pr-4">Exit</th>
          <th class="text-right pb-2">P&amp;L</th>
        </tr></thead>
        <tbody id="tr-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Bot Log -->
  <div class="bg-gray-800 rounded-xl p-4">
    <button onclick="toggleLog()" class="text-xs font-semibold text-gray-400 uppercase tracking-wide flex items-center gap-2 w-full text-left">
      <span id="log-icon">&#9654;</span> Bot Log (last 150 lines)
    </button>
    <div id="log-wrap" class="hidden mt-3">
      <pre id="log-pre" class="text-xs text-green-300 bg-gray-900 rounded p-3 max-h-96 overflow-y-auto whitespace-pre-wrap break-all"></pre>
    </div>
  </div>

  <p class="text-center text-xs text-gray-600 pb-2">Paper trading only &middot; Alpaca Markets &middot; Prices live every 60 s</p>
</div>

<script>
// ── Config ────────────────────────────────────────────────────────────────────
const SUPABASE_URL  = 'SUPABASE_URL_PLACEHOLDER';
const SUPABASE_ANON = 'SUPABASE_ANON_PLACEHOLDER';
const $ = id => document.getElementById(id);
let currentData = window.__DATA__;
let _eqChart = null, _pnlChart = null;

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt$(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return '$' + n.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function sign$(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return (n >= 0 ? '+' : '') + fmt$(n);
}
function pnlCls(v) { return parseFloat(v) >= 0 ? 'pnl-pos' : 'pnl-neg'; }
function fmtTs(iso) { return iso ? iso.slice(0, 16).replace('T', ' ') : '—'; }
function heldSince(iso) {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}
function computeStats(trades) {
  const pnls = trades.map(t => parseFloat(t.pnl)).filter(v => !isNaN(v));
  if (!pnls.length) return {total_trades:0,win_count:0,loss_count:0,win_rate:0,avg_win:0,avg_loss:0,profit_factor:0,total_pnl:0};
  const wins   = pnls.filter(v => v > 0);
  const losses = pnls.filter(v => v <= 0);
  const gp = wins.reduce((a,b) => a+b, 0);
  const gl = Math.abs(losses.reduce((a,b) => a+b, 0));
  return {
    total_trades:  pnls.length,
    win_count:     wins.length,
    loss_count:    losses.length,
    win_rate:      +(wins.length / pnls.length * 100).toFixed(1),
    avg_win:       wins.length   ? +(gp / wins.length).toFixed(2) : 0,
    avg_loss:      losses.length ? +(losses.reduce((a,b)=>a+b,0)/losses.length).toFixed(2) : 0,
    profit_factor: gl > 0 ? +(gp / gl).toFixed(2) : 0,
    total_pnl:     +pnls.reduce((a,b)=>a+b,0).toFixed(2),
  };
}

// ── Chart defaults ────────────────────────────────────────────────────────────
const CHART_OPTS = {
  responsive: true,
  plugins: { legend: { display: false } },
  scales: {
    x: { ticks: { color:'#9ca3af', maxTicksLimit:8, font:{size:10} }, grid:{color:'#374151'} },
    y: { ticks: { color:'#9ca3af', font:{size:10}, callback: v => '$'+v.toLocaleString() }, grid:{color:'#374151'} },
  },
};

// ── Full re-renderable dashboard ──────────────────────────────────────────────
function renderDashboard(D) {
  currentData = D;
  const daily = D.daily_pnl || [];
  const stats = D.stats || computeStats(D.trades || []);

  // Summary cards
  const last = daily.length ? daily[daily.length - 1] : null;
  $('c-equity').textContent = last ? fmt$(last.ending_equity) : '—';
  const todayEl = $('c-today-pnl');
  if (last) { todayEl.textContent = sign$(last.pnl); todayEl.className = 'text-xl font-bold ' + pnlCls(last.pnl); }
  else      { todayEl.textContent = '—';             todayEl.className = 'text-xl font-bold'; }
  $('c-open').textContent   = Object.keys(D.positions || {}).length;
  $('c-trades').textContent = stats.total_trades;

  // Stats
  $('s-winrate').textContent = stats.total_trades ? `${stats.win_rate}% (${stats.win_count}W / ${stats.loss_count}L)` : '—';
  $('s-avgwin').textContent  = stats.avg_win  ? fmt$(stats.avg_win)  : '—';
  $('s-avgloss').textContent = stats.avg_loss ? fmt$(stats.avg_loss) : '—';
  $('s-pf').textContent = stats.profit_factor || '—';

  // Charts — destroy before recreating on same canvas
  if (_eqChart)  { _eqChart.destroy();  _eqChart  = null; }
  if (_pnlChart) { _pnlChart.destroy(); _pnlChart = null; }
  const eqC = $('equity-chart'), pnlC = $('pnl-chart');
  if (daily.length > 0) {
    $('eq-empty').style.display  = 'none'; eqC.style.display  = '';
    $('pnl-empty').style.display = 'none'; pnlC.style.display = '';
    _eqChart = new Chart(eqC, { type:'line', data:{
      labels: daily.map(d => d.date),
      datasets:[{ data: daily.map(d => parseFloat(d.ending_equity)),
        borderColor:'#4ade80', backgroundColor:'rgba(74,222,128,0.07)',
        borderWidth:2, fill:true, tension:0.3,
        pointRadius: daily.length > 30 ? 0 : 3 }]
    }, options: CHART_OPTS });
    const pnls = daily.map(d => parseFloat(d.pnl));
    _pnlChart = new Chart(pnlC, { type:'bar', data:{
      labels: daily.map(d => d.date),
      datasets:[{ data: pnls,
        backgroundColor: pnls.map(v => v >= 0 ? 'rgba(74,222,128,0.75)' : 'rgba(248,113,113,0.75)'),
        borderRadius: 3 }]
    }, options: CHART_OPTS });
  } else {
    $('eq-empty').style.display  = ''; eqC.style.display  = 'none';
    $('pnl-empty').style.display = ''; pnlC.style.display = 'none';
  }

  // Positions table
  const posKeys = Object.keys(D.positions || {});
  if (posKeys.length > 0) {
    $('pos-empty').style.display = 'none';
    $('pos-wrap').style.display  = '';
    const tbody = $('pos-body');
    tbody.innerHTML = '';
    posKeys.forEach(sym => {
      const p = D.positions[sym];
      const safeSym = sym.replace('/', '_');
      const dCls = p.direction === 'long' ? 'text-green-400' : 'text-red-400';
      tbody.insertAdjacentHTML('beforeend',
        `<tr class="border-b border-gray-700">
          <td class="py-2 pr-4 font-semibold">${sym}</td>
          <td class="py-2 pr-4 ${dCls} uppercase">${p.direction}</td>
          <td class="py-2 pr-4 text-right">${p.qty}</td>
          <td class="py-2 pr-4 text-right">${fmt$(p.entry_price)}</td>
          <td class="py-2 pr-4 text-right text-yellow-300" id="lp-${safeSym}">…</td>
          <td class="py-2 pr-4 text-right" id="up-${safeSym}">…</td>
          <td class="py-2 pr-4 text-right text-red-400">${fmt$(p.hard_stop)}</td>
          <td class="py-2 pr-4 text-right text-yellow-400">${p.trailing_stop != null ? fmt$(p.trailing_stop) : '—'}</td>
          <td class="py-2 text-right text-gray-400">${heldSince(p.opened_at)}</td>
        </tr>`);
    });
  } else {
    $('pos-empty').style.display = '';
    $('pos-wrap').style.display  = 'none';
    $('pos-body').innerHTML      = '';
  }

  // Trades table (show latest 30, most-recent first)
  const trades = (D.trades || []).slice().reverse().slice(0, 30);
  if (trades.length > 0) {
    $('tr-empty').style.display = 'none';
    $('tr-wrap').style.display  = '';
    const tbody = $('tr-body');
    tbody.innerHTML = '';
    trades.forEach(t => {
      const dCls = t.direction === 'long' ? 'text-green-400' : 'text-red-400';
      tbody.insertAdjacentHTML('beforeend',
        `<tr class="border-b border-gray-700">
          <td class="py-2 pr-4 text-gray-400">${fmtTs(t.timestamp)}</td>
          <td class="py-2 pr-4 font-semibold">${t.instrument}</td>
          <td class="py-2 pr-4 ${dCls} uppercase">${t.direction}</td>
          <td class="py-2 pr-4 text-right">${t.position_size}</td>
          <td class="py-2 pr-4 text-right">${fmt$(t.entry_price)}</td>
          <td class="py-2 pr-4 text-right">${fmt$(t.exit_price)}</td>
          <td class="py-2 text-right ${pnlCls(t.pnl)}">${sign$(t.pnl)}</td>
        </tr>`);
    });
  } else {
    $('tr-empty').style.display = '';
    $('tr-wrap').style.display  = 'none';
    $('tr-body').innerHTML      = '';
  }

  // Log — only update from static data (Supabase doesn't store log lines)
  if (D.log_tail !== undefined) {
    $('log-pre').textContent = D.log_tail || 'No log data yet.';
  }
}

// ── Live price fetching ───────────────────────────────────────────────────────
const GECKO_IDS = {
  'BTC/USD':'bitcoin', 'ETH/USD':'ethereum',
  'SOL/USD':'solana',  'DOGE/USD':'dogecoin',
};
const YAHOO_SYMS = ['SPY','QQQ','GLD','USO'];

async function fetchLivePrices() {
  const prices = {};
  try {
    const ids = [...new Set(Object.values(GECKO_IDS))].join(',');
    const r = await fetch(
      `https://api.coingecko.com/api/v3/simple/price?ids=${ids}&vs_currencies=usd`,
      { signal: AbortSignal.timeout(8000) }
    );
    const d = await r.json();
    for (const [sym, id] of Object.entries(GECKO_IDS))
      if (d[id]?.usd) prices[sym] = d[id].usd;
  } catch {}
  try {
    const r = await fetch(
      `https://query1.finance.yahoo.com/v7/finance/quote?symbols=${YAHOO_SYMS.join(',')}`,
      { signal: AbortSignal.timeout(8000) }
    );
    const d = await r.json();
    for (const q of d?.quoteResponse?.result ?? [])
      if (q.regularMarketPrice) prices[q.symbol] = q.regularMarketPrice;
  } catch {}
  return prices;
}

async function refreshPrices() {
  const prices = await fetchLivePrices();
  const now = new Date().toLocaleTimeString();
  let anyLive = false;
  for (const sym of Object.keys(currentData.positions || {})) {
    const safeSym = sym.replace('/', '_');
    const lpEl = $('lp-' + safeSym);
    const upEl = $('up-' + safeSym);
    if (!lpEl) continue;
    const price = prices[sym];
    if (price == null) { lpEl.textContent = '—'; upEl.textContent = '—'; continue; }
    anyLive = true;
    lpEl.textContent = fmt$(price);
    const p = currentData.positions[sym];
    const qty   = parseFloat(p.qty);
    const entry = parseFloat(p.entry_price);
    const upnl  = p.direction === 'long' ? (price - entry) * qty : (entry - price) * qty;
    upEl.textContent = sign$(upnl);
    upEl.className   = 'py-2 pr-4 text-right ' + pnlCls(upnl);
  }
  $('price-at').textContent = anyLive ? now : 'unavailable';
}

// ── Supabase live data ────────────────────────────────────────────────────────
async function fetchSupabaseData() {
  if (!SUPABASE_URL || SUPABASE_URL === 'SUPABASE_URL_PLACEHOLDER') return null;
  const h = { apikey: SUPABASE_ANON, Authorization: `Bearer ${SUPABASE_ANON}` };
  const opts = (url) => ({ headers: h, signal: AbortSignal.timeout(10000) });
  try {
    const [tRes, pnlRes, posRes] = await Promise.all([
      fetch(`${SUPABASE_URL}/rest/v1/trades?order=timestamp.asc&limit=500&select=*`,   opts()),
      fetch(`${SUPABASE_URL}/rest/v1/daily_pnl?order=date.asc&select=*`,               opts()),
      fetch(`${SUPABASE_URL}/rest/v1/positions?select=*`,                               opts()),
    ]);
    if (!tRes.ok || !pnlRes.ok || !posRes.ok) return null;
    const trades    = await tRes.json();
    const daily_pnl = await pnlRes.json();
    const posArr    = await posRes.json();
    const positions = {};
    for (const {symbol, updated_at, ...rest} of posArr) positions[symbol] = rest;
    return {
      trades, daily_pnl, positions,
      stats:        computeStats(trades),
      log_tail:     undefined,              // keep the static log; Supabase has no log table
      generated_at: window.__DATA__.generated_at,
    };
  } catch { return null; }
}

// ── Init ──────────────────────────────────────────────────────────────────────
$('gen-at').textContent = window.__DATA__.generated_at;
renderDashboard(window.__DATA__);

(async () => {
  // Fetch live Supabase data immediately on load; re-render if newer
  const live = await fetchSupabaseData();
  if (live) {
    $('gen-at').textContent = '✓ LIVE ' + new Date().toLocaleTimeString();
    renderDashboard(live);
  }

  // Market-price refresh every 60 s
  refreshPrices();
  setInterval(refreshPrices, 60_000);

  // Supabase data refresh every 5 min (keeps open sessions current without page reload)
  if (SUPABASE_URL && SUPABASE_URL !== 'SUPABASE_URL_PLACEHOLDER') {
    setInterval(async () => {
      const fresh = await fetchSupabaseData();
      if (fresh) {
        $('gen-at').textContent = '✓ LIVE ' + new Date().toLocaleTimeString();
        renderDashboard(fresh);
      }
    }, 300_000);
  }
})();

function toggleLog() {
  const wrap = $('log-wrap');
  const hidden = wrap.classList.toggle('hidden');
  $('log-icon').innerHTML = hidden ? '&#9654;' : '&#9660;';
  if (!hidden) { const pre = $('log-pre'); pre.scrollTop = pre.scrollHeight; }
}
</script>
</body>
</html>
"""


def main():
    trades = _read_csv(f"{LOG_DIR}/trades.csv")
    daily_pnl = _read_csv(f"{LOG_DIR}/daily_pnl.csv")
    positions = _read_json(f"{LOG_DIR}/positions.json")
    log_tail = _read_log_tail(f"{LOG_DIR}/bot.log")
    stats = _compute_stats(trades)

    data = {
        "trades": trades[-30:],
        "daily_pnl": daily_pnl,
        "positions": positions,
        "log_tail": log_tail,
        "stats": stats,
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z"),
    }

    data_json = json.dumps(data, ensure_ascii=False)
    html = HTML_TEMPLATE.replace("DATA_JSON_PLACEHOLDER", data_json)

    # Inject Supabase anon key (read-only via RLS — safe to embed in browser HTML)
    supabase_url  = os.environ.get("SUPABASE_URL", "")
    supabase_anon = os.environ.get("SUPABASE_ANON_KEY", "")
    html = html.replace("SUPABASE_URL_PLACEHOLDER", supabase_url)
    html = html.replace("SUPABASE_ANON_PLACEHOLDER", supabase_anon)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = f"{OUT_DIR}/index.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard written to {out_path} ({len(html):,} bytes)")
    print(f"  Trades: {len(trades)}  |  Daily rows: {len(daily_pnl)}  |  Open positions: {len(positions)}")


if __name__ == "__main__":
    main()
