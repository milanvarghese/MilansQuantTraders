"""Web dashboard for monitoring Polymarket paper trading.

Features:
- Real-time P&L chart (bankroll over time)
- Edge distribution histogram
- Win rate gauge
- Category performance breakdown
- Open positions with unrealized P&L
- Trade history with sorting
- Live activity log
"""

import csv
import json
import os
import secrets
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template_string, request, Response, jsonify

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DIR = os.path.join(BASE_DIR, "paper_trading")
PAPER_STATE = os.path.join(PAPER_DIR, "state.json")
PAPER_TRADES = os.path.join(PAPER_DIR, "trades.csv")
PAPER_LOG_FILE = os.path.join(PAPER_DIR, "paper_trading.log")

# Auth
DASH_USER = os.getenv("DASH_USER", "admin")
DASH_PASS = os.getenv("DASH_PASS", "changeme123")

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)


def check_auth(username, password):
    return username == DASH_USER and password == DASH_PASS


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="Polymarket Bot"'},
            )
        return f(*args, **kwargs)
    return decorated


def load_paper_state():
    if os.path.exists(PAPER_STATE):
        with open(PAPER_STATE) as f:
            return json.load(f)
    return {
        "bankroll": 50.0, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
        "positions": [], "closed_trades": [], "total_trades": 0,
        "winning_trades": 0, "total_pnl": 0.0, "daily_pnl": 0.0,
        "daily_trade_count": 0, "last_scan": "",
    }


def load_trade_log():
    trades = []
    if os.path.exists(PAPER_TRADES):
        with open(PAPER_TRADES) as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    return trades


def load_recent_logs(n=50):
    lines = []
    if os.path.exists(PAPER_LOG_FILE):
        with open(PAPER_LOG_FILE) as f:
            lines = f.readlines()
    return lines[-n:]


