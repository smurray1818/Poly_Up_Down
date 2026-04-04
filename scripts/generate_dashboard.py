#!/usr/bin/env python3
"""
Generate docs/dashboard.html from logs/paper_trades.csv.

Charts:
  1. Cumulative P&L over time
  2. Win rate over time
  3. P&L-per-contract distribution (proxy for edge captured)
  4. Trades per hour of day
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CSV_PATH = PROJECT_ROOT / "logs" / "paper_trades.csv"
OUT_FILE = PROJECT_ROOT / "docs" / "dashboard.html"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("timestamp")]
    return rows


# ---------------------------------------------------------------------------
# Chart data builders
# ---------------------------------------------------------------------------

def cumulative_pnl_series(trades):
    """x = trade index, y = running_pnl in USD."""
    labels, data, colors = [], [], []
    for i, t in enumerate(trades, 1):
        labels.append(f"#{i}")
        pnl = float(t["running_pnl"])
        data.append(round(pnl, 4))
        colors.append("rgba(0,230,118,0.85)" if pnl >= 0 else "rgba(255,82,82,0.85)")
    return labels, data, colors


def win_rate_series(trades):
    """x = trade index, y = win_rate_pct."""
    labels = [f"#{i}" for i, _ in enumerate(trades, 1)]
    data = [round(float(t["win_rate_pct"]), 1) for t in trades]
    return labels, data


def pnl_per_contract_histogram(trades, buckets=12):
    """Histogram of pnl/size (edge captured per contract)."""
    values = []
    for t in trades:
        size = float(t["size"])
        if size > 0:
            values.append(round(float(t["pnl"]) / size, 4))

    if not values:
        return [], []

    lo, hi = min(values), max(values)
    if lo == hi:
        return [str(round(lo, 3))], [len(values)]

    step = (hi - lo) / buckets
    counts = defaultdict(int)
    for v in values:
        bucket = int((v - lo) / step)
        bucket = min(bucket, buckets - 1)  # clamp last value
        counts[bucket] += 1

    labels, data, bg = [], [], []
    for b in range(buckets):
        low = round(lo + b * step, 3)
        labels.append(f"{low:.3f}")
        data.append(counts[b])
        bg.append("rgba(0,230,118,0.75)" if low >= 0 else "rgba(255,82,82,0.75)")

    return labels, data, bg


def trades_per_hour(trades):
    """Bar chart: count of trades by hour of day (UTC)."""
    counts = defaultdict(int)
    for t in trades:
        try:
            # "2025-01-15 14:32:00 UTC"
            ts = t["timestamp"].replace(" UTC", "").strip()
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            counts[dt.hour] += 1
        except ValueError:
            pass
    labels = [f"{h:02d}:00" for h in range(24)]
    data = [counts[h] for h in range(24)]
    return labels, data


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------

def summary_stats(trades):
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "win_rate": "—",
            "total_pnl": "$0.00",
            "cum_return": "0.00%",
            "avg_pnl": "$0.00",
            "best_trade": "$0.00",
            "worst_trade": "$0.00",
        }
    last = trades[-1]
    pnls = [float(t["pnl"]) for t in trades]
    return {
        "total_trades": int(last["total_trades"]),
        "wins": int(last["wins"]),
        "win_rate": f"{float(last['win_rate_pct']):.1f}%",
        "total_pnl": f"{'+'if float(last['running_pnl'])>=0 else ''}${float(last['running_pnl']):.2f}",
        "cum_return": f"{float(last['cumulative_return_pct']):+.2f}%",
        "avg_pnl": f"{'+'if sum(pnls)/len(pnls)>=0 else ''}${sum(pnls)/len(pnls):.4f}",
        "best_trade": f"+${max(pnls):.4f}",
        "worst_trade": f"${min(pnls):.4f}",
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Paper Trading Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #00e676;
    --red: #ff5252;
    --blue: #58a6ff;
    --yellow: #e3b341;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}

  header {{
    padding: 20px 24px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
  }}
  header h1 {{ font-size: 1.25rem; font-weight: 600; }}
  header h1 span {{ color: var(--green); }}
  .updated {{ font-size: 0.75rem; color: var(--muted); }}

  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    padding: 20px 24px;
  }}
  .stat {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
  }}
  .stat-label {{ font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }}
  .stat-value {{ font-size: 1.3rem; font-weight: 700; }}
  .stat-value.pos {{ color: var(--green); }}
  .stat-value.neg {{ color: var(--red); }}
  .stat-value.neutral {{ color: var(--text); }}

  .charts-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    gap: 16px;
    padding: 0 24px 24px;
  }}
  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px 12px;
  }}
  .chart-card h2 {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 14px; }}
  .chart-wrapper {{ position: relative; height: 240px; }}

  .empty-notice {{
    text-align: center;
    padding: 80px 24px;
    color: var(--muted);
    font-size: 0.95rem;
  }}
  .empty-notice strong {{ display: block; font-size: 1.2rem; color: var(--text); margin-bottom: 8px; }}
</style>
</head>
<body>
<header>
  <h1>Polymarket <span>Paper Trades</span> Dashboard</h1>
  <span class="updated">Updated {updated_utc}</span>
</header>

{body}

<script>
Chart.defaults.color = "#8b949e";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
Chart.defaults.font.size = 11;

{scripts}
</script>
</body>
</html>
"""

