"""NewsLiquid transport + normalization: shapes, timestamps, REST polling, WebSocket push, refusal fallback."""
from __future__ import annotations

import asyncio
import http
import json
import os

import pytest

from conftest import ROOT, ws_item
from hlnews.models import now_ms
from hlnews.newsliquid import NLAuthError, NLQuotaError, display_text, newsliquid_feed, normalize, rest_rows, ts_ms


# --------------------------------------------------------------------------- normalization
def test_normalize_ws_ai_update_keeps_per_coin_rating():
    nl = normalize(ws_item("a1", "BTC up", ["BTC", "ETH"], score=85, per_coin={"BTC": {"score": 85, "signal": "long", "grade": "A"},
                                                                                 "ETH": {"score": 45, "signal": "short", "grade": "B"}}))
    assert nl["id"] == "a1" and nl["news_type"] == "Bloomberg" and nl["engine_type"] == "news"
    assert nl["rating"]["score"] == 85 and nl["rating"]["signal"] == "long"
    assert nl["coins"][1] == {"symbol": "ETH", "market_type": "cex", "score": 45.0, "signal": "short", "grade": "B"}


def test_normalize_rest_row_not_done_has_no_rating():
    row = {"id": 7, "text": "x", "newsType": "Reuters", "engineType": "news", "coins": [{"symbol": "BTC", "match": "title"}],
           "aiRating": {"score": None, "status": "pending"}, "ts": "2026-10-28T10:00:00+08:00"}
    nl = normalize(row)
    assert nl["rating"] is None
    assert nl["ts_ms"] == ts_ms("2026-10-28T02:00:00Z")
    row["aiRating"] = {"score": 70, "signal": "bearish", "status": "done", "summary": "中", "enSummary": "en"}
    assert normalize(row)["rating"]["signal"] == "short"


