"""Shared data types for the news bot."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventClass(str, Enum):
    # first-party data lanes (the bot reads the primary source itself; the judgment is arithmetic, not text)
    MACRO_CPI = "macro_cpi"          # BLS release page vs ForexFactory consensus
    MACRO_NFP = "macro_nfp"
    LISTING_HL = "listing_hl"        # Hyperliquid meta diff: a new perp appeared
    # NewsLiquid lanes: one class per NewsLiquid engineType; direction and score come from NewsLiquid's AI rating
    NL_NEWS = "nl_news"              # engineType "news", crypto tickers
    NL_NEWS_STOCK = "nl_news_stock"  # engineType "news", trade.xyz stock perps (xyz:*) -- separate template
    NL_LISTING = "nl_listing"        # engineType "listing" (Binance, Coinbase, OKX, Bybit, Upbit, Bithumb, ...)
    NL_ONCHAIN = "nl_onchain"
    NL_MARKET = "nl_market"
    NL_MEME = "nl_meme"
    NL_PREDICTION = "nl_prediction"
    NOISE = "noise"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"

    def flip(self) -> "Direction":
        if self is Direction.LONG:
            return Direction.SHORT
        if self is Direction.SHORT:
            return Direction.LONG
        return Direction.NONE


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class NewsItem:
    id: str
    source: str                 # feed: newsliquid / hl_listings / bls_poll / replay
    channel: str                # sub-source: NewsLiquid newsType (Bloomberg, Twitter, jin10, Binance ...), meta_diff, usGov
    title: str
    url: str
    ts_ms: int                  # origin timestamp as reported by the feed
    recv_ms: int                # when this process received it
    account: str | None = None  # publisher / account when the feed reports one
    suggestions: list[str] = field(default_factory=list)  # coins tagged by the feed (NewsLiquid coins[].symbol)
    raw: dict[str, Any] = field(default_factory=dict)     # NewsLiquid items carry their normalized rating in raw["nl"]

    @property
    def age_s(self) -> float:
        return max(0.0, (self.recv_ms - self.ts_ms) / 1000.0)


@dataclass
class Classification:
    lane: str                   # "newsliquid" | "macro" | "hl_listing"
    event_class: EventClass
    direction: Direction
    confidence: float           # 0..1
    tickers: list[str]          # HL coin names: BTC, ETH, xyz:TSLA ...
    reason: str
    is_rumor: bool = False
    is_scheduled: bool = False
    magnitude_usd: float | None = None
    latency_ms: int = 0
    # NewsLiquid rates each tagged coin separately: {"BTC": {"direction": "long", "confidence": 0.85, "source": "coin"}}.
    # When present, the engine opens each coin with its own direction / confidence instead of the headline verdict.
    per_ticker: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Signal:
    id: str
    news_id: str
    coin: str
    direction: Direction
    event_class: EventClass
    confidence: float
    created_ms: int
    template: dict[str, Any]    # hold_s, stop_pct, tp_pct, risk_pct, max_lev ...
    reason: str
    news_age_s: float


@dataclass
class Position:
    id: str
    signal_id: str
    coin: str
    direction: Direction
    event_class: EventClass
    size: float                 # coin units
    entry_px: float
    entry_ms: int
    stop_px: float
    tp_px: float | None
    time_exit_ms: int
    notional_usd: float
    leverage: float
    mode: str = "paper"
    fee_usd: float = 0.0
    slippage_bps: float = 0.0
    exit_px: float | None = None
    exit_ms: int | None = None
    exit_reason: str | None = None
    pnl_usd: float | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["event_class"] = self.event_class.value
        return d

    def unrealized(self, mid: float) -> float:
        sign = 1.0 if self.direction is Direction.LONG else -1.0
        return sign * (mid - self.entry_px) * self.size
