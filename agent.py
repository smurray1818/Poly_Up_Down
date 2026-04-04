"""
Polymarket Wallet Tracking Agent

Fetches active traders from the Polymarket leaderboard, computes stats
(win rate, trades/day, profit, profile views) per wallet, filters by
the criteria below, and writes results to docs/results.json.

Criteria:
  - trades_per_day >= 1  (7-day average)
  - profit         > 0
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_API   = "https://data-api.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"

# How many leaderboard pages to pull (50 wallets per page, max offset=1000)
LEADERBOARD_PAGES = 5
LEADERBOARD_LIMIT = 50

# Look-back window for per-wallet trade history
TRADE_HISTORY_LIMIT = 500          # max trades fetched per wallet
DAYS_LOOKBACK       = 7            # window for trades/day calculation

# Respect rate limits – sleep between per-wallet requests
REQUEST_DELAY = 0.3  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(url: str, params: dict | None = None, retries: int = 3) -> list | dict:
    """GET with simple retry logic."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == retries - 1:
                print(f"  [WARN] Failed {url} params={params}: {exc}")
                return []
            time.sleep(1.5 ** attempt)
    return []


# ---------------------------------------------------------------------------
# Step 1 – collect wallet addresses from the leaderboard
# ---------------------------------------------------------------------------

def fetch_leaderboard_wallets() -> list[dict]:
    """Return list of {address, pnl} from ALL leaderboard time periods."""
    seen: dict[str, dict] = {}

    for period in ("ALL", "MONTH", "WEEK"):
        for page in range(LEADERBOARD_PAGES):
            params = {
                "timePeriod": period,
                "orderBy":    "PNL",
                "limit":      LEADERBOARD_LIMIT,
                "offset":     page * LEADERBOARD_LIMIT,
            }
            data = get(f"{DATA_API}/v1/leaderboard", params)
            if not data:
                break
            for entry in data:
                addr = entry.get("proxyWallet", "").lower()
                if addr and addr not in seen:
                    seen[addr] = {
                        "address":  entry.get("proxyWallet", addr),
                        "pnl_raw":  entry.get("pnl", 0),
                    }
            if len(data) < LEADERBOARD_LIMIT:
                break
            time.sleep(REQUEST_DELAY)

    print(f"Collected {len(seen)} unique wallets from leaderboard.")
    return list(seen.values())


# ---------------------------------------------------------------------------
# Step 2 – compute per-wallet stats
# ---------------------------------------------------------------------------

def fetch_trade_stats(address: str) -> dict:
    """
    Returns:
      trades_per_day float  (avg over DAYS_LOOKBACK window)
      trade_count    int    (total trades fetched)
      profit         float  (total cash PnL from closed positions)
      pnl_per_trade  float  (profit / trade_count; None if no trades)
    """
    # --- trades ---
    trades = get(
        f"{DATA_API}/trades",
        {"user": address, "limit": TRADE_HISTORY_LIMIT},
    )
    if not isinstance(trades, list) or len(trades) == 0:
        return {}

    trade_count = len(trades)

    # trades/day over the look-back window
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff  = now_ts - DAYS_LOOKBACK * 86400
    recent  = [t for t in trades if (t.get("timestamp") or 0) >= cutoff]
    trades_per_day = len(recent) / DAYS_LOOKBACK

    # --- profit via positions ---
    positions = get(
        f"{DATA_API}/positions",
        {"user": address, "limit": 500},
    )
    profit = 0.0
    if isinstance(positions, list):
        for p in positions:
            cash_pnl = p.get("cashPnL") or p.get("cashPnl") or 0
            profit  += float(cash_pnl)

    # Fallback to leaderboard PnL if positions return nothing
    # (patched in the main loop from the leaderboard snapshot)

    pnl_per_trade = round(profit / trade_count, 4) if trade_count > 0 else None

    return {
        "trades_per_day": round(trades_per_day, 2),
        "trade_count":    trade_count,
        "profit":         round(profit, 2),
        "pnl_per_trade":  pnl_per_trade,
    }


# ---------------------------------------------------------------------------
# Step 3 – filter & write output
# ---------------------------------------------------------------------------

TRADES_PER_DAY_MIN = 1

# Push an incremental update to GitHub every N wallets processed
PUSH_EVERY = 50


def passes_filter(stats: dict) -> bool:
    tpd    = stats.get("trades_per_day", 0)
    profit = stats.get("profit", 0)
    return (
        tpd    >= TRADES_PER_DAY_MIN
        and profit >  0
    )


def write_results(results: list, out_path: Path) -> None:
    """Write sorted results to disk."""
    sorted_results = sorted(results, key=lambda x: (-(x["pnl_per_trade"] or 0), -x["profit"]))
    with open(out_path, "w") as f:
        json.dump(sorted_results, f, indent=2)


def git_push(out_path: Path, label: str) -> None:
    """Commit and push results.json if it changed."""
    import subprocess
    repo = out_path.parent.parent  # project root

    try:
        subprocess.run(["git", "-C", str(repo), "add", str(out_path)], check=True)
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode == 0:
            print(f"  [git] no changes at {label}, skipping push.")
            return
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m",
             f"chore: incremental update – {label}"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "push"], check=True)
        print(f"  [git] pushed update at {label}")
    except Exception as exc:
        print(f"  [git] push failed at {label}: {exc}")


def run():
    wallets  = fetch_leaderboard_wallets()
    results  = []
    out_path = Path(__file__).parent / "docs" / "results.json"
    out_path.parent.mkdir(exist_ok=True)

    for i, w in enumerate(wallets, 1):
        address = w["address"]
        print(f"[{i}/{len(wallets)}] {address} …", end=" ", flush=True)

        stats = fetch_trade_stats(address)
        if not stats:
            print("no trade data, skipping.")
        else:
            # Use leaderboard PnL as fallback profit when positions return 0
            if stats["profit"] == 0.0 and w.get("pnl_raw"):
                stats["profit"] = round(float(w["pnl_raw"]), 2)

            print(
                f"pnl_per_trade={stats['pnl_per_trade']}  tpd={stats['trades_per_day']}  "
                f"profit={stats['profit']}"
            )

            if passes_filter(stats):
                results.append(
                    {
                        "address":        address,
                        "pnl_per_trade":  stats["pnl_per_trade"],
                        "trades_per_day": stats["trades_per_day"],
                        "profit":         stats["profit"],
                        "polymarket_url": f"https://polymarket.com/profile/{address}",
                    }
                )

        # Incrementally write + push every PUSH_EVERY wallets
        if i % PUSH_EVERY == 0:
            write_results(results, out_path)
            git_push(out_path, f"{i}/{len(wallets)} wallets")

        time.sleep(REQUEST_DELAY)

    # Final write + push
    write_results(results, out_path)
    git_push(out_path, "final")

    print(f"\nDone. {len(results)} wallets matched filters → {out_path}")


if __name__ == "__main__":
    run()