def test_normalize_free_hot_row_from_replay_fixture():
    with open(os.path.join(ROOT, "data", "replay.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip() and not l.startswith("#")]
    hot = [r for r in rows if "score" in r]
    assert len(hot) == 12                                    # the 12 real free_hot rows (flat score/signal)
    nl = normalize(hot[0])
    assert nl["rating"]["status"] == "done" and isinstance(nl["coins"][0]["symbol"], str)
    assert all(normalize(r)["ts_ms"] > 1_700_000_000_000 for r in hot)


@pytest.mark.parametrize("v,expected", [
    (1708473600000, 1708473600000), (1708473600, 1708473600000), ("1708473600000", 1708473600000),
    ("2024-02-21T00:00:00Z", 1708473600000), ("2024-02-21T08:00:00+08:00", 1708473600000), (None, None), ("garbage", None),
])
def test_ts_formats(v, expected):
    assert ts_ms(v) == expected


def test_display_text_flattens_html():
    assert display_text("<b>个股新闻</b><br/><span>1. Anthropic</span> VCs&#8217; IPO") == "个股新闻 1. Anthropic VCs’ IPO"


def test_rest_rows_shapes_and_errors():
    assert rest_rows({"data": [{"id": 1}], "total": 1}) == [{"id": 1}]
    assert rest_rows({"success": True, "data": {"list": [{"id": 2}]}}) == [{"id": 2}]
    with pytest.raises(NLQuotaError):
        rest_rows({"success": False, "error": "quota exceeded"})
    with pytest.raises(NLAuthError):
        rest_rows({"success": False, "error": "invalid token"})
    with pytest.raises(RuntimeError):
        rest_rows({"success": False, "error": "limit must be <= 100"})   # not a quota problem


# --------------------------------------------------------------------------- feed helpers
async def collect(gen, n, timeout=5.0):
    out = []

    async def run():
        async for item in gen:
            out.append(item)
            if len(out) >= n:
                return

    try:
        await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        await gen.aclose()
    return out


# --------------------------------------------------------------------------- REST transport
HOUR_MS = 3_600_000


def test_rest_feed_skips_history_and_waits_for_rating():
    calls = []

    def fetch(body):
        calls.append(dict(body))
        old = ws_item("old", "history", ["BTC"], ts=now_ms() - HOUR_MS)
        if len(calls) == 1:
            return [old]
        if len(calls) == 2:
            pend = ws_item("p1", "pending", ["ETH"])
            pend["aiRating"] = {"status": "pending"}
            return [old, pend, ws_item("n1", "new rated", ["BTC"])]
        return [old, ws_item("p1", "pending now rated", ["ETH"], score=88), ws_item("n1", "new rated", ["BTC"])]

    cfg = {"transport": "rest", "poll_s": 0.05, "fast_poll_s": 0.05, "engine_types": {"news": []}, "has_coin": True}
    items = asyncio.run(collect(newsliquid_feed(cfg, token="t", fetch=fetch), 2))
    assert [i.id for i in items] == ["nl_n1", "nl_p1"]
    assert items[1].raw["nl_first_seen_ms"] <= items[1].raw["nl_rated_ms"]
    assert calls[0] == {"limit": 50, "page": 1, "engineTypes": {"news": []}, "hasCoin": True}


def test_restart_does_not_retrade_items_published_before_start():
    def fetch(body):
        return [ws_item("r", "30 s before start", ["BTC"], ts=now_ms() - 30_000)]

    items = asyncio.run(collect(newsliquid_feed({"transport": "rest", "poll_s": 0.05}, token="t", fetch=fetch), 1, timeout=0.6))
    assert items == []


def test_rest_burst_reads_following_pages():
    t0 = now_ms() + 1_000
    pages = {1: ["a", "b"], 2: ["c", "d"], 3: ["e"]}
    calls = []

    def fetch(body):
        calls.append(body["page"])
        if len(calls) > 3:
            return []
        return [ws_item(i, i, ["BTC"], ts=t0 + n) for n, i in enumerate(pages[body["page"]])]

    cfg = {"transport": "rest", "poll_s": 0.05, "page_size": 2, "max_pages": 3}
    items = asyncio.run(collect(newsliquid_feed(cfg, token="t", fetch=fetch), 5, timeout=2))
    assert sorted(i.id for i in items) == ["nl_a", "nl_b", "nl_c", "nl_d", "nl_e"]
    assert calls[:3] == [1, 2, 3]


def test_feed_without_token_reports_and_stops():
    seen = []
    cfg = {"transport": "auto", "token_env": "HLNEWS_TEST_NO_SUCH_ENV"}
    items = asyncio.run(collect(newsliquid_feed(cfg, on_health=lambda s, d: seen.append(s)), 1, timeout=1.0))
    assert items == [] and seen == ["auth_fail"]


def test_rest_quota_error_pauses_and_reports():
    seen = []

    def fetch(body):
        raise NLQuotaError("HTTP 429")

    cfg = {"transport": "rest", "poll_s": 0.05}
    asyncio.run(collect(newsliquid_feed(cfg, token="t", fetch=fetch, on_health=lambda s, d: seen.append(s)), 1, timeout=0.5))
    assert seen == ["quota"]


# --------------------------------------------------------------------------- WebSocket transport (local server)
ws_server = pytest.importorskip("websockets.asyncio.server")


def test_ws_push_yields_ai_update_with_first_seen():
    received = []

    async def handler(sock):
        sub = json.loads(await sock.recv())
        received.append(sub)
        await sock.send(json.dumps({"jsonrpc": "2.0", "id": sub["id"], "result": {"success": True, "filters": sub["params"]}}))
        upd = ws_item("w1", "raw first", ["BTC"])
        upd.pop("aiRating")
        await sock.send(json.dumps({"jsonrpc": "2.0", "method": "news.update", "params": upd}))
        await sock.send(json.dumps({"jsonrpc": "2.0", "method": "strategy.triggered", "params": {"id": 9, "text": "x"}}))
        await asyncio.sleep(0.05)
        await sock.send(json.dumps({"jsonrpc": "2.0", "method": "news.ai_update", "params": ws_item("w1", "rated", ["BTC"], score=91)}))
        await sock.send(json.dumps({"jsonrpc": "2.0", "method": "news.ai_update", "params": ws_item("w1", "rated again", ["BTC"])}))
        await asyncio.sleep(2)

    async def main():
        async with ws_server.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = {"transport": "ws", "ws_url": f"ws://127.0.0.1:{port}/open/news_wss", "engine_types": {"news": [], "listing": []},
                   "has_coin": True, "backfill_on_reconnect": False}
            return await collect(newsliquid_feed(cfg, token="tok en", fetch=lambda b: []), 2, timeout=1.5)

    items = asyncio.run(main())
    assert [i.id for i in items] == ["nl_w1"]                       # duplicate push ignored, strategy event ignored
    assert items[0].raw["nl"]["rating"]["score"] == 91
    assert items[0].raw["nl_first_seen_ms"] <= items[0].raw["nl_rated_ms"]
    assert received[0]["method"] == "news.subscribe"
    assert received[0]["params"] == {"engineTypes": {"news": [], "listing": []}, "hasCoin": True}


def test_ws_refused_handshake_falls_back_to_rest():
    seen = []

    def refuse(connection, request):
        assert "token=t" in request.path
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")

    async def handler(sock):  # never reached
        await sock.close()

    calls = []

    def fetch(body):
        calls.append(1)
        old = ws_item("r0", "history", ["BTC"], ts=now_ms() - HOUR_MS)
        return [old] if len(calls) == 1 else [old, ws_item("r1", "via rest", ["ETH"])]

    async def main():
        async with ws_server.serve(handler, "127.0.0.1", 0, process_request=refuse) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = {"transport": "auto", "ws_url": f"ws://127.0.0.1:{port}/open/news_wss", "poll_s": 0.05, "ws_retry_s": 600}
            return await collect(newsliquid_feed(cfg, token="t", fetch=fetch, on_health=lambda s, d: seen.append(s)), 1, timeout=3)

    items = asyncio.run(main())
    assert [i.id for i in items] == ["nl_r1"]
    assert "ws_refused" in seen


def test_ws_reconnect_backfills_the_gap_from_rest():
    conns = []

    async def handler(sock):
        sub = json.loads(await sock.recv())
        conns.append(1)
        await sock.send(json.dumps({"jsonrpc": "2.0", "id": sub["id"], "result": {"success": True}}))
        if len(conns) == 1:
            await sock.send(json.dumps({"jsonrpc": "2.0", "method": "news.ai_update", "params": ws_item("w1", "first", ["BTC"])}))
            await asyncio.sleep(0.1)
            return                                          # server drops the connection
        await sock.send(json.dumps({"jsonrpc": "2.0", "method": "news.ai_update", "params": ws_item("w2", "after", ["ETH"])}))
        await asyncio.sleep(3)

    gap = {}

    def fetch(body):     # REST knows w1 (already pushed) and gap1, published while the socket was down
        gap.setdefault("item", ws_item("gap1", "while down", ["BTC"]))
        return [ws_item("w1", "first", ["BTC"]), gap["item"]]

    async def main():
        async with ws_server.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = {"transport": "ws", "ws_url": f"ws://127.0.0.1:{port}/x"}
            return await collect(newsliquid_feed(cfg, token="t", fetch=fetch), 3, timeout=6)

    items = asyncio.run(main())
    assert [i.id for i in items] == ["nl_w1", "nl_gap1", "nl_w2"]


def test_ws_silent_socket_is_caught_by_audit_and_rest_takes_over():
    seen = []
    t0 = now_ms()

    async def handler(sock):            # accepts the subscription, then never pushes anything
        sub = json.loads(await sock.recv())
        await sock.send(json.dumps({"jsonrpc": "2.0", "id": sub["id"], "result": {"success": True}}))
        await asyncio.sleep(10)

    calls = []

    def fetch(body):
        if now_ms() < t0 + 300:
            return []                   # startup read and the subscribe backfill see nothing yet
        calls.append(1)
        late = ws_item("late", "rest has it, socket never pushed it", ["BTC"], ts=t0 + 100)
        return [late] if len(calls) == 1 else [late, ws_item("after", "rest polling", ["ETH"])]

    async def main():
        async with ws_server.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = {"transport": "auto", "ws_url": f"ws://127.0.0.1:{port}/x", "poll_s": 0.05, "ws_audit_s": 0.5, "ws_late_s": 0.2}
            return await collect(newsliquid_feed(cfg, token="t", fetch=fetch, on_health=lambda s, d: seen.append(s)), 2, timeout=4)

    items = asyncio.run(main())
    assert [i.id for i in items] == ["nl_late", "nl_after"]
    assert seen[:2] == ["ws_up", "ws_gap"]
