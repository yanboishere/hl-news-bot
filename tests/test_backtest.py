"""Event backtester: historical labels without a template fall back to today's NewsLiquid templates."""
from __future__ import annotations

import json
import os

from conftest import ROOT
from hlnews import backtest


def fake_klines(symbol, start_ms, limit):
    """Flat market except a 0.5 % drift per minute down: shorts win, longs lose, nothing touches stop/TP for a while."""
    out, px = [], 100.0
    for i in range(limit):
        o = px
        px *= 0.995
        out.append([start_ms + i * 60_000, o, o, px, px])
    return out


def test_template_fallback_and_grouping(cfg, monkeypatch):
    monkeypatch.setattr(backtest, "klines", fake_klines)
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    with open(os.path.join(ROOT, "data", "events.jsonl"), encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip() and not l.startswith("#")]
    labels = {e["event_class"] for e in events}
    assert {"hack_exploit", "etf", "tariff", "macro_cpi"} <= labels            # historical labels from the rules lane
    assert backtest.template_for({"event_class": "hack_exploit", "coin": "ETH"}, cfg["templates"]) is cfg["templates"]["nl_news"]
    assert backtest.template_for({"event_class": "earnings", "coin": "xyz:TSLA"}, cfg["templates"]) is cfg["templates"]["nl_news_stock"]
    assert backtest.template_for({"event_class": "macro_cpi", "coin": "BTC"}, cfg["templates"]) is cfg["templates"]["macro_cpi"]
    res = backtest.run(events, cfg["templates"], delay_s=20, verbose=False)
    assert res["_all"]["n"] == len(events)                                     # every BTC/ETH event simulated
    assert set(res) - {"_all"} == labels                                       # still grouped by the historical label