EMPTY_BODY = """\
<div class="empty-notice">
  <strong>No trades yet</strong>
  Run the bot with <code>PAPER_TRADING=true</code> and push <code>logs/paper_trades.csv</code>
  after the first 15-minute window closes.
</div>
"""

STATS_TEMPLATE = """\
<div class="stats-grid">
  <div class="stat">
    <div class="stat-label">Total Trades</div>
    <div class="stat-value neutral">{total_trades}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Win Rate</div>
    <div class="stat-value {wr_class}">{win_rate}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Cumulative P&amp;L</div>
    <div class="stat-value {pnl_class}">{total_pnl}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Cum. Return</div>
    <div class="stat-value {ret_class}">{cum_return}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Avg P&amp;L / Trade</div>
    <div class="stat-value {avg_class}">{avg_pnl}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Best Trade</div>
    <div class="stat-value pos">{best_trade}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Worst Trade</div>
    <div class="stat-value neg">{worst_trade}</div>
  </div>
  <div class="stat">
    <div class="stat-label">W / L</div>
    <div class="stat-value neutral">{wins} / {losses}</div>
  </div>
</div>
"""

CHARTS_BODY = """\
<div class="charts-grid">
  <div class="chart-card">
    <h2>Cumulative P&amp;L (USD)</h2>
    <div class="chart-wrapper"><canvas id="pnlChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h2>Win Rate Over Time (%)</h2>
    <div class="chart-wrapper"><canvas id="winRateChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h2>P&amp;L per Contract Distribution</h2>
    <div class="chart-wrapper"><canvas id="edgeChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h2>Trades per Hour (UTC)</h2>
    <div class="chart-wrapper"><canvas id="hourChart"></canvas></div>
  </div>
</div>
"""

CHART_SCRIPTS = """\
// 1. Cumulative P&L
(function() {{
  const labels = {pnl_labels};
  const data   = {pnl_data};
  const colors = {pnl_colors};
  new Chart(document.getElementById("pnlChart"), {{
    type: "line",
    data: {{
      labels,
      datasets: [{{
        label: "Running P&L ($)",
        data,
        borderColor: "#00e676",
        backgroundColor: "rgba(0,230,118,0.08)",
        pointBackgroundColor: colors,
        pointRadius: 4,
        pointHoverRadius: 6,
        tension: 0.3,
        fill: true,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
        label: ctx => ` ${{ctx.parsed.y >= 0 ? "+" : ""}}$${{ctx.parsed.y.toFixed(4)}}`
      }}}} }},
      scales: {{
        x: {{ ticks: {{ maxTicksLimit: 10 }} }},
        y: {{ grid: {{ color: "rgba(48,54,61,0.6)" }},
               ticks: {{ callback: v => (v >= 0 ? "+" : "") + "$" + v.toFixed(2) }} }}
      }}
    }}
  }});
}})();

// 2. Win Rate
(function() {{
  const labels = {wr_labels};
  const data   = {wr_data};
  new Chart(document.getElementById("winRateChart"), {{
    type: "line",
    data: {{
      labels,
      datasets: [{{
        label: "Win Rate (%)",
        data,
        borderColor: "#58a6ff",
        backgroundColor: "rgba(88,166,255,0.08)",
        pointRadius: 3,
        pointHoverRadius: 5,
        tension: 0.3,
        fill: true,
      }}, {{
        label: "50% baseline",
        data: labels.map(() => 50),
        borderColor: "rgba(139,148,158,0.4)",
        borderDash: [4, 4],
        pointRadius: 0,
        fill: false,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
        label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toFixed(1)}}%`
      }}}} }},
      scales: {{
        x: {{ ticks: {{ maxTicksLimit: 10 }} }},
        y: {{ min: 0, max: 100, grid: {{ color: "rgba(48,54,61,0.6)" }},
               ticks: {{ callback: v => v + "%" }} }}
      }}
    }}
  }});
}})();

// 3. P&L per contract histogram
(function() {{
  const labels = {edge_labels};
  const data   = {edge_data};
  const bg     = {edge_colors};
  new Chart(document.getElementById("edgeChart"), {{
    type: "bar",
    data: {{ labels, datasets: [{{ label: "Count", data, backgroundColor: bg }}] }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 45, font: {{ size: 10 }} }} }},
        y: {{ grid: {{ color: "rgba(48,54,61,0.6)" }},
               ticks: {{ stepSize: 1 }} }}
      }}
    }}
  }});
}})();

// 4. Trades per hour
(function() {{
  const labels = {hour_labels};
  const data   = {hour_data};
  new Chart(document.getElementById("hourChart"), {{
    type: "bar",
    data: {{ labels, datasets: [{{
      label: "Trades",
      data,
      backgroundColor: "rgba(227,179,65,0.7)",
      borderColor: "rgba(227,179,65,1)",
      borderWidth: 1,
    }}] }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 45, font: {{ size: 10 }} }} }},
        y: {{ grid: {{ color: "rgba(48,54,61,0.6)" }},
               ticks: {{ stepSize: 1 }} }}
      }}
    }}
  }});
}})();
"""


