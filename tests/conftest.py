"""Shared fixtures. Tests never touch the network: Hyperliquid is a fake, NewsLiquid is a fake fetch or a local socket."""
from __future__ import annotations

import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hlnews.models import NewsItem, now_ms  # noqa: E402
from hlnews.newsliquid import normalize, to_news_item  # noqa: E402


class FakeHL:
    """The slice of HLClient the engine uses, with fixed mids and paper fills."""

    def __init__(self):
        self.max_lev = {"BTC": 40, "ETH": 25, "xyz:NVDA": 10, "xyz:MU": 10, "xyz:TSLA": 10}
        self.mids = {"BTC": 100_000.0, "ETH": 4_000.0, "xyz:NVDA": 200.0, "xyz:MU": 150.0, "xyz:TSLA": 400.0}
        self.mids_ms = now_ms()
        self.opened = []

    def mid(self, coin):
        return self.mids.get(coin)

    async def refresh_mids(self):
        self.mids_ms = now_ms()

    async def l2_slippage_bps(self, coin, notional, is_buy):
        return 1.0

    def taker_fee(self, coin):
        return 0.00045

    async def open_position(self, pos):
        self.opened.append(pos)
        return pos

    async def close_position(self, pos, reason):
        pos.exit_px, pos.exit_ms, pos.exit_reason, pos.pnl_usd = self.mid(pos.coin), now_ms(), reason, 0.0
        return pos


@pytest.fixture(autouse=True)
def _reset_hot_window():
    """feeds keeps the hot window in a module global; one test's window must not change another test's poll interval."""
    import hlnews.feeds as F

    F._HOT_UNTIL_MS = 0
    yield
    F._HOT_UNTIL_MS = 0


@pytest.fixture
def cfg(tmp_path):
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        c = yaml.safe_load(f)
    c["storage"]["sqlite_path"] = str(tmp_path / "test.db")
    c["storage"]["log_path"] = str(tmp_path / "test.log")
    c["alerts"]["telegram"] = False
    c["alerts"]["macos_notification"] = False
    return c


@pytest.fixture
def engine(cfg):
    from hlnews.engine import Engine
    from hlnews.store import Store

    eng = Engine(cfg, Store(cfg["storage"]["sqlite_path"]), FakeHL(), [])
    eng.in_maintenance_window = lambda t=None: False
    return eng


def ws_item(iid, text, coins, score=85, signal="long", news_type="Bloomberg", engine="news", ts=None, per_coin=None):
    """A news.ai_update params payload as documented in the opennews-mcp README."""
    cs = []
    for c in coins:
        e = {"symbol": c, "market_type": "cex"}
        if per_coin and c in per_coin:
            e.update(per_coin[c])
        cs.append(e)
    return {"id": iid, "text": text, "newsType": news_type, "engineType": engine, "link": "https://example.com/" + str(iid),
            "coins": cs, "aiRating": {"score": score, "grade": "A", "signal": signal}, "ts": ts or now_ms()}


def nl_news_item(payload, source="newsliquid") -> NewsItem:
    nl = normalize(payload)
    assert nl is not None
    return to_news_item(nl, now_ms(), source=source)
