"""
Polymarket Wallet Tracking Agent

Fetches active traders from the Polymarket leaderboard, computes stats
(win rate, trades/day, profit, profile views) per wallet, filters by
the criteria below, and writes results to docs/results.json.

Criteria:
  - win_rate  >= 0.85
  - trades_per_day > 10
  - profile_views < 1000
"""

import json
import os
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
DAYS_LOOKBACK       = 30           # window for trades/day calculation

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
      win_rate       float  (resolved trades only; NaN → skip wallet)
      trades_per_day float
      profit         float  (total cash PnL from closed positions)
      views          int    (profile views; 0 when unavailable)
    """
    # --- trades ---
    trades = get(
        f"{DATA_API}/trades",
        {"user": address, "limit": TRADE_HISTORY_LIMIT},
    )
    if not isinstance(trades, list) or len(trades) == 0:
        return {}

    # trades/day over the look-back window
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff  = now_ts - DAYS_LOOKBACK * 86400
    recent  = [t for t in trades if (t.get("timestamp") or 0) >= cutoff]
    trades_per_day = len(recent) / DAYS_LOOKBACK

    # win rate: consider SELL trades as outcomes
    # A sell is a "win" when the cashPnL > 0 (sold for more than cost basis)
    # We approximate: trade is a win if side=="SELL" and price > 0.5 (sold YES tokens above fair)
    # More precisely: use the cashPnL field when present, else price comparison.
    wins   = 0
    losses = 0
    for t in trades:
        cash_pnl = t.get("cashPnL") or t.get("cashPnl")
        side     = (t.get("side") or "").upper()
        if side != "SELL":
            continue  # only resolved-position sells count
        if cash_pnl is not None:
            if float(cash_pnl) > 0:
                wins   += 1
            else:
                losses += 1
        else:
            # fallback: selling YES tokens at price > 0.5 implies profit
            price = float(t.get("price") or 0)
            if price > 0.5:
                wins   += 1
            elif price > 0:
                losses += 1

    total_resolved = wins + losses
    win_rate = wins / total_resolved if total_resolved > 0 else None

    # --- profit via positions ---
    positions = get(
        f"{DATA_API}/positions",
        {"user": address, "limit": 500},
    )
    profit = 0.0
    if isinstance(positions, list):
        for p in positions:
            # cashPnL is realised+unrealised PnL in USD
            cash_pnl = p.get("cashPnL") or p.get("cashPnl") or 0
            profit  += float(cash_pnl)

    # Fallback to leaderboard PnL if positions have no data
    # (will be patched in main loop from the leaderboard snapshot)

    # --- profile views ---
    # The data API exposes a /profile endpoint for some wallets
    views = 0
    profile = get(f"{GAMMA_API}/profiles", {"address": address})
    if isinstance(profile, list) and profile:
        views = int(profile[0].get("views", 0) or 0)
    elif isinstance(profile, dict):
        views = int(profile.get("views", 0) or 0)

    return {
        "trades_per_day": round(trades_per_day, 2),
        "win_rate":        round(win_rate, 4) if win_rate is not None else None,
        "profit":          round(profit, 2),
        "views":           views,
    }


# ---------------------------------------------------------------------------
# Step 3 – filter & write output
# ---------------------------------------------------------------------------

WIN_RATE_MIN       = 0.85
TRADES_PER_DAY_MIN = 10
VIEWS_MAX          = 1000


def passes_filter(stats: dict) -> bool:
    wr  = stats.get("win_rate")
    tpd = stats.get("trades_per_day", 0)
    v   = stats.get("views", 0)
    return (
        wr is not None
        and wr   >= WIN_RATE_MIN
        and tpd  >  TRADES_PER_DAY_MIN
        and v    <  VIEWS_MAX
    )


def run():
    wallets = fetch_leaderboard_wallets()
    results = []

    for i, w in enumerate(wallets, 1):
        address = w["address"]
        print(f"[{i}/{len(wallets)}] {address} …", end=" ", flush=True)

        stats = fetch_trade_stats(address)
        if not stats:
            print("no trade data, skipping.")
            continue

        # Use leaderboard PnL as fallback profit when positions return 0
        if stats["profit"] == 0.0 and w.get("pnl_raw"):
            stats["profit"] = round(float(w["pnl_raw"]), 2)

        print(
            f"wr={stats['win_rate']}  tpd={stats['trades_per_day']}  "
            f"profit={stats['profit']}  views={stats['views']}"
        )

        if passes_filter(stats):
            results.append(
                {
                    "address":        address,
                    "win_rate":       stats["win_rate"],
                    "trades_per_day": stats["trades_per_day"],
                    "profit":         stats["profit"],
                    "views":          stats["views"],
                    "polymarket_url": f"https://polymarket.com/profile/{address}",
                }
            )

        time.sleep(REQUEST_DELAY)

    # Sort by win rate desc, then profit desc
    results.sort(key=lambda x: (-x["win_rate"], -x["profit"]))

    out_path = Path(__file__).parent / "docs" / "results.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. {len(results)} wallets matched filters → {out_path}")


if __name__ == "__main__":
    run()