def compute_analytics(state, trades):
    """Compute chart data and analytics from paper state and trades."""
    # P&L timeline from trade log (cumulative realized P&L)
    pnl_timeline = []
    cumulative = 0.0
    for t in trades:
        try:
            pnl = float(t.get("pnl", 0))
            if pnl != 0:
                cumulative += pnl
                ts = t.get("timestamp", "")[:16]
                pnl_timeline.append({"time": ts, "pnl": round(cumulative, 2)})
        except (ValueError, TypeError):
            pass

    # Edge distribution (for histogram)
    edges = []
    for t in trades:
        try:
            edge = float(t.get("edge", 0))
            if edge > 0:
                edges.append(round(edge * 100, 1))
        except (ValueError, TypeError):
            pass

    # Category breakdown (replaces city breakdown)
    cat_stats = defaultdict(lambda: {"count": 0, "total_edge": 0.0, "total_size": 0.0})
    for t in trades:
        cat = t.get("category", "Unknown") or "Unknown"
        try:
            cat_stats[cat]["count"] += 1
            cat_stats[cat]["total_edge"] += abs(float(t.get("edge", 0)))
            cat_stats[cat]["total_size"] += float(t.get("cost_usd", 0))
        except (ValueError, TypeError):
            pass

    cat_data = []
    for cat, stats in sorted(cat_stats.items(), key=lambda x: -x[1]["count"]):
        avg_edge = (stats["total_edge"] / stats["count"] * 100) if stats["count"] > 0 else 0
        cat_data.append({
            "category": cat,
            "trades": stats["count"],
            "avg_edge": round(avg_edge, 1),
            "total_size": round(stats["total_size"], 2),
        })

    # Hourly distribution
    hour_counts = defaultdict(int)
    for t in trades:
        try:
            ts = t.get("timestamp", "")
            if ts:
                hour = int(ts[11:13])
                hour_counts[hour] += 1
        except (ValueError, IndexError):
            pass
    hourly = [{"hour": h, "count": hour_counts.get(h, 0)} for h in range(24)]

    return {
        "pnl_timeline": pnl_timeline,
        "edges": edges,
        "cat_data": cat_data,
        "hourly": hourly,
    }


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Paper Trading</title>
<meta http-equiv="refresh" content="30">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', Roboto, monospace; background: #0a0e17; color: #c9d1d9; }

  .topbar { background: #161b22; border-bottom: 1px solid #21262d; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; }
  .topbar h1 { font-size: 1.1em; color: #58a6ff; letter-spacing: 1px; }
  .topbar .meta { font-size: 0.75em; color: #8b949e; }

  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

  /* Stats grid */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 14px; }
  .stat .label { font-size: 0.65em; text-transform: uppercase; letter-spacing: 1.5px; color: #8b949e; margin-bottom: 4px; }
  .stat .val { font-size: 1.5em; font-weight: 700; }

  .g { color: #3fb950; }
  .r { color: #f85149; }
  .y { color: #d29922; }
  .b { color: #58a6ff; }
  .n { color: #8b949e; }

  /* Charts */
  .charts { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 20px; }
  .chart-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; }
  .chart-card h3 { font-size: 0.8em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  .chart-wrap { position: relative; height: 220px; }

  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }

  /* Tables */
  .section { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .section h2 { font-size: 0.85em; color: #58a6ff; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78em; }
  th { text-align: left; color: #8b949e; padding: 8px 6px; border-bottom: 1px solid #21262d; font-size: 0.7em; text-transform: uppercase; letter-spacing: 1px; }
  td { padding: 7px 6px; border-bottom: 1px solid #161b22; }
  tr:hover { background: #1c2333; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; }
  .badge-active { background: #0d2818; color: #3fb950; border: 1px solid #238636; }
  .badge-paper { background: #1c1d5e; color: #a5b4fc; border: 1px solid #6366f1; }

  .log-box { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 10px 12px; font-size: 0.68em; max-height: 250px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.7; font-family: 'JetBrains Mono', 'Fira Code', monospace; color: #8b949e; }
  .empty { color: #484f58; font-style: italic; padding: 20px; text-align: center; }

  @media (max-width: 800px) {
    .charts, .row2 { grid-template-columns: 1fr; }
    .stats { grid-template-columns: repeat(2, 1fr); }
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>POLYMARKET PAPER TRADER</h1>
  <div class="meta">
    <span class="badge badge-paper">PAPER TRADING</span>
    &nbsp;&middot;&nbsp; {{ now }} &nbsp;&middot;&nbsp; refreshes every 30s
    {% if state.last_scan %}
    &nbsp;&middot;&nbsp; last scan: {{ state.last_scan[:19] }}
    {% endif %}
  </div>
</div>

<div class="container">

<!-- Stats Row -->
<div class="stats">
  <div class="stat">
    <div class="label">Bankroll</div>
    <div class="val g">${{ "%.2f"|format(state.bankroll) }}</div>
  </div>
  <div class="stat">
    <div class="label">Starting</div>
    <div class="val n">${{ "%.2f"|format(state.starting_bankroll) }}</div>
  </div>
  <div class="stat">
    <div class="label">Peak</div>
    <div class="val b">${{ "%.2f"|format(state.peak_bankroll) }}</div>
  </div>
  <div class="stat">
    <div class="label">Total P&L</div>
    <div class="val {{ 'g' if state.total_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(state.total_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Daily P&L</div>
    <div class="val {{ 'g' if state.daily_pnl >= 0 else 'r' }}">${{ "%+.2f"|format(state.daily_pnl) }}</div>
  </div>
  <div class="stat">
    <div class="label">Unrealized</div>
    <div class="val {{ 'g' if unrealized >= 0 else 'r' }}">${{ "%+.2f"|format(unrealized) }}</div>
  </div>
  <div class="stat">
    <div class="label">Win Rate</div>
    <div class="val {{ 'g' if win_rate >= 55 else 'y' if win_rate >= 45 else 'r' }}">{{ "%.0f"|format(win_rate) }}%</div>
  </div>
  <div class="stat">
    <div class="label">Trades</div>
    <div class="val n">{{ state.total_trades }}</div>
  </div>
  <div class="stat">
    <div class="label">Open / Exposure</div>
    <div class="val n">{{ positions|length }} / ${{ "%.2f"|format(exposure) }}</div>
  </div>
  <div class="stat">
    <div class="label">ROI</div>
    <div class="val {{ 'g' if roi >= 0 else 'r' }}">{{ "%+.1f"|format(roi) }}%</div>
  </div>
  <div class="stat">
    <div class="label">Drawdown</div>
    <div class="val {{ 'r' if drawdown >= 15 else 'y' if drawdown >= 8 else 'g' }}">{{ "%.1f"|format(drawdown) }}%</div>
  </div>
</div>

<!-- Charts Row 1: P&L + Edge Distribution -->
<div class="charts">
  <div class="chart-card">
    <h3>Realized P&L Over Time</h3>
    <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Edge Distribution (%)</h3>
    <div class="chart-wrap"><canvas id="edgeChart"></canvas></div>
  </div>
</div>

<!-- Charts Row 2: Category Performance + Hourly Activity -->
<div class="row2">
  <div class="chart-card">
    <h3>Trades by Category</h3>
    <div class="chart-wrap"><canvas id="catChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Trade Activity by Hour (UTC)</h3>
    <div class="chart-wrap"><canvas id="hourChart"></canvas></div>
  </div>
</div>

<!-- Category Performance Table -->
{% if analytics.cat_data %}
<div class="section">
  <h2>Category Performance</h2>
  <table>
    <tr><th>Category</th><th>Trades</th><th>Avg Edge</th><th>Total Size</th></tr>
    {% for c in analytics.cat_data %}
    <tr>
      <td>{{ c.category }}</td>
      <td>{{ c.trades }}</td>
      <td class="g">{{ c.avg_edge }}%</td>
      <td>${{ c.total_size }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Open Positions -->
{% if positions %}
<div class="section">
  <h2>Open Positions ({{ positions|length }})</h2>
  <table>
    <tr><th>ID</th><th>Side</th><th>Market</th><th>Category</th><th>Entry</th><th>Current</th><th>Shares</th><th>Cost</th><th>P&L</th><th>Edge</th><th>Opened</th></tr>
    {% for p in positions %}
    <tr>
      <td>{{ p.id }}</td>
      <td><span class="{{ 'g' if p.side == 'YES' else 'r' }}">{{ p.side }}</span></td>
      <td>{{ p.market_question[:55] }}</td>
      <td>{{ p.category }}</td>
      <td>{{ "%.1f"|format(p.entry_price * 100) }}c</td>
      <td>{{ "%.1f"|format(p.get('current_price', p.entry_price) * 100) }}c</td>
      <td>{{ "%.1f"|format(p.shares) }}</td>
      <td>${{ "%.2f"|format(p.cost_usd) }}</td>
      <td class="{{ 'g' if p.get('unrealized_pnl', 0) >= 0 else 'r' }}">${{ "%+.2f"|format(p.get('unrealized_pnl', 0)) }}</td>
      <td class="g">{{ "%.1f"|format(p.edge_at_entry * 100) }}%</td>
      <td>{{ p.opened_at[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Closed Trades -->
{% if closed_trades %}
<div class="section">
  <h2>Closed Trades (Last 50 of {{ closed_trades|length }})</h2>
  <table>
    <tr><th>ID</th><th>Side</th><th>Market</th><th>Category</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th><th>Closed</th></tr>
    {% for t in closed_trades[-50:]|reverse %}
    <tr>
      <td>{{ t.id }}</td>
      <td><span class="{{ 'g' if t.side == 'YES' else 'r' }}">{{ t.side }}</span></td>
      <td>{{ t.market_question[:55] }}</td>
      <td>{{ t.category }}</td>
      <td>{{ "%.1f"|format(t.entry_price * 100) }}c</td>
      <td>{{ "%.1f"|format(t.exit_price * 100) }}c</td>
      <td class="{{ 'g' if t.pnl >= 0 else 'r' }}">${{ "%+.2f"|format(t.pnl) }}</td>
      <td>{{ t.reason }}</td>
      <td>{{ t.closed_at[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Trade Log (CSV) -->
<div class="section">
  <h2>Trade Log (Last 100)</h2>
  {% if trade_log %}
  <table>
    <tr><th>Time</th><th>Action</th><th>Market</th><th>Category</th><th>Side</th><th>Price</th><th>Shares</th><th>Cost</th><th>P&L</th><th>Edge</th><th>Confidence</th></tr>
    {% for t in trade_log[-100:]|reverse %}
    <tr>
      <td>{{ t.get('timestamp', '')[:16] }}</td>
      <td><span class="{{ 'g' if t.action == 'OPEN' else 'y' }}">{{ t.action }}</span></td>
      <td>{{ t.get('market_question', '')[:50] }}</td>
      <td>{{ t.get('category', '') }}</td>
      <td>{{ t.get('side', '') }}</td>
      <td>{{ "%.1f"|format(t.get('price', '0')|float * 100) }}c</td>
      <td>{{ t.get('shares', '') }}</td>
      <td>${{ t.get('cost_usd', '0') }}</td>
      <td class="{{ 'g' if t.get('pnl', '0')|float >= 0 else 'r' }}">${{ t.get('pnl', '0') }}</td>
      <td class="g">{{ "%.1f"|format(t.get('edge', '0')|float * 100) }}%</td>
      <td>{{ t.get('confidence', '') }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="empty">No trades yet -- run: cd scripts && python paper_trader.py --scan-once</div>
  {% endif %}
</div>

<!-- Activity Log -->
<div class="section">
  <h2>Activity Log (Last 50)</h2>
  <div class="log-box">{% if logs %}{% for line in logs %}{{ line }}{% endfor %}{% else %}No log entries yet{% endif %}</div>
</div>

</div>

<script>
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } },
};

// P&L Chart
const pnlData = {{ pnl_timeline | tojson }};
if (pnlData.length > 0) {
  new Chart(document.getElementById('pnlChart'), {
    type: 'line',
    data: {
      labels: pnlData.map(d => d.time),
      datasets: [{
        data: pnlData.map(d => d.pnl),
        borderColor: pnlData[pnlData.length-1].pnl >= 0 ? '#3fb950' : '#f85149',
        backgroundColor: pnlData[pnlData.length-1].pnl >= 0 ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      }]
    },
    options: {
      ...chartDefaults,
      scales: {
        x: { display: false },
        y: {
          grid: { color: '#21262d' },
          ticks: { color: '#8b949e', callback: v => '$' + v.toFixed(2) }
        }
      }
    }
  });
}

// Edge Distribution Histogram
const edges = {{ edges | tojson }};
if (edges.length > 0) {
  const bins = {};
  edges.forEach(e => {
    const bin = Math.floor(e / 2) * 2;
    const label = bin + '-' + (bin + 2) + '%';
    bins[label] = (bins[label] || 0) + 1;
  });
  const sortedLabels = Object.keys(bins).sort((a, b) => parseFloat(a) - parseFloat(b));
  new Chart(document.getElementById('edgeChart'), {
    type: 'bar',
    data: {
      labels: sortedLabels,
      datasets: [{
        data: sortedLabels.map(l => bins[l]),
        backgroundColor: '#58a6ff',
        borderRadius: 4,
      }]
    },
    options: {
      ...chartDefaults,
      scales: {
        x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 10 } } },
        y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e' } }
      }
    }
  });
}

// Category Chart (replaces City chart)
const catData = {{ cat_data | tojson }};
if (catData.length > 0) {
  const colors = ['#3fb950','#58a6ff','#d29922','#f85149','#bc8cff','#39d2c0','#ff7b72','#79c0ff','#ffa657','#d2a8ff','#7ee787'];
  new Chart(document.getElementById('catChart'), {
    type: 'doughnut',
    data: {
      labels: catData.map(c => c.category),
      datasets: [{
        data: catData.map(c => c.trades),
        backgroundColor: colors.slice(0, catData.length),
        borderWidth: 0,
      }]
    },
    options: {
      ...chartDefaults,
      plugins: {
        legend: { display: true, position: 'right', labels: { color: '#8b949e', font: { size: 11 }, padding: 8 } }
      }
    }
  });
}

// Hourly Activity
const hourly = {{ hourly | tojson }};
new Chart(document.getElementById('hourChart'), {
  type: 'bar',
  data: {
    labels: hourly.map(h => h.hour + ':00'),
    datasets: [{
      data: hourly.map(h => h.count),
      backgroundColor: hourly.map(h => [0,6,12,18].includes(h.hour) ? '#d29922' : '#21262d'),
      borderRadius: 3,
    }]
  },
  options: {
    ...chartDefaults,
    scales: {
      x: { grid: { display: false }, ticks: { color: '#8b949e', font: { size: 9 }, maxRotation: 0 } },
      y: { grid: { color: '#21262d' }, ticks: { color: '#8b949e' } }
    }
  }
});
</script>
</body>
</html>"""


@app.route("/")
@auth_required
def index():
    state = load_paper_state()
    trade_log = load_trade_log()
    logs = load_recent_logs()

    positions = state.get("positions", [])
    closed_trades = state.get("closed_trades", [])

    total = state.get("total_trades", 0)
    wins = state.get("winning_trades", 0)
    win_rate = (wins / total * 100) if total > 0 else 0

    exposure = sum(p.get("cost_usd", 0) for p in positions)
    unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

    starting = state.get("starting_bankroll", 50.0)
    roi = ((state.get("total_pnl", 0) / starting) * 100) if starting > 0 else 0

    peak = state.get("peak_bankroll", state.get("bankroll", 50.0))
    drawdown = ((1 - state.get("bankroll", 50.0) / peak) * 100) if peak > 0 else 0

    analytics = compute_analytics(state, trade_log)

    return render_template_string(
        TEMPLATE,
        state=state,
        positions=positions,
        closed_trades=closed_trades,
        trade_log=trade_log,
        logs=logs,
        win_rate=win_rate,
        exposure=exposure,
        unrealized=unrealized,
        roi=roi,
        drawdown=drawdown,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        analytics=analytics,
        pnl_timeline=analytics["pnl_timeline"],
        edges=analytics["edges"],
        cat_data=analytics["cat_data"],
        hourly=analytics["hourly"],
    )


@app.route("/api/status")
@auth_required
def api_status():
    return jsonify(load_paper_state())


@app.route("/api/trades")
@auth_required
def api_trades():
    return jsonify(load_trade_log()[-200:])


@app.route("/api/analytics")
@auth_required
def api_analytics():
    state = load_paper_state()
    return jsonify(compute_analytics(state, load_trade_log()))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    print(f"Dashboard running on http://0.0.0.0:{args.port}")
    print(f"Login: {DASH_USER} / {DASH_PASS}")
    app.run(host="0.0.0.0", port=args.port)
