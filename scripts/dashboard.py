"""Web dashboard for monitoring both Polymarket and Crypto trading.

Features:
- Tabbed view: Crypto Scalper / Polymarket
- Real-time P&L charts
- Open positions with live prices
- Trade history
- Activity logs
"""

import csv
import hmac
import json
import os
import secrets
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, render_template_string, request, Response, jsonify

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DIR = os.path.join(BASE_DIR, "paper_trading")
PAPER_STATE = os.path.join(PAPER_DIR, "state.json")
PAPER_TRADES = os.path.join(PAPER_DIR, "trades.csv")
PAPER_LOG_FILE = os.path.join(PAPER_DIR, "paper_trading.log")

CRYPTO_DIR = os.path.join(BASE_DIR, "crypto_trading")
CRYPTO_STATE = os.path.join(CRYPTO_DIR, "state.json")
CRYPTO_TRADES = os.path.join(CRYPTO_DIR, "trades.csv")
CRYPTO_LOG_FILE = os.path.join(CRYPTO_DIR, "scalper.log")

STOCK_DIR = os.path.join(BASE_DIR, "stock_trading")
STOCK_STATE = os.path.join(STOCK_DIR, "state.json")
STOCK_TRADES = os.path.join(STOCK_DIR, "trades.csv")
STOCK_LOG_FILE = os.path.join(STOCK_DIR, "stock_trader.log")

# Auth
DASH_USER = os.getenv("DASH_USER", "admin")
DASH_PASS = os.getenv("DASH_PASS", "changeme123")
if not os.getenv("DASH_USER") or not os.getenv("DASH_PASS"):
    print("WARNING: DASH_USER/DASH_PASS not set in environment. Using insecure defaults!")

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)


def check_auth(username, password):
    return (hmac.compare_digest(username, DASH_USER) and
            hmac.compare_digest(password, DASH_PASS))


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="Trading Bot"'},
            )
        return f(*args, **kwargs)
    return decorated


def load_json_state(path, defaults=None):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return defaults or {}


def load_csv_log(path):
    trades = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trades.append(row)
        except Exception:
            pass
    return trades


def load_recent_logs(path, n=50):
    lines = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception:
            pass
    return lines[-n:]


def load_paper_state():
    return load_json_state(PAPER_STATE, {
        "bankroll": 50.0, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
        "positions": [], "closed_trades": [], "total_trades": 0,
        "winning_trades": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
        "daily_trade_count": 0, "last_scan": "",
    })


def load_crypto_state():
    return load_json_state(CRYPTO_STATE, {
        "bankroll": 50.0, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
        "positions": [], "closed_trades": [], "total_trades": 0,
        "winning_trades": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
        "daily_trade_count": 0, "last_scan": "", "last_loss_time": 0,
    })


def load_stock_state():
    return load_json_state(STOCK_STATE, {
        "bankroll": 1000.0, "starting_bankroll": 1000.0, "peak_bankroll": 1000.0,
        "positions": [], "closed_trades": [], "total_trades": 0,
        "winning_trades": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
        "daily_trade_count": 0, "last_scan": "", "last_loss_time": 0,
        "regime": "bull",
    })


def compute_pnl_timeline(closed_trades, pnl_key="pnl"):
    timeline = []
    cumulative = 0.0
    for t in closed_trades:
        try:
            pnl = float(t.get(pnl_key, 0))
            cumulative += pnl
            ts = t.get("closed_at", t.get("timestamp", ""))[:16]
            timeline.append({"time": ts, "pnl": round(cumulative, 4)})
        except (ValueError, TypeError):
            pass
    return timeline


