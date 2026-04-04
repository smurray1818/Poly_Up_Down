"""
Entry point — wires feed → signal → risk → sizer → executor with asyncio.
"""
import asyncio
import json
import logging
import math
import os
import signal
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from dotenv import load_dotenv

from .executor import Executor
from .feed import BinancePrice, FeedManager, PolymarketBook
from .github_tracker import GitHubTracker
from .latency import tracker as latency_tracker
from .paper_trader import PaperTrader
from .risk import RiskConfig, RiskManager
from .signal import MomentumSignalEngine, Side
from .sizer import KellySizer

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
WINDOW_SECONDS = 15 * 60  # 15-minute windows


# ---------------------------------------------------------------------------
# Contracts — BTC and ETH 15-minute up/down markets
#
# Token IDs rotate every 15 minutes.  resolve_15m_token_id() computes the
# current window's end timestamp and fetches the live token ID from the
# Gamma API at startup and again at the top of every new window.
# ---------------------------------------------------------------------------
@dataclass
class Contract:
    name: str           # e.g. "BTC"
    binance_symbol: str # e.g. "BTCUSDT"
    asset_slug: str     # e.g. "btc"  (used to build Polymarket event slug)
    price_to_prob: object  # callable(float) -> float


CONTRACTS: list[Contract] = [
    Contract(
        name="BTC",
        binance_symbol="BTCUSDT",
        asset_slug="btc",
        price_to_prob=lambda price: 1 / (1 + math.exp(
            -(price - float(os.getenv("BTC_TARGET_PRICE", "85000")))
            / float(os.getenv("BTC_SCALE", "5000"))
        )),
    ),
    Contract(
        name="ETH",
        binance_symbol="ETHUSDT",
        asset_slug="eth",
        price_to_prob=lambda price: 1 / (1 + math.exp(
            -(price - float(os.getenv("ETH_TARGET_PRICE", "2000")))
            / float(os.getenv("ETH_SCALE", "200"))
        )),
    ),
]


def current_window_end_ts() -> int:
    """Return the Unix timestamp of the end of the current 15-minute window."""
    now = int(time.time())
    return now + (WINDOW_SECONDS - now % WINDOW_SECONDS)


async def resolve_15m_token_id(asset_slug: str, side: str = "UP") -> Optional[str]:
    """
    Fetch the YES (UP) or NO (DOWN) token ID for the currently active
    15-minute market for `asset_slug` (e.g. "btc", "eth").

    Polymarket event slug format: {asset}-updown-15m-{window_end_unix}
    """
    window_ts = current_window_end_ts()
    slug = f"{asset_slug}-updown-15m-{window_ts}"
    url = f"{GAMMA_API}/events?slug={slug}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            events = resp.json()

        if not events:
            logger.warning("No event found for slug %s", slug)
            return None

        markets = events[0].get("markets", [])
        if not markets:
            logger.warning("No markets in event %s", slug)
            return None

        market = markets[0]
        # Gamma API returns both clobTokenIds and outcomes as JSON-encoded strings
        raw_ids = market.get("clobTokenIds") or market.get("clob_token_ids") or "[]"
        token_ids: list[str] = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        raw_outcomes = market.get("outcomes") or "[]"
        outcomes: list[str] = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

        target = side.lower()  # "up" or "down"
        for token_id, outcome in zip(token_ids, outcomes):
            if outcome.lower() == target:
                logger.info(
                    "Resolved %s 15m %s token: ...%s (window ends %s)",
                    asset_slug.upper(), side, token_id[-8:], window_ts,
                )
                return token_id

        # Fallback: index 0 = Up on Polymarket 15m markets
        if token_ids:
            logger.info(
                "Resolved %s 15m %s token (fallback idx=0): ...%s",
                asset_slug.upper(), side, token_ids[0][-8:],
            )
            return token_ids[0]

    except Exception as e:
        logger.error("Failed to resolve token for %s: %s", slug, e)

    return None


