"""Rating -> Classification: symbol mapping, per-coin ratings, thresholds, macro arithmetic, dispatch."""
from __future__ import annotations

from conftest import nl_news_item, ws_item
from hlnews.classify import ClassifyContext, classify, event_class_for, map_symbol
from hlnews.models import Direction, EventClass, NewsItem, now_ms

UNIVERSE = {"BTC", "ETH", "kPEPE", "xyz:NVDA", "xyz:MU", "xyz:SKHX", "xyz:TSLA"}


def ctx(**kw) -> ClassifyContext:
    base = dict(symbol_map={"PEPE": "kPEPE"}, universe=UNIVERSE, min_score=60)
    base.update(kw)
    return ClassifyContext(**base)


def test_map_symbol_exact_only():
    c = ctx()
    assert map_symbol("BTC", c) == "BTC"
    assert map_symbol("btc", c) == "BTC"
    assert map_symbol("XYZ-NVDA", c) == "xyz:NVDA"
    assert map_symbol("NVDA", c) is None            # plain stock ticker: NewsLiquid adds XYZ-NVDA when it means the perp
    assert map_symbol("XYZ-SKHY", c) is None        # xyz:SKHY is a different perp and not in the universe
    assert map_symbol("PEPE", c) == "kPEPE"         # explicit symbol_map
    assert map_symbol("SOL", c) is None
    assert map_symbol("", c) is None


def test_item_rating_applies_to_a_single_asset_item():
    item = nl_news_item(ws_item("1", "BTC only", ["BTC"], score=85))
    cl = classify(item, ctx())
    assert cl.lane == "newsliquid" and cl.event_class is EventClass.NL_NEWS
    assert cl.direction is Direction.LONG and cl.confidence == 0.85 and cl.tickers == ["BTC"]
    assert cl.per_ticker["BTC"] == {"direction": "long", "confidence": 0.85, "source": "item"}
    stock = classify(nl_news_item(ws_item("1b", "NVDA", ["NVDA", "XYZ-NVDA"], score=85)), ctx())   # one asset, two tags
    assert stock.direction is Direction.LONG and stock.tickers == ["xyz:NVDA"]


def test_item_rating_is_withheld_on_multi_asset_items():
    """The real 2026-10-28 case: an Iran/oil item tagged CL, XYZ-CL, BTC, ETH, EPIC, SOL with one item-level 'long'."""
    item = nl_news_item(ws_item("1c", "Iran escalation", ["CL", "XYZ-CL", "BTC", "ETH", "EPIC", "SOL"], score=82))
    cl = classify(item, ctx())
    assert cl.direction is Direction.NONE and cl.tickers == ["BTC", "ETH"]
    assert cl.per_ticker["BTC"]["source"] == "item-withheld"
    assert "item rating not applied: 5 assets tagged" in cl.reason
    everyone = classify(item, ctx(item_rating_fallback="all"))
    assert everyone.direction is Direction.LONG and everyone.tickers == ["BTC", "ETH"]
    assert classify(nl_news_item(ws_item("1d", "x", ["BTC"], score=85)), ctx(item_rating_fallback="none")).direction is Direction.NONE


def test_per_coin_rating_overrides_item_rating():
    item = nl_news_item(ws_item("2", "mixed", ["BTC", "ETH"], score=85,
                                per_coin={"BTC": {"score": 85, "signal": "long"}, "ETH": {"score": 82, "signal": "short"}}))
    cl = classify(item, ctx())
    assert cl.per_ticker["ETH"] == {"direction": "short", "confidence": 0.82, "source": "coin"}
    assert cl.direction is Direction.LONG and cl.tickers == ["BTC", "ETH"]
    off = classify(item, ctx(use_coin_rating=False))                   # two assets: the item rating is withheld
    assert off.per_ticker["ETH"]["direction"] == "none" and off.direction is Direction.NONE
    off_all = classify(item, ctx(use_coin_rating=False, item_rating_fallback="all"))
    assert off_all.per_ticker["ETH"]["direction"] == "long"


def test_min_score_and_neutral_are_not_directional():
    low = classify(nl_news_item(ws_item("3", "meh", ["BTC"], score=55)), ctx())
    assert low.direction is Direction.NONE and "below min_score" in low.reason
    neutral = classify(nl_news_item(ws_item("4", "flat", ["BTC"], score=90, signal="neutral")), ctx())
    assert neutral.direction is Direction.NONE and "(neutral)" in neutral.reason
    nothing = classify(nl_news_item(ws_item("5", "alt", ["SOL"], score=95)), ctx())
    assert nothing.direction is Direction.NONE and nothing.tickers == [] and "SOL" in nothing.reason


def test_engine_types_map_to_classes_and_stock_template():
    assert classify(nl_news_item(ws_item("6", "l", ["BTC"], engine="listing")), ctx()).event_class is EventClass.NL_LISTING
    assert classify(nl_news_item(ws_item("7", "m", ["BTC"], engine="meme")), ctx()).event_class is EventClass.NL_MEME
    assert event_class_for(EventClass.NL_NEWS, "xyz:NVDA") is EventClass.NL_NEWS_STOCK
    assert event_class_for(EventClass.NL_NEWS, "BTC") is EventClass.NL_NEWS
    assert event_class_for(EventClass.NL_LISTING, "xyz:NVDA") is EventClass.NL_LISTING


def _bls(kind, parsed, consensus):
    return NewsItem(id="b", source="bls_poll", channel="usGov", title="BLS", url="", ts_ms=now_ms(), recv_ms=now_ms(),
                    raw={"kind": kind, "parsed": parsed, "consensus": consensus})


def test_macro_lane_is_arithmetic():
    hot = classify(_bls("cpi", {"yoy": 3.1, "core_yoy": 3.3}, {"yoy": 2.9, "core_yoy": 3.1}), ctx())
    assert (hot.lane, hot.event_class, hot.direction, hot.confidence) == ("macro", EventClass.MACRO_CPI, Direction.SHORT, 0.85)
    assert hot.tickers == ["BTC", "ETH"]
    cool = classify(_bls("cpi", {"yoy": 2.7}, {"yoy": 2.9}), ctx())
    assert cool.direction is Direction.LONG
    inline = classify(_bls("cpi", {"yoy": 2.9, "core_yoy": 3.1}, {"yoy": 2.9, "core_yoy": 3.1}), ctx())
    assert inline.direction is Direction.NONE
    missing = classify(_bls("cpi", {"yoy": 3.1}, {}), ctx())
    assert missing.direction is Direction.NONE and missing.confidence == 0.3
    armed = classify(_bls("cpi", {"yoy": 3.1}, {}), ctx(cpi_consensus=2.9))      # consensus armed on the context
    assert armed.direction is Direction.SHORT
    nfp = classify(_bls("nfp", {"k": 250}, {"k": 150}), ctx())
    assert (nfp.event_class, nfp.direction) == (EventClass.MACRO_NFP, Direction.SHORT)
    assert classify(_bls("nfp", {"k": 170}, {"k": 150}), ctx()).direction is Direction.NONE


def test_unrated_items_are_noise():
    plain = NewsItem(id="p", source="replay", channel="replay", title="headline", url="", ts_ms=now_ms(), recv_ms=now_ms())
    assert classify(plain, ctx()).event_class is EventClass.NOISE
    hl = NewsItem(id="h", source="hl_listings", channel="meta_diff", title="new", url="", ts_ms=now_ms(), recv_ms=now_ms(),
                  raw={"name": "xyz:NEW"})
    assert classify(hl, ctx()).event_class is EventClass.LISTING_HL