def compute_pnl_from_csv(csv_path, pnl_col="pnl", time_col="timestamp"):
    """Build cumulative P&L timeline from full CSV trade history."""
    timeline = []
    cumulative = 0.0
    trades = load_csv_log(csv_path)
    for t in trades:
        if t.get("action", "").upper() == "CLOSE":
            try:
                pnl = float(t.get(pnl_col, 0))
                cumulative += pnl
                ts = t.get(time_col, "")[:16]
                timeline.append({"time": ts, "pnl": round(cumulative, 4)})
            except (ValueError, TypeError):
                pass
    return timeline


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Dashboard</title>
<meta http-equiv="refresh" content="15">
<script>
// Persist UI state across refreshes
window.addEventListener('beforeunload', function() {
  if (typeof currentPair !== 'undefined' && currentPair) localStorage.setItem('ds_pair', currentPair);
  if (typeof currentGranularity !== 'undefined') localStorage.setItem('ds_gran', currentGranularity);
  if (typeof currentLimit !== 'undefined') localStorage.setItem('ds_limit', currentLimit);
  const activeTab = document.querySelector('.tab-content.active');
  if (activeTab) localStorage.setItem('ds_tab', activeTab.id);
});
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', Roboto, monospace; background: #0a0e17; color: #c9d1d9; }

  .topbar { background: #161b22; border-bottom: 1px solid #21262d; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; }
  .topbar h1 { font-size: 1.1em; color: #58a6ff; letter-spacing: 1px; }
  .topbar .meta { font-size: 0.75em; color: #8b949e; }

  /* Tabs */
  .tabs { display: flex; background: #161b22; border-bottom: 2px solid #21262d; padding: 0 24px; }
  .tab { padding: 12px 24px; cursor: pointer; color: #8b949e; font-size: 0.85em; font-weight: 600; letter-spacing: 0.5px;
         border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; }
  .tab:hover { color: #c9d1d9; }
  .tab.active { color: #58a6ff; border-bottom-color: #58a6ff; }
  .tab .badge-count { background: #21262d; color: #8b949e; padding: 1px 6px; border-radius: 10px; font-size: 0.75em; margin-left: 6px; }
  .tab.active .badge-count { background: #0d2744; color: #58a6ff; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

  /* Stats grid */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 20px; }
  .stat { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px; }
  .stat .label { font-size: 0.6em; text-transform: uppercase; letter-spacing: 1.5px; color: #8b949e; margin-bottom: 3px; }
  .stat .val { font-size: 1.4em; font-weight: 700; }

  .g { color: #3fb950; } .r { color: #f85149; } .y { color: #d29922; } .b { color: #58a6ff; } .n { color: #8b949e; }

  /* Charts */
  .charts { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .chart-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; }
  .chart-card h3 { font-size: 0.8em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  .chart-wrap { position: relative; height: 200px; }

  /* Tables */
  .section { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .section h2 { font-size: 0.85em; color: #58a6ff; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.75em; }
  th { text-align: left; color: #8b949e; padding: 7px 5px; border-bottom: 1px solid #21262d; font-size: 0.7em; text-transform: uppercase; letter-spacing: 1px; }
  td { padding: 6px 5px; border-bottom: 1px solid #161b22; }
  tr:hover { background: #1c2333; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; }
  .badge-paper { background: #1c1d5e; color: #a5b4fc; border: 1px solid #6366f1; }
  .badge-live { background: #0d2818; color: #3fb950; border: 1px solid #238636; }
  .badge-crypto { background: #2d1f0e; color: #d29922; border: 1px solid #9e6a03; }
  .badge-high { background: #0d2818; color: #3fb950; border: 1px solid #238636; }
  .badge-medium { background: #2d1f0e; color: #d29922; border: 1px solid #9e6a03; }
  .badge-low { background: #2d1117; color: #f85149; border: 1px solid #da3633; }

  .model-card { background: linear-gradient(135deg, #0d1f2d, #161b22); border: 1px solid #1f6feb33; border-radius: 8px; padding: 14px 20px; margin-bottom: 20px; display: flex; gap: 30px; align-items: center; flex-wrap: wrap; }
  .model-card .mc-label { font-size: 0.6em; text-transform: uppercase; letter-spacing: 1.5px; color: #58a6ff; }
  .model-card .mc-val { font-size: 1.1em; font-weight: 700; color: #c9d1d9; }

  .log-box { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 10px 12px; font-size: 0.65em; max-height: 220px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.7; font-family: 'JetBrains Mono', 'Fira Code', monospace; color: #8b949e; }
  .empty { color: #484f58; font-style: italic; padding: 20px; text-align: center; }

  /* Combined stats banner */
  .combined-banner { background: linear-gradient(135deg, #161b22, #1c2333); border: 1px solid #21262d; border-radius: 8px; padding: 16px 24px; margin-bottom: 20px; display: flex; gap: 40px; align-items: center; }
  .combined-banner .big { font-size: 1.8em; font-weight: 800; }
  .combined-banner .sub { font-size: 0.65em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }

  @media (max-width: 800px) {
    .charts { grid-template-columns: 1fr; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .combined-banner { flex-wrap: wrap; gap: 20px; }
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>TRADING DASHBOARD</h1>
  <div class="meta">
    <span class="badge badge-paper">PAPER TRADING</span>
    &nbsp;&middot;&nbsp; {{ now }} &nbsp;&middot;&nbsp; auto-refresh 15s
  </div>
</div>

<!-- Combined P&L Banner -->
<div class="container" style="padding-bottom:0">
<div class="combined-banner">
  <div>
    <div class="sub">Total Combined P&L</div>
    <div class="big {{ 'g' if combined_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(combined_pnl) }}</div>
  </div>
  <div>
    <div class="sub">Crypto Scalper</div>
    <div class="val {{ 'g' if crypto.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(crypto.total_pnl) }}
      <span class="n" style="font-size:0.6em">({{ crypto.total_trades }} trades)</span></div>
  </div>
  <div>
    <div class="sub">Polymarket</div>
    <div class="val {{ 'g' if poly.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(poly.total_pnl) }}
      <span class="n" style="font-size:0.6em">({{ poly.total_trades }} trades)</span></div>
  </div>
  <div>
    <div class="sub">Stocks</div>
    <div class="val {{ 'g' if stock.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(stock.total_pnl) }}
      <span class="n" style="font-size:0.6em">({{ stock.total_trades }} trades)</span></div>
  </div>
  <div>
    <div class="sub">Total Bankroll</div>
    <div class="val b">${{ "%.2f"|format(crypto.bankroll + crypto_exposure + poly.bankroll + poly_exposure + stock.bankroll + stock_exposure) }}</div>
  </div>
</div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('stock', this)">
    Stocks <span class="badge-count">{{ stock_positions|length }} open</span>
  </div>
  <div class="tab" onclick="switchTab('crypto', this)">
    Crypto Scalper <span class="badge-count">{{ crypto_positions|length }} open</span>
  </div>
  <div class="tab" onclick="switchTab('poly', this)">
    Polymarket <span class="badge-count">{{ poly_positions|length }} open</span>
  </div>
</div>

<!-- ===================== CRYPTO TAB ===================== -->
<div id="tab-crypto" class="tab-content">
<div class="container">

<!-- Scalper Model Card -->
<div class="model-card">
  <div>
    <div class="mc-label">Engine</div>
    <div class="mc-val">9-Signal Confluence</div>
  </div>
  <div>
    <div class="mc-label">Min Score</div>
    <div class="mc-val">{{ crypto_config.min_confluence }}</div>
  </div>
  <div>
    <div class="mc-label">Min Grade</div>
    <div class="mc-val">{{ crypto_config.min_grade }}</div>
  </div>
  <div>
    <div class="mc-label">Avg Score</div>
    <div class="mc-val b">{{ "%.1f"|format(crypto_avg_score) }}</div>
  </div>
  <div>
    <div class="mc-label">WR by Grade</div>
    <div class="mc-val">{% for g, s in crypto_grade_wr.items() %}<span class="{{ 'g' if s >= 50 else 'r' }}">{{g}}:{{s|int}}%</span> {% endfor %}</div>
  </div>
  <div>
    <div class="mc-label">Best Regime</div>
    <div class="mc-val g">{{ crypto_best_regime }}</div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="label">Bankroll</div>
    <div class="val {{ 'g' if (crypto.bankroll + crypto_exposure) >= crypto.starting_bankroll else 'r' }}">${{ "%.2f"|format(crypto.bankroll + crypto_exposure) }}</div>
    <div class="n" style="font-size:0.6em">${{ "%.2f"|format(crypto.bankroll) }} cash + ${{ "%.2f"|format(crypto_exposure) }} in positions</div>
  </div>
  <div class="stat">
    <div class="label">Total P&L</div>
    <div class="val {{ 'g' if crypto.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(crypto.total_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Daily P&L</div>
    <div class="val {{ 'g' if crypto.daily_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(crypto.daily_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Win Rate</div>
    <div class="val {{ 'g' if crypto_wr >= 50 else 'y' if crypto_wr >= 40 else 'r' }}">{{ "%.0f"|format(crypto_wr) }}%</div>
  </div>
  <div class="stat">
    <div class="label">Trades</div>
    <div class="val n">{{ crypto.total_trades }}</div>
  </div>
  <div class="stat">
    <div class="label">Open</div>
    <div class="val n">{{ crypto_positions|length }} / ${{ "%.2f"|format(crypto_exposure) }}</div>
  </div>
  <div class="stat">
    <div class="label">Avg P&L/Trade</div>
    <div class="val {{ 'g' if crypto_avg >= 0 else 'r' }}">${{ "%+.4f"|format(crypto_avg) }}</div>
  </div>
  <div class="stat">
    <div class="label">ROI</div>
    <div class="val {{ 'g' if crypto_roi >= 0 else 'r' }}">{{ "%+.1f"|format(crypto_roi) }}%</div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="label">Profit Factor</div>
    <div class="val {{ 'g' if crypto_analytics.profit_factor >= 1 else 'r' }}">{{ "%.2f"|format(crypto_analytics.profit_factor) }}</div>
  </div>
  <div class="stat">
    <div class="label">Sharpe</div>
    <div class="val {{ 'g' if crypto_analytics.sharpe >= 0 else 'r' }}">{{ "%.2f"|format(crypto_analytics.sharpe) }}</div>
  </div>
  <div class="stat">
    <div class="label">Max Drawdown</div>
    <div class="val r">${{ "%.4f"|format(crypto_analytics.max_dd) }}</div>
  </div>
  <div class="stat">
    <div class="label">Best Trade</div>
    <div class="val g">${{ "%+.4f"|format(crypto_analytics.best_trade) }}</div>
  </div>
  <div class="stat">
    <div class="label">Worst Trade</div>
    <div class="val r">${{ "%+.4f"|format(crypto_analytics.worst_trade) }}</div>
  </div>
  <div class="stat">
    <div class="label">Avg Win / Loss</div>
    <div class="val"><span class="g">${{ "%.4f"|format(crypto_analytics.avg_win) }}</span> / <span class="r">${{ "%.4f"|format(crypto_analytics.avg_loss) }}</span></div>
  </div>
  <div class="stat">
    <div class="label">Streak</div>
    <div class="val {{ 'g' if crypto_analytics.streak > 0 else 'r' if crypto_analytics.streak < 0 else 'n' }}">{{ crypto_analytics.streak }}{{ crypto_analytics.streak_type }}</div>
  </div>
</div>

<!-- Crypto P&L Chart -->
<div class="charts">
  <div class="chart-card">
    <h3>Crypto P&L Over Time</h3>
    <div class="chart-wrap"><canvas id="cryptoPnlChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Trades by Signal Type</h3>
    <div class="chart-wrap"><canvas id="cryptoSignalChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Daily P&L</h3>
    <div class="chart-wrap"><canvas id="cryptoDailyChart"></canvas></div>
  </div>
</div>
<div class="charts" style="grid-template-columns: 1fr 1fr 1fr;">
  <div class="chart-card">
    <h3>Win Rate by Grade</h3>
    <div class="chart-wrap"><canvas id="cryptoGradeChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>P&L by Regime</h3>
    <div class="chart-wrap"><canvas id="cryptoRegimeChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Confluence Score Distribution</h3>
    <div class="chart-wrap"><canvas id="cryptoScoreChart"></canvas></div>
  </div>
</div>

<!-- Crypto Price Charts -->
<div class="section">
  <h2>Price Charts</h2>
  <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
    {% for pair in crypto_pairs %}
    <button class="pair-btn" onclick="loadChart('{{pair}}')" id="btn-{{pair}}"
            style="padding:6px 14px;border:1px solid #21262d;background:#161b22;color:#8b949e;border-radius:6px;cursor:pointer;font-size:0.75em;font-weight:600;">
      {{pair.replace('-USD','')}}
    </button>
    {% endfor %}
  </div>
  <div style="display:grid;grid-template-columns:1fr;gap:0;">
    <div class="chart-card" style="margin-bottom:0;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h3 id="priceChartTitle">Select a pair</h3>
        <div style="display:flex;gap:6px;">
          <button class="tf-btn" onclick="changeTimeframe(event,'FIVE_MINUTE',288)" style="padding:3px 10px;border:1px solid #21262d;background:#161b22;color:#8b949e;border-radius:4px;cursor:pointer;font-size:0.65em;">24H</button>
          <button class="tf-btn" onclick="changeTimeframe(event,'ONE_HOUR',168)" style="padding:3px 10px;border:1px solid #21262d;background:#0d2744;color:#58a6ff;border-radius:4px;cursor:pointer;font-size:0.65em;">7D</button>
          <button class="tf-btn" onclick="changeTimeframe(event,'SIX_HOUR',120)" style="padding:3px 10px;border:1px solid #21262d;background:#161b22;color:#8b949e;border-radius:4px;cursor:pointer;font-size:0.65em;">1M</button>
          <button class="tf-btn" onclick="changeTimeframe(event,'ONE_DAY',90)" style="padding:3px 10px;border:1px solid #21262d;background:#161b22;color:#8b949e;border-radius:4px;cursor:pointer;font-size:0.65em;">3M</button>
          <button class="tf-btn" onclick="changeTimeframe(event,'ONE_DAY',365)" style="padding:3px 10px;border:1px solid #21262d;background:#161b22;color:#8b949e;border-radius:4px;cursor:pointer;font-size:0.65em;">1Y</button>
          <button class="tf-btn" onclick="changeTimeframe(event,'ONE_DAY',3000)" style="padding:3px 10px;border:1px solid #21262d;background:#161b22;color:#8b949e;border-radius:4px;cursor:pointer;font-size:0.65em;">ALL</button>
        </div>
      </div>
      <div style="position:relative;height:350px;"><canvas id="priceChart"></canvas></div>
    </div>
  </div>
</div>

<!-- Crypto Open Positions -->
{% if crypto_positions %}
<div class="section">
  <h2>Open Positions ({{ crypto_positions|length }})</h2>
  <table>
    <tr><th>ID</th><th>Pair</th><th>Entry</th><th>Current</th><th>%</th><th>P&L</th><th>Size</th><th>Score</th><th>Grade</th><th>Regime</th><th>Signal</th><th>TP</th><th>SL</th><th>Opened</th></tr>
    {% for p in crypto_positions %}
    {% set pct = ((p.current_price - p.entry_price) / p.entry_price * 100) if p.entry_price > 0 else 0 %}
    <tr>
      <td>{{ p.id }}</td>
      <td><strong>{{ p.pair }}</strong></td>
      <td>{{ fmt_price(p.entry_price) }}</td>
      <td>{{ fmt_price(p.current_price) }}</td>
      <td class="{{ 'g' if pct >= 0 else 'r' }}">{{ "%+.2f"|format(pct) }}%</td>
      <td class="{{ 'g' if p.get('unrealized_pnl', 0) >= 0 else 'r' }}">${{ "%+.4f"|format(p.get('unrealized_pnl', 0)) }}</td>
      <td>${{ "%.2f"|format(p.cost_usd) }}</td>
      <td class="b">{{ p.get('confluence_score', '?') }}</td>
      <td><span class="badge badge-{{ 'high' if p.get('quality_grade','') in ('A','B') else 'medium' if p.get('quality_grade','')=='C' else 'low' }}">{{ p.get('quality_grade', '?') }}</span></td>
      <td>{{ p.get('regime', '?') }}</td>
      <td><span class="badge badge-crypto">{{ p.signal_type }}</span></td>
      <td class="g">{{ fmt_price(p.take_profit) }}</td>
      <td class="r">{{ fmt_price(p.stop_loss) }}</td>
      <td>{{ p.opened_at[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Crypto Closed Trades -->
{% if crypto_closed %}
<div class="section">
  <h2>Closed Trades (Last 50 of {{ crypto_closed|length }})</h2>
  <table>
    <tr><th>ID</th><th>Pair</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Score</th><th>Grade</th><th>Regime</th><th>Signal</th><th>Reason</th><th>Closed</th></tr>
    {% for t in crypto_closed[-50:]|reverse %}
    <tr>
      <td>{{ t.id }}</td>
      <td><strong>{{ t.pair }}</strong></td>
      <td>{{ fmt_price(t.entry_price) }}</td>
      <td>{{ fmt_price(t.exit_price) }}</td>
      <td class="{{ 'g' if t.pnl >= 0 else 'r' }}">${{ "%+.4f"|format(t.pnl) }}</td>
      <td class="{{ 'g' if t.get('pnl_pct', 0) >= 0 else 'r' }}">{{ "%+.1f"|format(t.get('pnl_pct', 0)) }}%</td>
      <td class="b">{{ t.get('confluence_score', '?') }}</td>
      <td><span class="badge badge-{{ 'high' if t.get('quality_grade','') in ('A','B') else 'medium' if t.get('quality_grade','')=='C' else 'low' }}">{{ t.get('quality_grade', '?') }}</span></td>
      <td>{{ t.get('regime', '?') }}</td>
      <td><span class="badge badge-crypto">{{ t.signal_type }}</span></td>
      <td>{{ t.reason }}</td>
      <td>{{ t.get('closed_at', '')[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Crypto Log -->
<div class="section">
  <h2>Scalper Log (Last 50)</h2>
  <div class="log-box">{% if crypto_logs %}{% for line in crypto_logs %}{{ line }}{% endfor %}{% else %}No log entries yet{% endif %}</div>
</div>

</div>
</div>

<!-- ===================== POLYMARKET TAB ===================== -->
<div id="tab-poly" class="tab-content">
<div class="container">

<!-- Model Info Card -->
<div class="model-card">
  <div>
    <div class="mc-label">Model</div>
    <div class="mc-val">GBM + Momentum</div>
  </div>
  <div>
    <div class="mc-label">Brier Score</div>
    <div class="mc-val g">0.1506</div>
  </div>
  <div>
    <div class="mc-label">Backtest Accuracy</div>
    <div class="mc-val">77.9%</div>
  </div>
  <div>
    <div class="mc-label">Markets Tested</div>
    <div class="mc-val">9,838</div>
  </div>
  <div>
    <div class="mc-label">Vol Multiplier</div>
    <div class="mc-val">1.6x</div>
  </div>
  <div>
    <div class="mc-label">Avg Edge (Trades)</div>
    <div class="mc-val b">{{ "%.1f"|format(poly_avg_edge * 100) }}%</div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="label">Bankroll</div>
    <div class="val {{ 'g' if poly.bankroll >= poly.starting_bankroll else 'r' }}">${{ "%.2f"|format(poly.bankroll) }}</div>
  </div>
  <div class="stat">
    <div class="label">Total P&L</div>
    <div class="val {{ 'g' if poly.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(poly.total_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Daily P&L</div>
    <div class="val {{ 'g' if poly.daily_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(poly.daily_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Win Rate</div>
    <div class="val {{ 'g' if poly_wr >= 50 else 'y' if poly_wr >= 40 else 'r' }}">{{ "%.0f"|format(poly_wr) }}%</div>
  </div>
  <div class="stat">
    <div class="label">Trades</div>
    <div class="val n">{{ poly.total_trades }}</div>
  </div>
  <div class="stat">
    <div class="label">Open</div>
    <div class="val n">{{ poly_positions|length }} / ${{ "%.2f"|format(poly_exposure) }}</div>
  </div>
  <div class="stat">
    <div class="label">ROI</div>
    <div class="val {{ 'g' if poly_roi >= 0 else 'r' }}">{{ "%+.1f"|format(poly_roi) }}%</div>
  </div>
  <div class="stat">
    <div class="label">Last Scan</div>
    <div class="val n" style="font-size:0.7em">{{ poly.last_scan[:16] if poly.last_scan else 'never' }}</div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="label">Profit Factor</div>
    <div class="val {{ 'g' if poly_analytics.profit_factor >= 1 else 'r' }}">{{ "%.2f"|format(poly_analytics.profit_factor) }}</div>
  </div>
  <div class="stat">
    <div class="label">Sharpe</div>
    <div class="val {{ 'g' if poly_analytics.sharpe >= 0 else 'r' }}">{{ "%.2f"|format(poly_analytics.sharpe) }}</div>
  </div>
  <div class="stat">
    <div class="label">Max Drawdown</div>
    <div class="val r">${{ "%.4f"|format(poly_analytics.max_dd) }}</div>
  </div>
  <div class="stat">
    <div class="label">Best Trade</div>
    <div class="val g">${{ "%+.4f"|format(poly_analytics.best_trade) }}</div>
  </div>
  <div class="stat">
    <div class="label">Worst Trade</div>
    <div class="val r">${{ "%+.4f"|format(poly_analytics.worst_trade) }}</div>
  </div>
  <div class="stat">
    <div class="label">Avg Win / Loss</div>
    <div class="val"><span class="g">${{ "%.4f"|format(poly_analytics.avg_win) }}</span> / <span class="r">${{ "%.4f"|format(poly_analytics.avg_loss) }}</span></div>
  </div>
  <div class="stat">
    <div class="label">Streak</div>
    <div class="val {{ 'g' if poly_analytics.streak > 0 else 'r' if poly_analytics.streak < 0 else 'n' }}">{{ poly_analytics.streak }}{{ poly_analytics.streak_type }}</div>
  </div>
</div>

<!-- Poly P&L Chart -->
<div class="charts">
  <div class="chart-card">
    <h3>Polymarket P&L Over Time</h3>
    <div class="chart-wrap"><canvas id="polyPnlChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Edge Distribution</h3>
    <div class="chart-wrap"><canvas id="polyEdgeChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Daily P&L</h3>
    <div class="chart-wrap"><canvas id="polyDailyChart"></canvas></div>
  </div>
</div>
<div class="charts" style="grid-template-columns: 1fr 1fr 1fr;">
  <div class="chart-card">
    <h3>Trades by Category</h3>
    <div class="chart-wrap"><canvas id="polyCatChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Confidence Breakdown</h3>
    <div class="chart-wrap"><canvas id="polyConfChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>P&L by Confidence</h3>
    <div class="chart-wrap"><canvas id="polyConfPnlChart"></canvas></div>
  </div>
</div>

<!-- Poly Open Positions -->
{% if poly_positions %}
<div class="section">
  <h2>Open Positions ({{ poly_positions|length }})</h2>
  <table>
    <tr><th>ID</th><th>Side</th><th>Market</th><th>Cat</th><th>Entry</th><th>Model</th><th>Current</th><th>Cost</th><th>P&L</th><th>Edge</th><th>Conf</th><th>Opened</th></tr>
    {% for p in poly_positions %}
    <tr>
      <td>{{ p.id }}</td>
      <td><span class="{{ 'g' if p.side == 'YES' else 'r' }}">{{ p.side }}</span></td>
      <td title="{{ p.market_question }}">{{ p.market_question[:50] }}</td>
      <td>{{ p.category }}</td>
      <td>{{ "%.1f"|format(p.entry_price * 100) }}c</td>
      <td class="b">{{ "%.0f"|format(p.get('estimated_prob', 0) * 100) }}%</td>
      <td>{{ "%.1f"|format(p.get('current_price', p.entry_price) * 100) }}c</td>
      <td>${{ "%.2f"|format(p.cost_usd) }}</td>
      <td class="{{ 'g' if p.get('unrealized_pnl', 0) >= 0 else 'r' }}">${{ "%+.2f"|format(p.get('unrealized_pnl', 0)) }}</td>
      <td class="g">{{ "%.1f"|format(p.edge_at_entry * 100) }}%</td>
      <td><span class="badge badge-{{ p.get('confidence', 'low') }}">{{ p.get('confidence', '?') }}</span></td>
      <td>{{ p.opened_at[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Poly Closed Trades -->
{% if poly_closed %}
<div class="section">
  <h2>Closed Trades (Last 50 of {{ poly_closed|length }})</h2>
  <table>
    <tr><th>ID</th><th>Side</th><th>Market</th><th>Cat</th><th>Entry</th><th>Model</th><th>Exit</th><th>P&L</th><th>Edge</th><th>CLV</th><th>Conf</th><th>Reason</th><th>Closed</th></tr>
    {% for t in poly_closed[-50:]|reverse %}
    <tr>
      <td>{{ t.id }}</td>
      <td><span class="{{ 'g' if t.side == 'YES' else 'r' }}">{{ t.side }}</span></td>
      <td title="{{ t.market_question }}">{{ t.market_question[:45] }}</td>
      <td>{{ t.category }}</td>
      <td>{{ "%.1f"|format(t.entry_price * 100) }}c</td>
      <td class="b">{{ "%.0f"|format(t.get('estimated_prob', 0) * 100) }}%</td>
      <td>{{ "%.1f"|format(t.exit_price * 100) }}c</td>
      <td class="{{ 'g' if t.pnl >= 0 else 'r' }}">${{ "%+.2f"|format(t.pnl) }}</td>
      <td class="g">{{ "%.1f"|format(t.get('edge_at_entry', 0) * 100) }}%</td>
      <td class="{{ 'g' if t.get('clv', 0) >= 0 else 'r' }}">{{ "%+.1f"|format(t.get('clv', 0) * 100) }}c</td>
      <td><span class="badge badge-{{ t.get('confidence', 'low') }}">{{ t.get('confidence', '?') }}</span></td>
      <td>{{ t.reason }}</td>
      <td>{{ t.get('closed_at', '')[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Poly Log -->
<div class="section">
  <h2>Paper Trading Log (Last 50)</h2>
  <div class="log-box">{% if poly_logs %}{% for line in poly_logs %}{{ line }}{% endfor %}{% else %}No log entries yet{% endif %}</div>
</div>

</div>
</div>

<!-- ===================== STOCKS TAB ===================== -->
<div id="tab-stock" class="tab-content active">
<div class="container">

<!-- Stock Model Card -->
<div class="model-card">
  <div>
    <div class="mc-label">Engine</div>
    <div class="mc-val">12-Signal Confluence</div>
  </div>
  <div>
    <div class="mc-label">Min Score</div>
    <div class="mc-val">5</div>
  </div>
  <div>
    <div class="mc-label">Regime</div>
    <div class="mc-val b">{{ stock.get('regime', 'unknown')|upper }}</div>
  </div>
  <div>
    <div class="mc-label">Avg Score</div>
    <div class="mc-val b">{{ "%.1f"|format(stock_avg_score) }}</div>
  </div>
  <div>
    <div class="mc-label">WR by Grade</div>
    <div class="mc-val">{% for g, s in stock_grade_wr.items() %}<span class="{{ 'g' if s >= 50 else 'r' }}">{{g}}:{{s|int}}%</span> {% endfor %}</div>
  </div>
  <div>
    <div class="mc-label">Market</div>
    <div class="mc-val {{ 'g' if stock_market_open else 'r' }}">{{ 'OPEN' if stock_market_open else 'CLOSED' }}</div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="label">Bankroll</div>
    <div class="val {{ 'g' if (stock.bankroll + stock_exposure) >= stock.starting_bankroll else 'r' }}">${{ "%.2f"|format(stock.bankroll + stock_exposure) }}</div>
    <div class="n" style="font-size:0.6em">${{ "%.2f"|format(stock.bankroll) }} cash + ${{ "%.2f"|format(stock_exposure) }} in positions</div>
  </div>
  <div class="stat">
    <div class="label">Total P&L</div>
    <div class="val {{ 'g' if stock.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(stock.total_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Daily P&L</div>
    <div class="val {{ 'g' if stock.daily_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(stock.daily_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Win Rate</div>
    <div class="val {{ 'g' if stock_wr >= 50 else 'y' if stock_wr >= 40 else 'r' }}">{{ "%.0f"|format(stock_wr) }}%</div>
  </div>
  <div class="stat">
    <div class="label">Trades</div>
    <div class="val n">{{ stock.total_trades }}</div>
  </div>
  <div class="stat">
    <div class="label">Open</div>
    <div class="val n">{{ stock_positions|length }} / ${{ "%.2f"|format(stock_exposure) }}</div>
  </div>
  <div class="stat">
    <div class="label">Avg P&L/Trade</div>
    <div class="val {{ 'g' if stock_avg >= 0 else 'r' }}">${{ "%+.4f"|format(stock_avg) }}</div>
  </div>
  <div class="stat">
    <div class="label">ROI</div>
    <div class="val {{ 'g' if stock_roi >= 0 else 'r' }}">{{ "%+.1f"|format(stock_roi) }}%</div>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="label">Profit Factor</div>
    <div class="val {{ 'g' if stock_analytics.profit_factor >= 1 else 'r' }}">{{ "%.2f"|format(stock_analytics.profit_factor) }}</div>
  </div>
  <div class="stat">
    <div class="label">Sharpe</div>
    <div class="val {{ 'g' if stock_analytics.sharpe >= 0 else 'r' }}">{{ "%.2f"|format(stock_analytics.sharpe) }}</div>
  </div>
  <div class="stat">
    <div class="label">Max Drawdown</div>
    <div class="val r">${{ "%.2f"|format(stock_analytics.max_dd) }}</div>
  </div>
  <div class="stat">
    <div class="label">Best Trade</div>
    <div class="val g">${{ "%+.2f"|format(stock_analytics.best_trade) }}</div>
  </div>
  <div class="stat">
    <div class="label">Worst Trade</div>
    <div class="val r">${{ "%+.2f"|format(stock_analytics.worst_trade) }}</div>
  </div>
  <div class="stat">
    <div class="label">Avg Win / Loss</div>
    <div class="val"><span class="g">${{ "%.2f"|format(stock_analytics.avg_win) }}</span> / <span class="r">${{ "%.2f"|format(stock_analytics.avg_loss) }}</span></div>
  </div>
  <div class="stat">
    <div class="label">Streak</div>
    <div class="val {{ 'g' if stock_analytics.streak > 0 else 'r' if stock_analytics.streak < 0 else 'n' }}">{{ stock_analytics.streak }}{{ stock_analytics.streak_type }}</div>
  </div>
</div>

<!-- Stock Charts -->
<div class="charts">
  <div class="chart-card">
    <h3>Stock P&L Over Time</h3>
    <div class="chart-wrap"><canvas id="stockPnlChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Sector Allocation</h3>
    <div class="chart-wrap"><canvas id="stockSectorChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Daily P&L</h3>
    <div class="chart-wrap"><canvas id="stockDailyChart"></canvas></div>
  </div>
</div>
<div class="charts" style="grid-template-columns: 1fr 1fr 1fr;">
  <div class="chart-card">
    <h3>Win Rate by Grade</h3>
    <div class="chart-wrap"><canvas id="stockGradeChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>P&L by Regime</h3>
    <div class="chart-wrap"><canvas id="stockRegimeChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Confluence Score Distribution</h3>
    <div class="chart-wrap"><canvas id="stockScoreChart"></canvas></div>
  </div>
</div>

<!-- Stock Open Positions -->
{% if stock_positions %}
<div class="section">
  <h2>Open Positions ({{ stock_positions|length }})</h2>
  <table>
    <tr><th>ID</th><th>Symbol</th><th>Sector</th><th>Entry</th><th>Current</th><th>%</th><th>P&L</th><th>Size</th><th>Score</th><th>Grade</th><th>Regime</th><th>TP</th><th>SL</th><th>Opened</th></tr>
    {% for p in stock_positions %}
    {% set pct = ((p.current_price - p.entry_price) / p.entry_price * 100) if p.entry_price > 0 else 0 %}
    <tr>
      <td>{{ p.id }}</td>
      <td><strong>{{ p.symbol }}</strong></td>
      <td>{{ p.get('sector', '?') }}</td>
      <td>${{ "%.2f"|format(p.entry_price) }}</td>
      <td>${{ "%.2f"|format(p.get('current_price', p.entry_price)) }}</td>
      <td class="{{ 'g' if pct >= 0 else 'r' }}">{{ "%+.2f"|format(pct) }}%</td>
      <td class="{{ 'g' if p.get('unrealized_pnl', 0) >= 0 else 'r' }}">${{ "%+.4f"|format(p.get('unrealized_pnl', 0)) }}</td>
      <td>${{ "%.2f"|format(p.cost_usd) }}</td>
      <td class="b">{{ p.get('confluence_score', '?') }}</td>
      <td><span class="badge badge-{{ 'high' if p.get('quality_grade','') in ('A','B') else 'medium' if p.get('quality_grade','')=='C' else 'low' }}">{{ p.get('quality_grade', '?') }}</span></td>
      <td>{{ p.get('regime', '?') }}</td>
      <td class="g">${{ "%.2f"|format(p.take_profit) }}</td>
      <td class="r">${{ "%.2f"|format(p.stop_loss) }}</td>
      <td>{{ p.opened_at[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Stock Closed Trades -->
{% if stock_closed %}
<div class="section">
  <h2>Closed Trades (Last 50 of {{ stock_closed|length }})</h2>
  <table>
    <tr><th>ID</th><th>Symbol</th><th>Sector</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Score</th><th>Grade</th><th>Regime</th><th>Reason</th><th>Closed</th></tr>
    {% for t in stock_closed[-50:]|reverse %}
    {% set pct = ((t.get('close_price', t.entry_price) - t.entry_price) / t.entry_price * 100) if t.entry_price > 0 else 0 %}
    <tr>
      <td>{{ t.id }}</td>
      <td><strong>{{ t.symbol }}</strong></td>
      <td>{{ t.get('sector', '?') }}</td>
      <td>${{ "%.2f"|format(t.entry_price) }}</td>
      <td>${{ "%.2f"|format(t.get('close_price', t.entry_price)) }}</td>
      <td class="{{ 'g' if t.pnl >= 0 else 'r' }}">${{ "%+.4f"|format(t.pnl) }}</td>
      <td class="{{ 'g' if pct >= 0 else 'r' }}">{{ "%+.2f"|format(pct) }}%</td>
      <td class="b">{{ t.get('confluence_score', '?') }}</td>
      <td><span class="badge badge-{{ 'high' if t.get('quality_grade','') in ('A','B') else 'medium' if t.get('quality_grade','')=='C' else 'low' }}">{{ t.get('quality_grade', '?') }}</span></td>
      <td>{{ t.get('regime', '?') }}</td>
      <td>{{ t.get('close_reason', '?') }}</td>
      <td>{{ t.get('closed_at', '')[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Stock Log -->
<div class="section">
  <h2>Stock Trader Log (Last 50)</h2>
  <div class="log-box">{% if stock_logs %}{% for line in stock_logs %}{{ line }}{% endfor %}{% else %}No log entries yet{% endif %}</div>
</div>

</div>
</div>

<script>
// Tab switching with persistence
function switchTab(tab, el) {
  document.querySelectorAll('.tab-content').forEach(e => e.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(e => e.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  if (el) el.classList.add('active');
  else {
    const tabIdx = {stock: 0, crypto: 1, poly: 2}[tab] || 0;
    document.querySelectorAll('.tab')[tabIdx].classList.add('active');
  }
  localStorage.setItem('ds_tab', 'tab-' + tab);
}

const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } },
};
const chartColors = ['#3fb950','#58a6ff','#d29922','#f85149','#bc8cff','#39d2c0','#ff7b72','#79c0ff','#ffa657','#d2a8ff'];

// --- Crypto Charts ---
const cryptoPnl = {{ crypto_pnl_timeline | tojson | replace("</", "<\\/") }};
if (cryptoPnl.length > 0) {
  new Chart(document.getElementById('cryptoPnlChart'), {
    type: 'line',
    data: {
      labels: cryptoPnl.map(d => d.time),
      datasets: [{
        data: cryptoPnl.map(d => d.pnl),
        borderColor: cryptoPnl[cryptoPnl.length-1].pnl >= 0 ? '#3fb950' : '#f85149',
        backgroundColor: cryptoPnl[cryptoPnl.length-1].pnl >= 0 ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
      }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { ticks: { color: '#8b949e', maxTicksLimit: 8, maxRotation: 0, font: { size: 9 },
          callback: function(val, i) { const t = cryptoPnl[i]?.time || ''; return t.slice(5,10); } } ,
          grid: { display: false } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(4) } }
      },
      plugins: { ...chartDefaults.plugins, tooltip: { callbacks: { title: function(ctx) { return cryptoPnl[ctx[0].dataIndex]?.time || ''; } } } }
    }
  });
}

// Crypto signal type chart
const cryptoSignals = {{ crypto_signal_counts | tojson | replace("</", "<\\/") }};
if (Object.keys(cryptoSignals).length > 0) {
  new Chart(document.getElementById('cryptoSignalChart'), {
    type: 'doughnut',
    data: {
      labels: Object.keys(cryptoSignals),
      datasets: [{ data: Object.values(cryptoSignals), backgroundColor: chartColors, borderWidth: 0 }]
    },
    options: { ...chartDefaults, plugins: { legend: { display: true, position: 'right', labels: { color: '#8b949e', font: { size: 10 }, padding: 6 } } } }
  });
}

// Crypto daily P&L bars
const cryptoDaily = {{ crypto_daily | tojson | replace("</", "<\\/") }};
if (cryptoDaily.length > 0) {
  new Chart(document.getElementById('cryptoDailyChart'), {
    type: 'bar',
    data: {
      labels: cryptoDaily.map(d => d.day.slice(5)),
      datasets: [{
        data: cryptoDaily.map(d => d.pnl),
        backgroundColor: cryptoDaily.map(d => d.pnl >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'),
        borderWidth: 0,
      }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      }
    }
  });
}

// Crypto grade win rate chart
const cryptoGradeWr = {{ crypto_grade_wr_full | tojson | replace("</", "<\\/") }};
if (Object.keys(cryptoGradeWr).length > 0) {
  const gradeLabels = Object.keys(cryptoGradeWr);
  new Chart(document.getElementById('cryptoGradeChart'), {
    type: 'bar',
    data: {
      labels: gradeLabels,
      datasets: [
        { label: 'Win Rate %', data: gradeLabels.map(g => cryptoGradeWr[g].wr), backgroundColor: gradeLabels.map(g => cryptoGradeWr[g].wr >= 50 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'), borderWidth: 0 },
      ]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e' } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => v + '%' }, max: 100 }
      },
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ctx.parsed.y.toFixed(0) + '% (' + gradeLabels.map(g => cryptoGradeWr[g].n)[ctx.dataIndex] + ' trades)' } }
      }
    }
  });
}

// Crypto P&L by regime chart
const cryptoRegimePnl = {{ crypto_regime_pnl | tojson | replace("</", "<\\/") }};
if (Object.keys(cryptoRegimePnl).length > 0) {
  const regLabels = Object.keys(cryptoRegimePnl);
  new Chart(document.getElementById('cryptoRegimeChart'), {
    type: 'bar',
    data: {
      labels: regLabels,
      datasets: [{ data: Object.values(cryptoRegimePnl), backgroundColor: regLabels.map(r => cryptoRegimePnl[r] >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'), borderWidth: 0 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      }
    }
  });
}

// Crypto confluence score distribution
const cryptoScores = {{ crypto_score_data | tojson | replace("</", "<\\/") }};
if (cryptoScores.length > 0) {
  const scoreBins = {};
  cryptoScores.forEach(s => { scoreBins[s] = (scoreBins[s] || 0) + 1; });
  const sLabels = Object.keys(scoreBins).sort((a,b) => a-b);
  new Chart(document.getElementById('cryptoScoreChart'), {
    type: 'bar',
    data: {
      labels: sLabels.map(s => s + '/10'),
      datasets: [{ data: sLabels.map(s => scoreBins[s]), backgroundColor: 'rgba(88,166,255,0.6)', borderColor: '#58a6ff', borderWidth: 1 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e' } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', stepSize: 1 } }
      }
    }
  });
}

// --- Polymarket Charts ---
const polyPnl = {{ poly_pnl_timeline | tojson | replace("</", "<\\/") }};
if (polyPnl.length > 0) {
  new Chart(document.getElementById('polyPnlChart'), {
    type: 'line',
    data: {
      labels: polyPnl.map(d => d.time),
      datasets: [{
        data: polyPnl.map(d => d.pnl),
        borderColor: polyPnl[polyPnl.length-1].pnl >= 0 ? '#3fb950' : '#f85149',
        backgroundColor: polyPnl[polyPnl.length-1].pnl >= 0 ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
      }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { ticks: { color: '#8b949e', maxTicksLimit: 8, maxRotation: 0, font: { size: 9 },
          callback: function(val, i) { const t = polyPnl[i]?.time || ''; return t.slice(5,10); } },
          grid: { display: false } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      },
      plugins: { ...chartDefaults.plugins, tooltip: { callbacks: { title: function(ctx) { return polyPnl[ctx[0].dataIndex]?.time || ''; } } } }
    }
  });
}

// Poly category chart
const polyCats = {{ poly_cat_counts | tojson | replace("</", "<\\/") }};
if (Object.keys(polyCats).length > 0) {
  new Chart(document.getElementById('polyCatChart'), {
    type: 'doughnut',
    data: {
      labels: Object.keys(polyCats),
      datasets: [{ data: Object.values(polyCats), backgroundColor: chartColors, borderWidth: 0 }]
    },
    options: { ...chartDefaults, plugins: { legend: { display: true, position: 'right', labels: { color: '#8b949e', font: { size: 10 }, padding: 6 } } } }
  });
}

// Poly daily P&L bars
const polyDaily = {{ poly_daily | tojson | replace("</", "<\\/") }};
if (polyDaily.length > 0) {
  new Chart(document.getElementById('polyDailyChart'), {
    type: 'bar',
    data: {
      labels: polyDaily.map(d => d.day.slice(5)),
      datasets: [{
        data: polyDaily.map(d => d.pnl),
        backgroundColor: polyDaily.map(d => d.pnl >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'),
        borderWidth: 0,
      }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      }
    }
  });
}

// Edge distribution histogram
const polyEdges = {{ poly_edge_data | tojson | replace("</", "<\\/") }};
if (polyEdges.length > 0) {
  const bins = ['5-10%','10-15%','15-20%','20-30%','30%+'];
  const counts = [0,0,0,0,0];
  polyEdges.forEach(e => {
    if (e < 10) counts[0]++;
    else if (e < 15) counts[1]++;
    else if (e < 20) counts[2]++;
    else if (e < 30) counts[3]++;
    else counts[4]++;
  });
  new Chart(document.getElementById('polyEdgeChart'), {
    type: 'bar',
    data: {
      labels: bins,
      datasets: [{ data: counts, backgroundColor: 'rgba(88,166,255,0.6)', borderColor: '#58a6ff', borderWidth: 1 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', stepSize: 1 } }
      }
    }
  });
}

// Confidence breakdown
const polyConfs = {{ poly_conf_counts | tojson | replace("</", "<\\/") }};
if (Object.keys(polyConfs).length > 0) {
  const confColors = { high: '#3fb950', medium: '#d29922', low: '#f85149' };
  const labels = Object.keys(polyConfs);
  new Chart(document.getElementById('polyConfChart'), {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{ data: Object.values(polyConfs), backgroundColor: labels.map(l => confColors[l] || '#8b949e'), borderWidth: 0 }]
    },
    options: { ...chartDefaults, plugins: { legend: { display: true, position: 'right', labels: { color: '#8b949e', font: { size: 10 }, padding: 6 } } } }
  });
}

// P&L by confidence
const polyConfPnl = {{ poly_conf_pnl | tojson | replace("</", "<\\/") }};
if (Object.keys(polyConfPnl).length > 0) {
  const confLabels = Object.keys(polyConfPnl);
  const confPnlColors = { high: 'rgba(63,185,80,0.7)', medium: 'rgba(210,153,34,0.7)', low: 'rgba(248,81,73,0.7)' };
  new Chart(document.getElementById('polyConfPnlChart'), {
    type: 'bar',
    data: {
      labels: confLabels,
      datasets: [{ data: Object.values(polyConfPnl), backgroundColor: confLabels.map(l => confPnlColors[l] || 'rgba(139,148,158,0.7)'), borderWidth: 0 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e' } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      }
    }
  });
}

// --- Stock Charts ---
const stockPnl = {{ stock_pnl_timeline | tojson | replace("</", "<\\/") }};
if (stockPnl.length > 0) {
  new Chart(document.getElementById('stockPnlChart'), {
    type: 'line',
    data: {
      labels: stockPnl.map(d => d.time),
      datasets: [{
        data: stockPnl.map(d => d.pnl),
        borderColor: stockPnl[stockPnl.length-1].pnl >= 0 ? '#3fb950' : '#f85149',
        backgroundColor: stockPnl[stockPnl.length-1].pnl >= 0 ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
      }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { ticks: { color: '#8b949e', maxTicksLimit: 8, maxRotation: 0, font: { size: 9 },
          callback: function(val, i) { const t = stockPnl[i]?.time || ''; return t.slice(5,10); } },
          grid: { display: false } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      },
      plugins: { ...chartDefaults.plugins, tooltip: { callbacks: { title: function(ctx) { return stockPnl[ctx[0].dataIndex]?.time || ''; } } } }
    }
  });
}

// Stock sector allocation pie
const stockSectors = {{ stock_sector_counts | tojson | replace("</", "<\\/") }};
if (Object.keys(stockSectors).length > 0) {
  new Chart(document.getElementById('stockSectorChart'), {
    type: 'doughnut',
    data: {
      labels: Object.keys(stockSectors),
      datasets: [{ data: Object.values(stockSectors), backgroundColor: chartColors, borderWidth: 0 }]
    },
    options: { ...chartDefaults, plugins: { legend: { display: true, position: 'right', labels: { color: '#8b949e', font: { size: 10 }, padding: 6 } } } }
  });
}

// Stock daily P&L bars
const stockDaily = {{ stock_daily | tojson | replace("</", "<\\/") }};
if (stockDaily.length > 0) {
  new Chart(document.getElementById('stockDailyChart'), {
    type: 'bar',
    data: {
      labels: stockDaily.map(d => d.day.slice(5)),
      datasets: [{
        data: stockDaily.map(d => d.pnl),
        backgroundColor: stockDaily.map(d => d.pnl >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'),
        borderWidth: 0,
      }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      }
    }
  });
}

// Stock grade win rate chart
const stockGradeWr = {{ stock_grade_wr_full | tojson | replace("</", "<\\/") }};
if (Object.keys(stockGradeWr).length > 0) {
  const sGradeLabels = Object.keys(stockGradeWr);
  new Chart(document.getElementById('stockGradeChart'), {
    type: 'bar',
    data: {
      labels: sGradeLabels,
      datasets: [{ label: 'Win Rate %', data: sGradeLabels.map(g => stockGradeWr[g].wr), backgroundColor: sGradeLabels.map(g => stockGradeWr[g].wr >= 50 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'), borderWidth: 0 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e' } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => v + '%' }, max: 100 }
      },
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ctx.parsed.y.toFixed(0) + '% (' + sGradeLabels.map(g => stockGradeWr[g].n)[ctx.dataIndex] + ' trades)' } }
      }
    }
  });
}

// Stock P&L by regime chart
const stockRegimePnl = {{ stock_regime_pnl | tojson | replace("</", "<\\/") }};
if (Object.keys(stockRegimePnl).length > 0) {
  const sRegLabels = Object.keys(stockRegimePnl);
  new Chart(document.getElementById('stockRegimeChart'), {
    type: 'bar',
    data: {
      labels: sRegLabels,
      datasets: [{ data: Object.values(stockRegimePnl), backgroundColor: sRegLabels.map(r => stockRegimePnl[r] >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'), borderWidth: 0 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) } }
      }
    }
  });
}

// Stock confluence score distribution
const stockScores = {{ stock_score_data | tojson | replace("</", "<\\/") }};
if (stockScores.length > 0) {
  const sScoreBins = {};
  stockScores.forEach(s => { sScoreBins[s] = (sScoreBins[s] || 0) + 1; });
  const sSLabels = Object.keys(sScoreBins).sort((a,b) => a-b);
  new Chart(document.getElementById('stockScoreChart'), {
    type: 'bar',
    data: {
      labels: sSLabels.map(s => s + '/12'),
      datasets: [{ data: sSLabels.map(s => sScoreBins[s]), backgroundColor: 'rgba(88,166,255,0.6)', borderColor: '#58a6ff', borderWidth: 1 }]
    },
    options: { ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e' } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e', stepSize: 1 } }
      }
    }
  });
}

// --- Price Charts ---
let priceChart = null;
let currentPair = '';
let currentGranularity = 'ONE_HOUR';
let currentLimit = 168;

function loadChart(pair) {
  currentPair = pair;
  localStorage.setItem('ds_pair', pair);
  document.querySelectorAll('.pair-btn').forEach(b => {
    b.style.background = '#161b22'; b.style.color = '#8b949e'; b.style.borderColor = '#21262d';
  });
  const btn = document.getElementById('btn-' + pair);
  if (btn) { btn.style.background = '#0d2744'; btn.style.color = '#58a6ff'; btn.style.borderColor = '#58a6ff'; }

  document.getElementById('priceChartTitle').textContent = pair + ' Price';

  fetch('/api/prices/' + pair + '?granularity=' + currentGranularity + '&limit=' + currentLimit)
    .then(r => r.json())
    .then(data => {
      if (data.error) { console.error(data.error); return; }
      renderPriceChart(data, pair);
    });
}

function changeTimeframe(event, gran, limit) {
  currentGranularity = gran;
  currentLimit = limit;
  localStorage.setItem('ds_gran', gran);
  localStorage.setItem('ds_limit', limit);
  document.querySelectorAll('.tf-btn').forEach(b => {
    b.style.background = '#161b22'; b.style.color = '#8b949e';
  });
  event.target.style.background = '#0d2744'; event.target.style.color = '#58a6ff';
  if (currentPair) loadChart(currentPair);
}

function renderPriceChart(candles, pair) {
  const ctx = document.getElementById('priceChart');
  if (priceChart) priceChart.destroy();

  const closes = candles.map(c => c.close);
  const highs = candles.map(c => c.high);
  const lows = candles.map(c => c.low);
  const labels = candles.map(c => c.time);
  const volumes = candles.map(c => c.volume);

  const isUp = closes[closes.length-1] >= closes[0];
  const color = isUp ? '#3fb950' : '#f85149';
  const bgColor = isUp ? 'rgba(63,185,80,0.08)' : 'rgba(248,81,73,0.08)';

  priceChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Close',
          data: closes,
          borderColor: color,
          backgroundColor: bgColor,
          fill: true,
          tension: 0.2,
          pointRadius: 0,
          borderWidth: 2,
          yAxisID: 'y',
        },
        {
          label: 'High',
          data: highs,
          borderColor: 'rgba(63,185,80,0.2)',
          borderWidth: 1,
          borderDash: [2,2],
          pointRadius: 0,
          fill: false,
          yAxisID: 'y',
        },
        {
          label: 'Low',
          data: lows,
          borderColor: 'rgba(248,81,73,0.2)',
          borderWidth: 1,
          borderDash: [2,2],
          pointRadius: 0,
          fill: false,
          yAxisID: 'y',
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, position: 'top', labels: { color: '#8b949e', font: { size: 10 }, padding: 8, usePointStyle: true } },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              const i = ctx.dataIndex;
              if (ctx.datasetIndex === 0) {
                return 'O: $' + candles[i].open.toFixed(2) + '  H: $' + candles[i].high.toFixed(2) + '  L: $' + candles[i].low.toFixed(2) + '  C: $' + candles[i].close.toFixed(2);
              }
              return '';
            }
          }
        }
      },
      scales: {
        x: { display: true, grid: { display: false }, ticks: { color: '#484f58', font: { size: 9 }, maxTicksLimit: 12, maxRotation: 0 } },
        y: { position: 'right', grid: { color: '#21262d' }, ticks: { color: '#8b949e', callback: v => '$' + v.toLocaleString() } },
      }
    }
  });
}

// Restore saved state or auto-load defaults
{
  const savedGran = localStorage.getItem('ds_gran');
  const savedLimit = localStorage.getItem('ds_limit');
  if (savedGran) { currentGranularity = savedGran; currentLimit = parseInt(savedLimit) || 168; }

  const savedTab = localStorage.getItem('ds_tab');
  if (savedTab && savedTab !== 'tab-stock') {
    const tabName = savedTab.replace('tab-', '');
    if (['crypto', 'poly', 'stock'].includes(tabName)) switchTab(tabName);
  }

  {% if crypto_pairs %}
  const savedPair = localStorage.getItem('ds_pair');
  const pairs = {{ crypto_pairs | tojson }};
  const startPair = (savedPair && pairs.includes(savedPair)) ? savedPair : '{{ crypto_pairs[0] }}';
  loadChart(startPair);

  // Highlight saved timeframe button
  if (savedGran) {
    document.querySelectorAll('.tf-btn').forEach(b => {
      b.style.background = '#161b22'; b.style.color = '#8b949e';
      if (b.textContent.trim() === {ONE_HOUR:'1H',FIVE_MINUTE:'5M',FIFTEEN_MINUTE:'15M',SIX_HOUR:'6H',ONE_DAY:'1D'}[savedGran])
        { b.style.background = '#0d2744'; b.style.color = '#58a6ff'; }
    });
  }
  {% endif %}
}
</script>
</body>
</html>"""


def fmt_price(p):
    """Format prices for display: $74,501 for BTC, $0.1005 for small coins."""
    if p is None:
        return "$0"
    p = float(p)
    if p >= 100:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:.2f}"
    elif p >= 0.01:
        return f"${p:.4f}"
    else:
        return f"${p:.8f}"


@app.route("/")
@auth_required
def index():
    # Load all states
    poly_state = load_paper_state()
    crypto_state = load_crypto_state()
    stock_state = load_stock_state()

    poly_positions = poly_state.get("positions", [])
    poly_closed = poly_state.get("closed_trades", [])
    crypto_positions = crypto_state.get("positions", [])
    crypto_closed = crypto_state.get("closed_trades", [])
    stock_positions = stock_state.get("positions", [])
    stock_closed = stock_state.get("closed_trades", [])

    # Poly stats
    poly_total = poly_state.get("total_trades", 0)
    poly_wr = (sum(1 for t in poly_closed if float(t.get("pnl", 0)) > 0) / len(poly_closed) * 100) if poly_closed else 0
    poly_exposure = sum(p.get("cost_usd", 0) for p in poly_positions)
    poly_starting = poly_state.get("starting_bankroll", 50.0)
    poly_roi = ((poly_state.get("total_pnl", 0) / poly_starting) * 100) if poly_starting > 0 else 0

    # Crypto stats
    crypto_total = crypto_state.get("total_trades", 0)
    crypto_wr = (sum(1 for t in crypto_closed if float(t.get("pnl", 0)) > 0) / len(crypto_closed) * 100) if crypto_closed else 0
    crypto_exposure = sum(p.get("cost_usd", 0) for p in crypto_positions)
    crypto_starting = crypto_state.get("starting_bankroll", 50.0)
    crypto_roi = ((crypto_state.get("total_pnl", 0) / crypto_starting) * 100) if crypto_starting > 0 else 0
    crypto_avg = (crypto_state.get("total_pnl", 0) / crypto_total) if crypto_total > 0 else 0

    # Stock stats
    stock_total = stock_state.get("total_trades", 0)
    stock_wr = (sum(1 for t in stock_closed if float(t.get("pnl", 0)) > 0) / len(stock_closed) * 100) if stock_closed else 0
    stock_exposure = sum(p.get("cost_usd", 0) for p in stock_positions)
    stock_starting = stock_state.get("starting_bankroll", 1000.0)
    stock_roi = ((stock_state.get("total_pnl", 0) / stock_starting) * 100) if stock_starting > 0 else 0
    stock_avg = (stock_state.get("total_pnl", 0) / stock_total) if stock_total > 0 else 0

    combined_pnl = poly_state.get("total_pnl", 0) + crypto_state.get("total_pnl", 0) + stock_state.get("total_pnl", 0)

    # Advanced analytics
    def calc_analytics(closed_trades):
        if not closed_trades:
            return {"max_dd": 0, "best_trade": 0, "worst_trade": 0, "streak": 0, "streak_type": "",
                    "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "sharpe": 0}
        pnls = []
        for t in closed_trades:
            try:
                pnls.append(float(t.get("pnl", 0)))
            except (ValueError, TypeError):
                pass
        if not pnls:
            return {"max_dd": 0, "best_trade": 0, "worst_trade": 0, "streak": 0, "streak_type": "",
                    "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "sharpe": 0}

        # Max drawdown from equity curve
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Current streak
        streak = 0
        if pnls:
            last_sign = 1 if pnls[-1] >= 0 else -1
            for p in reversed(pnls):
                if (p >= 0 and last_sign > 0) or (p < 0 and last_sign < 0):
                    streak += 1
                else:
                    break
            streak *= last_sign

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        total_wins = sum(wins)
        total_losses = abs(sum(losses))
        profit_factor = total_wins / total_losses if total_losses > 0 else float('inf') if total_wins > 0 else 0

        # Simple Sharpe (per-trade)
        import math
        mean_pnl = sum(pnls) / len(pnls)
        if len(pnls) > 1:
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
            std_pnl = math.sqrt(variance) if variance > 0 else 0
            sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
        else:
            sharpe = 0

        return {
            "max_dd": round(max_dd, 4),
            "best_trade": round(max(pnls), 4),
            "worst_trade": round(min(pnls), 4),
            "streak": streak,
            "streak_type": "W" if streak > 0 else "L" if streak < 0 else "",
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
            "sharpe": round(sharpe, 2),
        }

    crypto_analytics = calc_analytics(crypto_closed)
    poly_analytics = calc_analytics(poly_closed)
    stock_analytics = calc_analytics(stock_closed)

    # Daily P&L bars
    def daily_pnl_bars(closed_trades):
        daily = defaultdict(float)
        for t in closed_trades:
            try:
                ts = t.get("closed_at", t.get("timestamp", ""))[:10]
                pnl = float(t.get("pnl", 0))
                if ts:
                    daily[ts] += pnl
            except (ValueError, TypeError):
                pass
        sorted_days = sorted(daily.items())
        return [{"day": d, "pnl": round(p, 4)} for d, p in sorted_days[-30:]]

    crypto_daily = daily_pnl_bars(crypto_closed)
    poly_daily = daily_pnl_bars(poly_closed)

    # P&L timelines (prefer full CSV history over capped state.json)
    crypto_pnl_timeline = compute_pnl_from_csv(CRYPTO_TRADES) or compute_pnl_timeline(crypto_closed)
    poly_pnl_timeline = compute_pnl_from_csv(PAPER_TRADES) or compute_pnl_timeline(poly_closed)

    # Signal type counts for crypto
    crypto_signal_counts = defaultdict(int)
    for t in crypto_closed:
        sig = t.get("signal_type", "unknown")
        crypto_signal_counts[sig] += 1

    # Crypto scalper model metrics
    crypto_config = {
        "min_confluence": 3,
        "min_grade": "C",
    }

    # Win rate by quality grade
    grade_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in crypto_closed:
        grade = t.get("quality_grade", "?")
        grade_stats[grade]["total"] += 1
        if float(t.get("pnl", 0)) > 0:
            grade_stats[grade]["wins"] += 1
    crypto_grade_wr = {}
    crypto_grade_wr_full = {}
    for g in sorted(grade_stats.keys()):
        s = grade_stats[g]
        wr = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
        crypto_grade_wr[g] = round(wr, 0)
        crypto_grade_wr_full[g] = {"wr": round(wr, 1), "n": s["total"]}

    # P&L by regime
    regime_pnl = defaultdict(float)
    for t in crypto_closed:
        regime = t.get("regime", "unknown")
        regime_pnl[regime] += float(t.get("pnl", 0))
    crypto_regime_pnl = {k: round(v, 4) for k, v in regime_pnl.items()}

    # Best regime
    crypto_best_regime = max(crypto_regime_pnl, key=crypto_regime_pnl.get) if crypto_regime_pnl else "N/A"

    # Avg confluence score
    scores = [int(t.get("confluence_score", 0)) for t in crypto_closed if t.get("confluence_score")]
    crypto_avg_score = sum(scores) / len(scores) if scores else 0

    # Score distribution data
    crypto_score_data = scores

    # Category counts for polymarket
    poly_cat_counts = defaultdict(int)
    for t in poly_closed:
        cat = t.get("category", "unknown")
        poly_cat_counts[cat] += 1

    # Edge distribution data (as percentages)
    poly_edge_data = []
    for t in poly_closed:
        edge = float(t.get("edge_at_entry", 0)) * 100
        if edge > 0:
            poly_edge_data.append(round(edge, 1))
    for p in poly_positions:
        edge = float(p.get("edge_at_entry", 0)) * 100
        if edge > 0:
            poly_edge_data.append(round(edge, 1))

    # Confidence breakdown
    poly_conf_counts = defaultdict(int)
    for t in poly_closed:
        conf = t.get("confidence", "unknown")
        poly_conf_counts[conf] += 1

    # P&L by confidence
    poly_conf_pnl = defaultdict(float)
    for t in poly_closed:
        conf = t.get("confidence", "unknown")
        poly_conf_pnl[conf] += float(t.get("pnl", 0))
    poly_conf_pnl = {k: round(v, 4) for k, v in poly_conf_pnl.items()}

    # Average edge across all trades
    all_edges = [float(t.get("edge_at_entry", 0)) for t in poly_closed] + \
                [float(p.get("edge_at_entry", 0)) for p in poly_positions]
    poly_avg_edge = sum(all_edges) / len(all_edges) if all_edges else 0

    # Stock analytics
    stock_daily = daily_pnl_bars(stock_closed)
    stock_pnl_timeline = compute_pnl_from_csv(STOCK_TRADES) or compute_pnl_timeline(stock_closed)

    # Stock sector counts (from open positions)
    stock_sector_counts = defaultdict(int)
    for p in stock_positions:
        sector = p.get("sector", "unknown")
        stock_sector_counts[sector] += 1
    # Also count from closed trades if no open positions
    if not stock_positions:
        for t in stock_closed[-20:]:
            sector = t.get("sector", "unknown")
            stock_sector_counts[sector] += 1

    # Stock grade win rate
    stock_grade_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in stock_closed:
        grade = t.get("quality_grade", "?")
        stock_grade_stats[grade]["total"] += 1
        if float(t.get("pnl", 0)) > 0:
            stock_grade_stats[grade]["wins"] += 1
    stock_grade_wr = {}
    stock_grade_wr_full = {}
    for g in sorted(stock_grade_stats.keys()):
        s = stock_grade_stats[g]
        wr = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
        stock_grade_wr[g] = round(wr, 0)
        stock_grade_wr_full[g] = {"wr": round(wr, 1), "n": s["total"]}

    # Stock P&L by regime
    stock_regime_pnl_data = defaultdict(float)
    for t in stock_closed:
        regime = t.get("regime", "unknown")
        stock_regime_pnl_data[regime] += float(t.get("pnl", 0))
    stock_regime_pnl = {k: round(v, 4) for k, v in stock_regime_pnl_data.items()}

    # Stock avg confluence score
    stock_scores = [int(t.get("confluence_score", 0)) for t in stock_closed if t.get("confluence_score")]
    stock_avg_score = sum(stock_scores) / len(stock_scores) if stock_scores else 0
    stock_score_data = stock_scores

    # Stock market open status
    stock_market_open = False
    try:
        import requests as _req
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        if api_key and secret_key:
            resp = _req.get(f"{base_url}/v2/clock",
                           headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
                           timeout=5)
            if resp.status_code == 200:
                stock_market_open = resp.json().get("is_open", False)
    except Exception:
        pass

    # Logs
    crypto_logs = load_recent_logs(CRYPTO_LOG_FILE)
    poly_logs = load_recent_logs(PAPER_LOG_FILE)
    stock_logs = load_recent_logs(STOCK_LOG_FILE)

    # Get active crypto pairs from state or default
    crypto_pairs = [
        "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD",
        "AVAX-USD", "LINK-USD", "XRP-USD", "SUI-USD",
        "ADA-USD", "DOT-USD", "NEAR-USD", "MATIC-USD",
        "UNI-USD", "ATOM-USD", "ARB-USD", "OP-USD",
    ]

    return render_template_string(
        TEMPLATE,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        combined_pnl=combined_pnl,
        # Crypto
        crypto=crypto_state,
        crypto_positions=crypto_positions,
        crypto_closed=crypto_closed,
        crypto_wr=crypto_wr,
        crypto_exposure=crypto_exposure,
        crypto_roi=crypto_roi,
        crypto_avg=crypto_avg,
        crypto_pnl_timeline=crypto_pnl_timeline,
        crypto_signal_counts=dict(crypto_signal_counts),
        crypto_config=crypto_config,
        crypto_grade_wr=crypto_grade_wr,
        crypto_grade_wr_full=crypto_grade_wr_full,
        crypto_regime_pnl=crypto_regime_pnl,
        crypto_best_regime=crypto_best_regime,
        crypto_avg_score=crypto_avg_score,
        crypto_score_data=crypto_score_data,
        crypto_logs=crypto_logs,
        crypto_analytics=crypto_analytics,
        crypto_daily=crypto_daily,
        # Polymarket
        poly=poly_state,
        poly_positions=poly_positions,
        poly_closed=poly_closed,
        poly_wr=poly_wr,
        poly_exposure=poly_exposure,
        poly_roi=poly_roi,
        poly_pnl_timeline=poly_pnl_timeline,
        poly_cat_counts=dict(poly_cat_counts),
        poly_edge_data=poly_edge_data,
        poly_conf_counts=dict(poly_conf_counts),
        poly_conf_pnl=dict(poly_conf_pnl),
        poly_avg_edge=poly_avg_edge,
        poly_logs=poly_logs,
        poly_analytics=poly_analytics,
        poly_daily=poly_daily,
        # Stocks
        stock=stock_state,
        stock_positions=stock_positions,
        stock_closed=stock_closed,
        stock_wr=stock_wr,
        stock_exposure=stock_exposure,
        stock_roi=stock_roi,
        stock_avg=stock_avg,
        stock_pnl_timeline=stock_pnl_timeline,
        stock_analytics=stock_analytics,
        stock_daily=stock_daily,
        stock_sector_counts=dict(stock_sector_counts),
        stock_grade_wr=stock_grade_wr,
        stock_grade_wr_full=stock_grade_wr_full,
        stock_regime_pnl=stock_regime_pnl,
        stock_avg_score=stock_avg_score,
        stock_score_data=stock_score_data,
        stock_market_open=stock_market_open,
        stock_logs=stock_logs,
        # Helper
        fmt_price=fmt_price,
        # Price charts
        crypto_pairs=crypto_pairs,
    )


@app.route("/api/status")
@auth_required
def api_status():
    return jsonify({
        "crypto": load_crypto_state(),
        "polymarket": load_paper_state(),
        "stocks": load_stock_state(),
    })


@app.route("/api/prices/<pair>")
@auth_required
def api_price_history(pair):
    """Fetch price history for a crypto pair from Coinbase.

    Paginates automatically for large requests (Coinbase max 300 per call).
    Supports full history via granularity + limit params.
    """
    import time as _time
    granularity = request.args.get("granularity", "ONE_HOUR")
    limit = int(request.args.get("limit", "168"))

    COINBASE_API = "https://api.exchange.coinbase.com"
    GRAN_SEC = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900,
                "ONE_HOUR": 3600, "SIX_HOUR": 21600, "ONE_DAY": 86400}

    gran_sec = GRAN_SEC.get(granularity, 3600)
    end_ts = int(_time.time())
    start_ts = end_ts - (gran_sec * limit)

    # Date format depends on timeframe for readability
    if gran_sec >= 86400:
        time_fmt = "%Y-%m-%d"
    elif gran_sec >= 3600:
        time_fmt = "%m/%d %H:%M"
    else:
        time_fmt = "%H:%M"

    try:
        all_raw = []
        # Coinbase returns max 300 candles per request — paginate backwards
        chunk_end = end_ts
        max_pages = 15  # Safety limit (15 * 300 = 4500 candles max)
        for _ in range(max_pages):
            chunk_start = max(start_ts, chunk_end - gran_sec * 300)
            resp = requests.get(
                f"{COINBASE_API}/products/{pair}/candles",
                params={"start": chunk_start, "end": chunk_end, "granularity": gran_sec},
                headers={"User-Agent": "TradingDashboard/1.0"},
                timeout=15,
            )
            if resp.status_code != 200:
                break
            raw = resp.json()
            if not raw:
                break
            all_raw.extend(raw)
            # Move window back
            oldest = min(c[0] for c in raw)
            if oldest <= start_ts:
                break
            chunk_end = oldest
            _time.sleep(0.15)  # Rate limit courtesy

        if not all_raw:
            return jsonify({"error": f"No data for {pair}"}), 404

        # Deduplicate by timestamp, sort oldest-first
        seen = set()
        unique = []
        for c in all_raw:
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)
        unique.sort(key=lambda x: x[0])

        candles = []
        for c in unique:
            candles.append({
                "time": datetime.fromtimestamp(c[0], tz=timezone.utc).strftime(time_fmt),
                "open": c[3], "high": c[2], "low": c[1], "close": c[4], "volume": c[5]
            })
        return jsonify(candles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    print(f"Dashboard running on http://0.0.0.0:{args.port}")
    print(f"Login: {DASH_USER} / {'*' * len(DASH_PASS)}")
    app.run(host="0.0.0.0", port=args.port)
