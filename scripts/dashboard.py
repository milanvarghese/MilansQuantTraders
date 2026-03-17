"""Web dashboard for monitoring the Polymarket weather trading bot."""

import csv
import json
import os
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template_string, request, Response

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISK_STATE = os.path.join(BASE_DIR, "logs", "risk_state.json")
TRADES_LOG = os.path.join(BASE_DIR, "logs", "trades.log")
PAPER_LOG = os.path.join(BASE_DIR, "logs", "paper_trades.csv")

# Auth — change these!
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


def load_risk_state():
    if os.path.exists(RISK_STATE):
        with open(RISK_STATE) as f:
            return json.load(f)
    return {
        "bankroll": 43.0, "open_positions": [], "daily_pnl": 0,
        "total_pnl": 0, "total_trades": 0, "winning_trades": 0,
        "is_paused": False, "pause_reason": "",
    }


def load_paper_trades():
    trades = []
    if os.path.exists(PAPER_LOG):
        with open(PAPER_LOG) as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    return trades[-50:]  # Last 50


def load_recent_logs(n=30):
    lines = []
    if os.path.exists(TRADES_LOG):
        with open(TRADES_LOG) as f:
            lines = f.readlines()
    return lines[-n:]


TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Weather Bot</title>
<meta http-equiv="refresh" content="60">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Courier New', monospace; background: #0a0a0a; color: #e0e0e0; padding: 20px; }
  h1 { color: #00ff88; margin-bottom: 5px; font-size: 1.4em; }
  .subtitle { color: #666; margin-bottom: 20px; font-size: 0.85em; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
  .card { background: #141414; border: 1px solid #222; border-radius: 8px; padding: 15px; }
  .card .label { color: #888; font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { font-size: 1.6em; font-weight: bold; margin-top: 5px; }
  .green { color: #00ff88; }
  .red { color: #ff4444; }
  .yellow { color: #ffaa00; }
  .neutral { color: #aaa; }
  h2 { color: #00ff88; margin: 25px 0 10px; font-size: 1.1em; border-bottom: 1px solid #222; padding-bottom: 5px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
  th { text-align: left; color: #888; padding: 8px; border-bottom: 1px solid #333; text-transform: uppercase; font-size: 0.75em; letter-spacing: 1px; }
  td { padding: 8px; border-bottom: 1px solid #1a1a1a; }
  tr:hover { background: #1a1a1a; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75em; }
  .badge-active { background: #003322; color: #00ff88; }
  .badge-paused { background: #332200; color: #ffaa00; }
  .log-box { background: #0d0d0d; border: 1px solid #222; border-radius: 8px; padding: 12px; font-size: 0.72em; max-height: 300px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.6; }
  .empty { color: #555; font-style: italic; padding: 20px; text-align: center; }
</style>
</head>
<body>
<h1>POLYMARKET WEATHER BOT</h1>
<p class="subtitle">Auto-refreshes every 60s &middot; {{ now }}</p>

<div class="grid">
  <div class="card">
    <div class="label">Bankroll</div>
    <div class="value green">${{ "%.2f"|format(state.bankroll) }}</div>
  </div>
  <div class="card">
    <div class="label">Total P&L</div>
    <div class="value {{ 'green' if state.total_pnl >= 0 else 'red' }}">
      ${{ "%+.2f"|format(state.total_pnl) }}
    </div>
  </div>
  <div class="card">
    <div class="label">Daily P&L</div>
    <div class="value {{ 'green' if state.daily_pnl >= 0 else 'red' }}">
      ${{ "%+.2f"|format(state.daily_pnl) }}
    </div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value {{ 'green' if win_rate >= 55 else 'yellow' if win_rate >= 45 else 'red' }}">
      {{ "%.0f"|format(win_rate) }}%
    </div>
  </div>
  <div class="card">
    <div class="label">Trades</div>
    <div class="value neutral">{{ state.total_trades }}</div>
  </div>
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value neutral">{{ state.open_positions|length }}</div>
  </div>
  <div class="card">
    <div class="label">Exposure</div>
    <div class="value neutral">${{ "%.2f"|format(exposure) }}</div>
  </div>
  <div class="card">
    <div class="label">Status</div>
    <div class="value">
      {% if state.is_paused %}
        <span class="badge badge-paused">PAUSED</span>
      {% else %}
        <span class="badge badge-active">ACTIVE</span>
      {% endif %}
    </div>
  </div>
</div>

{% if state.open_positions %}
<h2>Open Positions</h2>
<table>
  <tr><th>City</th><th>Bucket</th><th>Entry</th><th>Shares</th><th>Cost</th><th>Time</th></tr>
  {% for p in state.open_positions %}
  <tr>
    <td>{{ p.city }}</td>
    <td>{{ p.bucket }}</td>
    <td>${{ "%.3f"|format(p.entry_price) }}</td>
    <td>{{ "%.1f"|format(p.size_shares) }}</td>
    <td>${{ "%.2f"|format(p.cost_usd) }}</td>
    <td>{{ p.timestamp[:16] }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}

<h2>Paper Trades (Last 50)</h2>
{% if paper_trades %}
<table>
  <tr><th>Time</th><th>City</th><th>Bucket</th><th>Model</th><th>Market</th><th>Edge</th><th>Size</th></tr>
  {% for t in paper_trades|reverse %}
  <tr>
    <td>{{ t.timestamp[:16] if t.timestamp else '' }}</td>
    <td>{{ t.city }}</td>
    <td>{{ t.bucket_low }}-{{ t.bucket_high }}°C</td>
    <td>{{ "%.1f"|format(t.model_prob|float * 100) }}%</td>
    <td>{{ "%.1f"|format(t.market_price|float * 100) }}%</td>
    <td class="green">{{ "%.1f"|format(t.edge|float * 100) }}%</td>
    <td>${{ t.kelly_size }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}
<div class="empty">No paper trades yet — waiting for weather markets to appear</div>
{% endif %}

<h2>Recent Activity Log</h2>
<div class="log-box">{% if logs %}{% for line in logs %}{{ line }}{% endfor %}{% else %}No log entries yet{% endif %}</div>

</body>
</html>"""


@app.route("/")
@auth_required
def index():
    state = load_risk_state()
    paper_trades = load_paper_trades()
    logs = load_recent_logs()
    total = state.get("total_trades", 0)
    wins = state.get("winning_trades", 0)
    win_rate = (wins / total * 100) if total > 0 else 0
    exposure = sum(p.get("cost_usd", 0) for p in state.get("open_positions", []))

    return render_template_string(
        TEMPLATE,
        state=state,
        paper_trades=paper_trades,
        logs=logs,
        win_rate=win_rate,
        exposure=exposure,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/api/status")
@auth_required
def api_status():
    return load_risk_state()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    print(f"Dashboard running on http://0.0.0.0:{args.port}")
    print(f"Login: {DASH_USER} / {DASH_PASS}")
    app.run(host="0.0.0.0", port=args.port)