class ContractBot:
    """
    Manages a single FeedManager + SignalEngine pair for one Contract.
    Token ID is refreshed at the start of every new 15-minute window.
    """

    def __init__(
        self,
        contract: Contract,
        risk: RiskManager,
        sizer: KellySizer,
        executor: "Executor",
        paper_trader: Optional[PaperTrader] = None,
    ):
        self.contract = contract
        self.risk = risk
        self.sizer = sizer
        self.executor = executor
        self.paper_trader = paper_trader

        self.active_token_id: Optional[str] = None
        self._prev_token_id: Optional[str] = None  # used to detect window rollover
        self._last_mid: float = 0.0
        self._window_end: int = 0
        self._refreshing: bool = False  # guard against concurrent refreshes

        self.feed = FeedManager(
            binance_symbol=contract.binance_symbol,
            poly_token_id="",  # set dynamically; feed polls whatever token_id is set
            poly_poll_interval=float(os.getenv("POLY_POLL_INTERVAL", "0.5")),
        )
        self.signal_engine = MomentumSignalEngine(
            price_to_prob=contract.price_to_prob,
            min_edge=float(os.getenv("MIN_EDGE", "0.02")),
            min_ticks=int(os.getenv("MIN_TICKS", "2")),
        )
        self.feed.on_binance(self.signal_engine.on_binance)
        self.feed.on_poly(self._on_poly_update)

    async def refresh_token_if_needed(self):
        now = int(time.time())
        if now < self._window_end - 5:
            return
        if self._refreshing:
            return
        self._refreshing = True
        try:
            token_id = await resolve_15m_token_id(self.contract.asset_slug, side="UP")
            if token_id:
                # Detect rollover: close the previous paper position at last known mid
                if (
                    self.paper_trader
                    and self._prev_token_id
                    and token_id != self._prev_token_id
                ):
                    self.paper_trader.close_position(self._prev_token_id)

                self._prev_token_id = self.active_token_id
                self.active_token_id = token_id
                self.feed.poly_token_id = token_id
                self._window_end = current_window_end_ts()
                logger.info(
                    "%s: new window token ...%s, expires in %ds",
                    self.contract.name, token_id[-8:], self._window_end - now,
                )
            else:
                logger.warning("%s: could not resolve token ID for new window", self.contract.name)
        finally:
            self._refreshing = False

    async def _on_poly_update(self, book: PolymarketBook):
        await self.refresh_token_if_needed()
        if not self.active_token_id:
            return

        # Keep paper trader's last_mid current on every tick
        if self.paper_trader and book.mid is not None:
            self._last_mid = book.mid
            self.paper_trader.update_mid(self.active_token_id, book.mid)

        with latency_tracker.measure("pipeline.total"):
            sig = self.signal_engine.on_poly(book)
            if sig is None:
                return

            size_result = self.sizer.size(
                edge=sig.edge,
                poly_mid=sig.poly_mid,
                bankroll=self.risk.bankroll,
            )
            if size_result.contracts <= 0:
                return

            notional = size_result.bankroll_used
            ok, reason = self.risk.check(
                token_id=self.active_token_id,
                side=sig.side.value,
                notional=notional,
                edge=sig.edge,
                poly_bid=book.best_bid,
                poly_ask=book.best_ask,
            )
            if not ok:
                logger.info("%s risk rejected: %s — %s", self.contract.name, reason.code, reason.detail)
                return

            price = book.best_ask if sig.side == Side.BUY else book.best_bid
            if price is None:
                return

            result = await self.executor.submit(
                token_id=self.active_token_id,
                side=sig.side,
                size=size_result.contracts,
                price=price,
            )
            if result.success:
                self.risk.record_fill(self.active_token_id, notional)
                if self.paper_trader:
                    self.paper_trader.record_fill(
                        asset=self.contract.name,
                        token_id=self.active_token_id,
                        side=sig.side.value,
                        size=size_result.contracts,
                        entry_price=price,
                    )
            else:
                logger.warning("%s order failed: %s", self.contract.name, result.error)


class Bot:
    def __init__(self):
        bankroll = float(os.getenv("BANKROLL_USD", "1000"))
        dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

        self.risk = RiskManager(
            bankroll=bankroll,
            config=RiskConfig(
                max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.10")),
                max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05")),
                max_notional_per_trade=float(os.getenv("MAX_NOTIONAL", "500")),
                max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "0.30")),
            ),
        )
        self.sizer = KellySizer(
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.05")),
        )
        self.executor = Executor(dry_run=dry_run)
        self.github = GitHubTracker(
            risk_manager=self.risk,
            executor=self.executor,
        )

        paper_trading = os.getenv("PAPER_TRADING", "false").lower() == "true"
        self.paper_trader: Optional[PaperTrader] = None
        if paper_trading:
            csv_path = os.path.join(os.path.dirname(__file__), "..", "logs", "paper_trades.csv")
            self.paper_trader = PaperTrader(
                starting_bankroll=bankroll,
                csv_path=os.path.normpath(csv_path),
            )
            logger.info("Paper trading ENABLED — logging to logs/paper_trades.csv")

        self.contract_bots = [
            ContractBot(c, self.risk, self.sizer, self.executor, self.paper_trader)
            for c in CONTRACTS
        ]

    async def run(self):
        logger.info("Bot starting (dry_run=%s) with %d contracts", self.executor.dry_run, len(CONTRACTS))

        # Resolve initial token IDs for all contracts
        await asyncio.gather(*[cb.refresh_token_if_needed() for cb in self.contract_bots])

        await self.github.post_now()

        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _shutdown(signum, frame):
            logger.info("Shutdown signal received")
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        feed_tasks = [asyncio.create_task(cb.feed.start()) for cb in self.contract_bots]
        github_task = asyncio.create_task(self.github.start())

        try:
            await stop_event.wait()
        finally:
            logger.info("Shutting down…")
            for cb in self.contract_bots:
                await cb.feed.stop()
            if self.paper_trader:
                self.paper_trader.close_all()
            await self.github.stop()
            for t in feed_tasks:
                t.cancel()
            github_task.cancel()
            await self.github.post_now()
            logger.info("Final latency stats:\n%s", latency_tracker.summary_table())


def main():
    bot = Bot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