# ---------------------------------------------------------------------------
# Build page
# ---------------------------------------------------------------------------

def _color_class(value_str: str) -> str:
    """Return pos/neg/neutral CSS class based on first char."""
    s = value_str.lstrip()
    if s.startswith("+"):
        return "pos"
    if s.startswith("-"):
        return "neg"
    return "neutral"


def generate(trades: list[dict]) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not trades:
        return HTML_TEMPLATE.format(
            updated_utc=now_utc,
            body=EMPTY_BODY,
            scripts="",
        )

    stats = summary_stats(trades)

    pnl_labels, pnl_data, pnl_colors = cumulative_pnl_series(trades)
    wr_labels, wr_data = win_rate_series(trades)
    edge_labels, edge_data, edge_colors = pnl_per_contract_histogram(trades)
    hour_labels, hour_data = trades_per_hour(trades)

    stats_html = STATS_TEMPLATE.format(
        **stats,
        losses=stats["total_trades"] - stats["wins"],
        wr_class=_color_class(stats["win_rate"]) if float(stats["win_rate"].rstrip("%")) >= 50 else "neg",
        pnl_class=_color_class(stats["total_pnl"]),
        ret_class=_color_class(stats["cum_return"]),
        avg_class=_color_class(stats["avg_pnl"]),
    )

    scripts = CHART_SCRIPTS.format(
        pnl_labels=json.dumps(pnl_labels),
        pnl_data=json.dumps(pnl_data),
        pnl_colors=json.dumps(pnl_colors),
        wr_labels=json.dumps(wr_labels),
        wr_data=json.dumps(wr_data),
        edge_labels=json.dumps(edge_labels),
        edge_data=json.dumps(edge_data),
        edge_colors=json.dumps(edge_colors),
        hour_labels=json.dumps(hour_labels),
        hour_data=json.dumps(hour_data),
    )

    return HTML_TEMPLATE.format(
        updated_utc=now_utc,
        body=stats_html + CHARTS_BODY,
        scripts=scripts,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_badges(trades: list[dict], out_dir: Path) -> None:
    """
    Write shields.io endpoint JSON files so README badges stay live without
    ever modifying README.md.

    Served from GitHub Pages; referenced in README as:
      https://img.shields.io/endpoint?url=https://seanmurray.github.io/Poly_Up_Down/badge_winrate.json
    """
    # Win-rate badge
    if trades:
        wr = float(trades[-1]["win_rate_pct"])
        wr_color = "brightgreen" if wr >= 55 else ("yellow" if wr >= 45 else "red")
        wr_msg = f"{wr:.1f}%"
    else:
        wr_color = "lightgrey"
        wr_msg = "no data"

    # P&L badge
    if trades:
        pnl = float(trades[-1]["running_pnl"])
        pnl_color = "brightgreen" if pnl > 0 else ("red" if pnl < 0 else "lightgrey")
        pnl_msg = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
    else:
        pnl_color = "lightgrey"
        pnl_msg = "no data"

    # Trade count badge
    total = int(trades[-1]["total_trades"]) if trades else 0

    badges = {
        "badge_winrate.json": {
            "schemaVersion": 1,
            "label": "win rate",
            "message": wr_msg,
            "color": wr_color,
            "namedLogo": "chartdotjs",
        },
        "badge_pnl.json": {
            "schemaVersion": 1,
            "label": "paper P&L",
            "message": pnl_msg,
            "color": pnl_color,
            "namedLogo": "chartdotjs",
        },
        "badge_trades.json": {
            "schemaVersion": 1,
            "label": "trades",
            "message": str(total),
            "color": "blue",
            "namedLogo": "chartdotjs",
        },
    }

    for filename, payload in badges.items():
        (out_dir / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Badge written: {out_dir / filename}")


if __name__ == "__main__":
    trades = load_trades(CSV_PATH)
    print(f"Loaded {len(trades)} trades from {CSV_PATH}")

    html = generate(trades)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {OUT_FILE}")

    generate_badges(trades, OUT_FILE.parent)
    print("Done.")
