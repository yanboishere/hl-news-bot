"""Shared data types for the news bot."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventClass(str, Enum):
    MACRO_CPI = "macro_cpi"
    MACRO_NFP = "macro_nfp"
    MACRO_FOMC = "macro_fomc"
    LISTING_BINANCE = "listing_binance"
    LISTING_UPBIT = "listing_upbit"
    LISTING_COINBASE = "listing_coinbase"
    LISTING_HL = "listing_hl"
    HACK = "hack_exploit"
    DEPEG = "depeg"
    ETF = "etf"
    REGULATORY = "regulatory"
    TARIFF = "tariff"
    TRUMP = "trump_policy"
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    MNA = "m_and_a"
    SUPPLY_EXPANSION = "supply_expansion"   # a maker adds capacity -> bearish for its tradeable peers
    SUPPLY_CUT = "supply_cut"               # a maker cuts output / plant outage -> bullish for peers
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
    source: str                 # feed: treeofalpha / telegram / hl_listings / replay
    channel: str                # sub-source: Twitter / Blogs / Binance EN / Upbit / @BWEnews ...
    title: str
    url: str
    ts_ms: int                  # origin timestamp as reported by the feed
    recv_ms: int                # when this process received it
    account: str | None = None  # twitter handle, telegram channel, blog name
    suggestions: list[str] = field(default_factory=list)  # coins suggested by the feed
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        return max(0.0, (self.recv_ms - self.ts_ms) / 1000.0)


@dataclass
class Classification:
    lane: str                   # "rules" | "llm"
    event_class: EventClass
    direction: Direction
    confidence: float           # 0..1
    tickers: list[str]          # HL coin names: BTC, ETH, xyz:TSLA ...
    reason: str
    is_rumor: bool = False
    is_scheduled: bool = False
    magnitude_usd: float | None = None
    latency_ms: int = 0


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
