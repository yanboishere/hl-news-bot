"""Engine with NewsLiquid items: thresholds, per-coin legs, stock template, two-source policy, replay fixture."""
from __future__ import annotations

import asyncio
import os

from conftest import ROOT, nl_news_item, ws_item
from hlnews import feeds
from hlnews.feeds import replay_feed
from hlnews.models import Direction, EventClass


def run(coro):
    return asyncio.run(coro)


def test_high_score_crypto_item_opens_paper_position(engine):
    sigs = run(engine.handle(nl_news_item(ws_item("e1", "Bloomberg: big BTC news", ["BTC"], score=86))))
    assert len(sigs) == 1 and sigs[0].event_class is EventClass.NL_NEWS and sigs[0].direction is Direction.LONG
    assert sigs[0].confidence == 0.86
    assert [p.coin for p in engine.state.open] == ["BTC"]
    assert not feeds.is_hot()                              # no hot window (REST quota) for an item that needs no second source


def test_below_template_threshold_makes_no_signal(engine):
    assert run(engine.handle(nl_news_item(ws_item("e2", "Reuters: something", ["BTC"], score=78)))) == []
    row = engine.store.conn.execute("select lane, direction, confidence from classifications where news_id='nl_e2'").fetchone()
    assert row == ("newsliquid", "long", 0.78)            # the verdict is still on the tape


def test_stock_perp_uses_stock_template(engine):
    sigs = run(engine.handle(nl_news_item(ws_item("e3", "NVIDIA beats", ["NVDA", "XYZ-NVDA"], score=90))))
    assert [(s.coin, s.event_class) for s in sigs] == [("xyz:NVDA", EventClass.NL_NEWS_STOCK)]
    assert sigs[0].template["hold_s"] == engine.templates["nl_news_stock"]["hold_s"]


def test_per_coin_directions_open_two_legs(engine):
    item = nl_news_item(ws_item("e4", "rotation", ["BTC", "ETH"], score=88,
                                per_coin={"BTC": {"score": 88, "signal": "long"}, "ETH": {"score": 84, "signal": "short"}}))
    sigs = run(engine.handle(item))
    assert {(s.coin, s.direction) for s in sigs} == {("BTC", Direction.LONG), ("ETH", Direction.SHORT)}


def test_lone_tweet_waits_for_a_second_source(engine):
    tweet = nl_news_item(ws_item("t1", "KOL: ETH to the moon", ["ETH"], score=92, news_type="Twitter"))
    assert run(engine.handle(tweet)) == []
    assert feeds.is_hot()                                  # REST polls fast while the second source is awaited
    second = nl_news_item(ws_item("t2", "Reuters: Ether rallies on ETF flows", ["ETH"], score=88, news_type="Reuters"))
    sigs = run(engine.handle(second))
    assert [(s.coin, s.direction) for s in sigs] == [("ETH", Direction.LONG)]
    # a second tweet is not a second source of a different kind; two tweets from the same newsType never corroborate
    engine.state.open.clear()
    engine.state.last_trade_ms_by_coin.clear()
    t3 = nl_news_item(ws_item("t3", "KOL: BTC breakout", ["BTC"], score=95, news_type="twitter"))
    t4 = nl_news_item(ws_item("t4", "KOL2: BTC breakout soon", ["BTC"], score=95, news_type="Twitter"))
    assert run(engine.handle(t3)) == [] and run(engine.handle(t4)) == []


def test_meme_and_listing_engines_do_not_trade(engine):
    assert run(engine.handle(nl_news_item(ws_item("m1", "meme pump", ["BTC"], score=99, engine="meme")))) == []
    sigs = run(engine.handle(nl_news_item(ws_item("l1", "Binance will list", ["ETH"], score=95, engine="listing", news_type="Binance"))))
    assert len(sigs) == 1 and sigs[0].event_class is EventClass.NL_LISTING
    assert engine.state.open == []                         # nl_listing is alert-only (trade: false)
    dec = engine.store.conn.execute("select reason from decisions where signal_id=?", (sigs[0].id,)).fetchone()
    assert dec[0].startswith("alert-only")


def test_replay_fixture_end_to_end(engine):
    async def go():
        out = []
        async for item in replay_feed(os.path.join(ROOT, "data", "replay.jsonl")):
            out.append((item, await engine.handle(item)))
        return out

    results = run(go())
    assert len(results) == 15
    traded = {(s.coin, s.event_class.value, s.direction.value) for _, sigs in results for s in sigs}
    assert ("BTC", "nl_news", "long") in traded                       # the example with a per-coin rating (84)
    assert not any(ec == "nl_news" and coin == "ETH" for coin, ec, _ in traded)   # ETH short 55 < min_score
    assert ("ETH", "macro_cpi", "short") in traded                     # synthetic hot CPI
    real = [sigs for item, sigs in results if not item.id.startswith(("nl_example", "bls_", "plain"))]
    assert all(s == [] for s in real)                                  # none of the 12 real hot-list items trades
    assert sorted((p.coin, p.direction.value) for p in engine.state.open) == [("BTC", "long"), ("ETH", "short")]
    n_classified = engine.store.conn.execute("select count(distinct news_id) from classifications").fetchone()[0]
    assert n_classified == 14                            # 15 lines, the repeated Datavault story is deduplicated
    iran = engine.store.conn.execute("select reason from classifications where news_id='nl_4614262'").fetchone()[0]
    assert "item rating not applied" in iran
