"""NewsLiquid (6551 OpenNews) feed: the bot's news source *and* news judge.

NewsLiquid aggregates ~85 sources (55 news sources incl. Bloomberg, Reuters, jin10, BWEnews, Twitter/X KOLs and
Truth Social; 9 exchange listing feeds; Hyperliquid on-chain; market anomalies; meme; prediction) and attaches an
AI rating to every item: score 0-100, grade, signal long / short / neutral, and the same three fields per tagged
coin. Stock perps on trade.xyz are tagged as "XYZ-NVDA" style symbols next to plain tickers.

This module only transports and normalizes. It never reads the text to decide anything; classify.py maps the
rating to the bot's Classification.

Transport (config feeds.newsliquid.transport):
  ws    wss://ai.6551.io/open/news_wss?token=...   JSON-RPC 2.0. news.subscribe -> news.update (unrated, recorded
        for latency measurement only) and news.ai_update (rated, yielded). Text "ping" -> "pong" keep-alive.
        Real-time push is a Max-plan feature (99U/month per the founder's post of 2026-03-10).
  rest  POST https://ai.6551.io/open/news_search {limit, page, engineTypes, hasCoin}; each call costs 1 quota.
        poll_s 15 -> 5,760 calls/day, ~173k per 30 days (Pro plan: 200k).
  auto  WebSocket first. If the handshake is refused (HTTP 401/402/403: no token, or a plan without WS) or the
        socket is down, REST polling carries the stream; WS is retried every ws_retry_s. After every (re)subscribe one
        REST read backfills what the socket did not deliver, and while the socket carries the stream a REST read every
        ws_audit_s checks that it is not silently dropping items (if it never pushed anything, REST takes over).

Only rated items are yielded. REST items whose aiRating is not done yet stay pending and are re-checked on the
next polls; WS news.update items wait for their news.ai_update. REST rows published before the feed started are
history and never yielded, so a restart cannot re-trade the last page.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from .feeds import poll_interval
from .models import NewsItem, now_ms

log = logging.getLogger("newsliquid")

API_BASE = "https://ai.6551.io"
WS_URL = "wss://ai.6551.io/open/news_wss"
TOKEN_ENV = "OPENNEWS_TOKEN"

_SIGNALS = {"long": "long", "short": "short", "neutral": "neutral", "bullish": "long", "bearish": "short", "none": "neutral"}
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]{1,200}>")


class NLAuthError(Exception):
    """Token missing/invalid, or the plan does not include this endpoint (HTTP 401/402/403)."""


class NLQuotaError(Exception):
    """Quota used up or rate limited (HTTP 429 or a quota error body)."""


# --------------------------------------------------------------------------- normalization
def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _signal(v: Any) -> str:
    return _SIGNALS.get(str(v or "").strip().lower(), "neutral")


def ts_ms(v: Any) -> int | None:
    """NewsLiquid timestamps come as epoch ms (WS), epoch s, or ISO-8601 strings with an offset (REST / free_hot)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return int(x if x > 1e12 else x * 1000)
    s = str(v).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return ts_ms(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _rating(d: dict) -> dict | None:
    """aiRating -> {score, grade, signal, status, summary, summary_en}; None while the AI analysis is not done."""
    ai = d.get("aiRating")
    if isinstance(ai, dict):
        score = _num(ai.get("score"))
        status = str(ai.get("status") or "").strip().lower()
        if score is None or (status and status != "done"):
            return None
        return {"score": score, "grade": str(ai.get("grade") or ""), "signal": _signal(ai.get("signal")), "status": status or "done",
                "summary": str(ai.get("summary") or ""), "summary_en": str(ai.get("enSummary") or "")}
    if _num(d.get("score")) is not None:  # flat shape of the free /open/free_hot endpoint
        return {"score": _num(d.get("score")), "grade": str(d.get("grade") or ""), "signal": _signal(d.get("signal")), "status": "done",
                "summary": str(d.get("summary_zh") or ""), "summary_en": str(d.get("summary_en") or "")}
    return None


def _coins(d: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for c in d.get("coins") or []:
        if isinstance(c, str):
            sym, extra = c, {}
        elif isinstance(c, dict):
            sym, extra = (c.get("symbol") or c.get("coin") or ""), c
        else:
            continue
        sym = str(sym).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        e: dict[str, Any] = {"symbol": sym}
        if extra.get("market_type"):
            e["market_type"] = str(extra["market_type"])
        if _num(extra.get("score")) is not None:
            e["score"] = _num(extra.get("score"))
        if extra.get("signal"):
            e["signal"] = _signal(extra.get("signal"))
        if extra.get("grade"):
            e["grade"] = str(extra["grade"])
        out.append(e)
    return out


def normalize(d: Any) -> dict | None:
    """Any NewsLiquid item shape (WS push params, REST news_search row, free_hot row) -> one flat dict.

    {id, text, news_type, engine_type, link, ts_ms, coins: [{symbol, market_type?, score?, signal?, grade?}],
     rating: {score, grade, signal, status, summary, summary_en} | None}"""
    if not isinstance(d, dict):
        return None
    iid = d.get("id")
    text = d.get("text") or d.get("title") or ""
    if iid in (None, "") or not text:
        return None
    return {
        "id": str(iid),
        "text": str(text),
        "news_type": str(d.get("newsType") or d.get("source") or ""),
        "engine_type": str(d.get("engineType") or d.get("engine_type") or "news").strip().lower(),
        "link": str(d.get("link") or d.get("url") or ""),
        "ts_ms": ts_ms(d.get("ts")) or ts_ms(d.get("published_at")) or ts_ms(d.get("created_at")),
        "coins": _coins(d),
        "rating": _rating(d),
    }


def display_text(text: str) -> str:
    """Some NewsLiquid texts carry HTML (jin10 digests: <b>, <br/>, <span>; entities like &#8217;). Flatten for the tape.
    raw["nl"]["text"] keeps the original."""
    t = _TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", html.unescape(t)).strip()


def to_news_item(nl: dict, recv_ms: int, source: str = "newsliquid", first_seen_ms: int | None = None) -> NewsItem:
    """A normalized, rated NewsLiquid item -> NewsItem. The rating travels in raw["nl"] for classify.py."""
    title = display_text(nl["text"])[:600]
    raw: dict[str, Any] = {"nl": nl, "nl_rated_ms": recv_ms}
    if first_seen_ms:
        raw["nl_first_seen_ms"] = first_seen_ms   # news.update arrival; nl_rated_ms - this = NewsLiquid AI latency
    return NewsItem(
        id="nl_" + nl["id"],
        source=source,
        channel=nl["news_type"] or "NewsLiquid",
        title=title,
        url=nl["link"],
        ts_ms=nl["ts_ms"] or recv_ms,
        recv_ms=recv_ms,
        account=nl["news_type"] or None,
        suggestions=[c["symbol"] for c in nl["coins"]],
        raw=raw,
    )


# --------------------------------------------------------------------------- REST
def rest_rows(resp: Any) -> list[dict]:
    """news_search body -> rows. Seen shapes: {"data": [...], "total": n} and {"success": true, "data": [...]};
    tolerate {"data": {"list": [...]}} as well. A {"success": false, "error": ...} body raises."""
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []
    if resp.get("success") is False:
        err = str(resp.get("error") or resp.get("message") or "request failed")
        if re.search(r"quota|rate.?limit|too many requests|exceeded|insufficient|额度|余额|次数", err, re.I):
            raise NLQuotaError(err)
        if re.search(r"token|unauthori|forbidden|subscription|\bplan\b|权限|订阅", err, re.I):
            raise NLAuthError(err)
        raise RuntimeError(err)
    data = resp.get("data")
    if isinstance(data, dict):
        data = data.get("list") or data.get("items") or data.get("data") or []
    return data if isinstance(data, list) else []


def make_rest_fetch(api_base: str, token: str, timeout: float = 15.0) -> Callable[[dict], list[dict]]:
    """Blocking POST /open/news_search; run it in an executor. Maps HTTP errors to NLAuthError / NLQuotaError."""
    url = api_base.rstrip("/") + "/open/news_search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "hl-news-bot"}

    def fetch(body: dict) -> list[dict]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = (e.read() or b"")[:200].decode("utf-8", "replace")
            if e.code in (401, 402, 403):
                raise NLAuthError(f"HTTP {e.code} {detail}") from None
            if e.code == 429:
                raise NLQuotaError(f"HTTP 429 {detail}") from None
            raise RuntimeError(f"HTTP {e.code} {detail}") from None
        return rest_rows(payload)

    return fetch


# --------------------------------------------------------------------------- feed
def _http_status(e: BaseException) -> int | None:
    """Status code of a refused WebSocket handshake (websockets >= 14: e.response.status_code; legacy: e.status_code)."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None) or getattr(e, "status_code", None)
    try:
        return int(code) if code else None
    except (TypeError, ValueError):
        return None


def _close_code(e: BaseException) -> int | None:
    rcvd = getattr(e, "rcvd", None)
    code = getattr(rcvd, "code", None) if rcvd is not None else getattr(e, "code", None)
    return int(code) if isinstance(code, int) else None


class _Bounded(OrderedDict):
    """Insertion-ordered dict that forgets its oldest keys past `cap`."""

    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def put(self, k: str, v: int) -> None:
        self[k] = v
        self.move_to_end(k)
        while len(self) > self.cap:
            self.popitem(last=False)


async def newsliquid_feed(cfg: dict, on_health: Callable[[str, str], Awaitable[None] | None] | None = None,
                          token: str | None = None, fetch: Callable[[dict], list[dict]] | None = None) -> AsyncIterator[NewsItem]:
    """Yield rated NewsLiquid items as NewsItem(source="newsliquid").

    cfg = config.yaml feeds.newsliquid. The token is read from the environment variable named by cfg["token_env"]
    (default OPENNEWS_TOKEN) unless passed in. `fetch(body) -> rows` replaces the HTTP call in tests.
    on_health(status, detail) is called on ws_up / ws_down / ws_refused / ws_gap / rest_fail / rest_ok / auth_fail / quota.

    History rule: REST rows published before the feed started are never yielded (so a restart cannot re-trade the
    last page); rows without a timestamp count as history on the first REST page only. WebSocket pushes are live by
    definition and always pass."""
    token_env = str(cfg.get("token_env") or TOKEN_ENV)
    token = token if token is not None else os.environ.get(token_env, "")
    transport = str(cfg.get("transport") or "auto").lower()
    if transport not in ("auto", "ws", "rest"):
        log.warning("unknown NewsLiquid transport %r, using auto", transport)
        transport = "auto"
    api_base = str(cfg.get("api_base") or API_BASE)
    ws_url = str(cfg.get("ws_url") or WS_URL)
    engine_types = cfg.get("engine_types") or {}
    coins = [str(c).upper() for c in (cfg.get("coins") or [])]
    has_coin = bool(cfg.get("has_coin", True))
    poll_s = float(cfg.get("poll_s", 15))
    fast_poll_s = float(cfg.get("fast_poll_s", 5))
    page_size = max(1, min(int(cfg.get("page_size", 50)), 100))
    max_pages = max(1, int(cfg.get("max_pages", 3)))
    ws_retry_s = float(cfg.get("ws_retry_s", 1800))
    ws_audit_s = float(cfg.get("ws_audit_s", 300))
    ws_late_s = float(cfg.get("ws_late_s", 120))    # an item REST has but the socket never pushed after this long = a gap
    ping_s = float(cfg.get("ping_s", 20))
    backfill = bool(cfg.get("backfill_on_reconnect", True))

    async def health(status: str, detail: str) -> None:
        if on_health:
            try:
                r = on_health(status, detail)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                pass

    if not token and fetch is None:
        log.error("NewsLiquid token missing: set %s (get one at https://6551.io/mcp); the news feed is off", token_env)
        await health("auth_fail", f"{token_env} not set")
        return

    fetch = fetch or make_rest_fetch(api_base, token)
    body: dict[str, Any] = {"limit": page_size, "page": 1}
    if engine_types:
        body["engineTypes"] = engine_types
    if coins:
        body["coins"] = coins
    if has_coin:
        body["hasCoin"] = True
    if cfg.get("server_min_score"):
        body["score"] = int(cfg["server_min_score"])
    sub_params = {k: v for k, v in body.items() if k in ("engineTypes", "coins", "hasCoin")}

    queue: asyncio.Queue[NewsItem] = asyncio.Queue()
    seen = _Bounded(20000)      # ids already yielded (or recorded as history)
    pending = _Bounded(5000)    # id -> first-seen ms for items whose rating has not arrived yet
    state = {"start_ms": now_ms(), "ws_up": False, "ws_silent": False, "ws_pushes": 0, "ws_blocked_until": 0.0,
             "connects": 0, "rest_polled": False, "rest_fail": 0, "quota_until": 0.0, "last_audit": time.time()}

    def accept(d: Any, history: bool = False) -> NewsItem | None:
        nl = normalize(d)
        if nl is None or nl["id"] in seen:
            return None
        recv = now_ms()
        if nl["rating"] is None:
            if nl["id"] not in pending:
                pending.put(nl["id"], recv)
            return None
        seen.put(nl["id"], recv)
        first = pending.pop(nl["id"], None)
        if history:
            return None
        item = to_news_item(nl, recv, first_seen_ms=first)
        queue.put_nowait(item)
        return item

    def row_ts(x: Any) -> int:
        x = x if isinstance(x, dict) else {}
        return ts_ms(x.get("ts")) or ts_ms(x.get("published_at")) or ts_ms(x.get("created_at")) or 0

    async def rest_once() -> list[NewsItem]:
        """One REST read (page 1, plus pages 2..max_pages while every row of a full page is new). Returns what it queued."""
        loop = asyncio.get_running_loop()
        out: list[NewsItem] = []
        first_poll = not state["rest_polled"]
        for page in range(1, max_pages + 1):
            rows = await loop.run_in_executor(None, fetch, dict(body, page=page))
            rows = [r for r in rows if isinstance(r, dict)]
            unseen = [r for r in rows if str(r.get("id")) not in seen]
            for d in sorted(rows, key=lambda x: (row_ts(x), str(x.get("id")))):
                t = row_ts(d)
                history = (0 < t < state["start_ms"]) or (t == 0 and first_poll)
                item = accept(d, history=history)
                if item is not None:
                    out.append(item)
            state["rest_polled"] = True
            burst = len(rows) >= page_size and len(unseen) == len(rows) and all(row_ts(r) >= state["start_ms"] for r in rows)
            if not burst:
                break
        return out

    async def rest_loop() -> None:
        while True:
            if time.time() < state["quota_until"]:
                await asyncio.sleep(1.0)
                continue
            if transport == "auto" and state["ws_up"] and not state["ws_silent"]:
                # the socket carries the stream; an occasional REST read proves it is not silently dropping items
                if ws_audit_s > 0 and time.time() - state["last_audit"] >= ws_audit_s:
                    state["last_audit"] = time.time()
                    try:
                        late = [i for i in await rest_once() if i.ts_ms < now_ms() - ws_late_s * 1000]
                    except Exception as e:  # noqa: BLE001
                        late = []
                        log.debug("NewsLiquid audit read failed: %s", e)
                    if late:
                        silent = state["ws_pushes"] == 0
                        state["ws_silent"] = silent
                        note = (f"WebSocket missed {len(late)} rated item(s) that REST returned"
                                + ("; it has pushed nothing this session, REST polling takes over" if silent else ""))
                        log.warning("NewsLiquid %s", note)
                        await health("ws_gap", note)
                await asyncio.sleep(1.0)
                continue
            try:
                await rest_once()
                if state["rest_fail"] >= 3:
                    await health("rest_ok", "REST polling recovered")
                state["rest_fail"] = 0
            except NLAuthError as e:
                log.error("NewsLiquid REST refused: %s (token invalid, or the plan does not include news_search)", e)
                await health("auth_fail", f"REST refused: {e}")
                await asyncio.sleep(300)
                continue
            except NLQuotaError as e:
                log.error("NewsLiquid quota / rate limit: %s; pausing REST for 10 min", e)
                state["quota_until"] = time.time() + 600
                await health("quota", str(e))
                continue
            except Exception as e:  # noqa: BLE001
                state["rest_fail"] += 1
                log.warning("NewsLiquid REST failed (%d): %s", state["rest_fail"], e)
                if state["rest_fail"] == 3:
                    await health("rest_fail", f"REST failed 3x: {e}")
            await asyncio.sleep(poll_interval(poll_s, fast_poll_s))

    async def ws_loop() -> None:
        import websockets

        backoff = 1.0
        url = ws_url + ("&" if "?" in ws_url else "?") + "token=" + urllib.parse.quote(token, safe="")

        async def subscribed(sess: dict) -> None:
            nonlocal backoff
            sess["confirmed"] = True
            backoff = 1.0
            state["connects"] += 1
            state["ws_pushes"] = 0
            state["ws_silent"] = False
            state["last_audit"] = time.time()
            if not state["ws_up"]:
                state["ws_up"] = True
                log.info("NewsLiquid WS subscribed %s", json.dumps(sub_params, ensure_ascii=False))
                await health("ws_up", "subscribed")
            if backfill and (state["connects"] > 1 or state["rest_polled"]):
                try:   # one REST read covers what was published since the last REST poll or socket session
                    await rest_once()
                except Exception as e:  # noqa: BLE001
                    log.warning("NewsLiquid backfill after (re)connect failed: %s", e)

        while True:
            wait = state["ws_blocked_until"] - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 60.0))
                continue
            t_open = time.time()
            sess = {"confirmed": False}
            try:
                async with websockets.connect(url, ping_interval=None, open_timeout=15, close_timeout=5, max_size=2**23) as sock:
                    await sock.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "news.subscribe", "params": sub_params}))
                    last_ping = last_rx = time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(sock.recv(), timeout=max(0.2, last_ping + ping_s - time.time()))
                        except asyncio.TimeoutError:
                            if time.time() - last_rx > 3 * ping_s:
                                raise RuntimeError(f"no frame for {3 * ping_s:.0f}s") from None
                            await sock.send("ping")
                            last_ping = time.time()
                            continue
                        last_rx = time.time()
                        if last_rx - last_ping >= ping_s:
                            await sock.send("ping")
                            last_ping = last_rx
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        if raw.strip() == "pong":
                            continue
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        if not sess["confirmed"] and str(msg.get("id")) == "1" and ("result" in msg or "error" in msg):
                            if msg.get("error") or (msg.get("result") or {}).get("success") is False:
                                raise NLAuthError(f"subscribe refused: {json.dumps(msg.get('error') or msg.get('result'))[:200]}")
                            await subscribed(sess)
                            continue
                        method = msg.get("method")
                        if method not in ("news.update", "news.ai_update"):
                            if method == "strategy.triggered":
                                log.debug("strategy.triggered ignored: %s", str(msg.get("params"))[:160])
                            continue
                        if not sess["confirmed"]:
                            await subscribed(sess)        # a push proves the subscription even without an ack frame
                        state["ws_pushes"] += 1
                        params = msg.get("params")
                        nl = normalize(params)
                        if nl is None or nl["id"] in seen:
                            continue
                        if nl["rating"] is not None:
                            accept(params)                # news.ai_update, or a news.update that already carries the rating
                        elif nl["id"] not in pending:
                            pending.put(nl["id"], now_ms())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                status = _http_status(e)
                code = _close_code(e)
                refused = isinstance(e, NLAuthError) or status in (401, 402, 403) or \
                    (code is not None and (code == 1008 or code >= 4000) and time.time() - t_open < 10)
                if state["ws_up"]:
                    state["ws_up"] = False
                    await health("ws_down", str(e)[:160])
                if refused:
                    state["ws_blocked_until"] = time.time() + ws_retry_s
                    note = (f"WebSocket refused ({status or code or e}); "
                            + ("REST polling carries the feed" if transport == "auto" else "no fallback (transport: ws)")
                            + f"; retry in {ws_retry_s / 60:.0f} min")
                    log.warning("NewsLiquid %s", note)
                    await health("ws_refused", note)
                    continue
                log.warning("NewsLiquid WS error: %s; reconnect in %.0fs", str(e)[:160], backoff)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60.0)

    tasks = []
    if transport in ("auto", "rest"):
        tasks.append(asyncio.create_task(rest_loop()))
    if transport in ("auto", "ws"):
        tasks.append(asyncio.create_task(ws_loop()))
    log.info("NewsLiquid feed starting: transport=%s engines=%s hasCoin=%s poll=%ss", transport, engine_types or "all", has_coin, poll_s)
    try:
        while True:
            yield await queue.get()
    finally:
        for t in tasks:
            t.cancel()
