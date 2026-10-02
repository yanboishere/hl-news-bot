"""News feeds. Every feed is an async generator yielding NewsItem."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from typing import AsyncIterator

from .models import NewsItem, new_id, now_ms

log = logging.getLogger("feeds")


# --------------------------------------------------------------------------- Tree of Alpha
def parse_toa(d: dict) -> NewsItem | None:
    """Tree of Alpha payload -> NewsItem.

    Observed shapes (2026-10-01, free WS + REST history):
      Twitter (REST):   {_id, source:"Twitter", title:"Name (@handle): text", url, time, info{twitterId,truthId,isReply,isRetweet,isQuote}, suggestions[]}
      Blogs/Binance EN/Upbit/Bithumb/usGov: {title, source, url, time, symbols[], sourceName, en, firstPrice{}, suggestions[]}
      WS "direct" (Twitter/Truth Social push): {title:"Name (@handle)", body:"text", link, type:"direct", info{twitterId,truthId,...},
                                               suggestions[], stockSuggestions[], time, _id}   (no `source` key)
    """
    if not isinstance(d, dict) or "title" not in d:
        return None
    info = d.get("info") or {}
    title = d.get("en") or d.get("title") or ""
    body = d.get("body") or ""
    if body:
        title = f"{title}: {body}" if title and not title.endswith(":") else f"{title} {body}"
    source = d.get("source")
    if not source:
        source = "TruthSocial" if info.get("truthId") else ("Twitter" if info.get("twitterId") else (d.get("type") or "unknown"))
    account = None
    if source in ("Twitter", "TruthSocial"):
        head = title.split(":", 1)[0]
        if "(@" in head and head.endswith(")"):
            account = head[head.index("(@") + 2 : -1]
        if info.get("isRetweet") or info.get("isReply"):
            return None  # retweets/replies are noise for us
        if info.get("truthId") and not account:
            account = "realDonaldTrump"
    else:
        account = d.get("sourceName")
    coins = []
    for s in d.get("suggestions") or []:
        c = s.get("coin") if isinstance(s, dict) else s
        if c:
            coins.append(str(c).upper())
    for s in d.get("stockSuggestions") or []:
        c = s.get("symbol") if isinstance(s, dict) else s
        if c:
            coins.append(str(c).upper())
    ts = int(d.get("time") or now_ms())
    return NewsItem(
        id=str(d.get("_id") or new_id("toa")),
        source="treeofalpha",
        channel=source,
        title=title.strip(),
        url=d.get("url") or d.get("link") or "",
        ts_ms=ts,
        recv_ms=now_ms(),
        account=account,
        suggestions=coins,
        raw=d,
    )


async def treeofalpha_feed(ws_url: str, api_key: str = "", max_age_s: float = 20.0) -> AsyncIterator[NewsItem]:
    import websockets

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                if api_key:
                    await ws.send(f"login {api_key}")
                log.info("treeofalpha connected")
                backoff = 1.0
                async for raw in ws:
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    item = parse_toa(d)
                    if item is None:
                        continue
                    if item.age_s > max_age_s:
                        log.debug("drop stale toa item age=%.1fs %s", item.age_s, item.title[:80])
                        continue
                    yield item
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("treeofalpha ws error: %s; reconnect in %.1fs", e, backoff)
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30.0)


# --------------------------------------------------------------------------- Hyperliquid new listings
async def hl_listings_feed(info_post, poll_s: float = 5.0, dexs: tuple[str, ...] = ("", "xyz")) -> AsyncIterator[NewsItem]:
    """Diff the perp universe; a new, non-delisted name becomes a LISTING_HL news item.

    info_post(body: dict) -> dict must be an async callable that POSTs to /info.
    """
    known: dict[str, set[str]] = {}
    first = True
    while True:
        for dex in dexs:
            try:
                body = {"type": "meta"} if dex == "" else {"type": "meta", "dex": dex}
                meta = await info_post(body)
                names = {u["name"] for u in meta.get("universe", []) if not u.get("isDelisted")}
            except Exception as e:  # noqa: BLE001
                log.warning("hl listings poll failed dex=%r: %s", dex, e)
                continue
            prev = known.get(dex)
            known[dex] = names
            if prev is None or first:
                continue
            for n in sorted(names - prev):
                coin = n if dex == "" else n  # HIP-3 names already carry the "dex:" prefix
                yield NewsItem(
                    id=new_id("hl"),
                    source="hl_listings",
                    channel="meta_diff",
                    title=f"Hyperliquid listed new perp {coin}",
                    url="https://app.hyperliquid.xyz/trade/" + coin,
                    ts_ms=now_ms(),
                    recv_ms=now_ms(),
                    account="hyperliquid",
                    suggestions=[coin.split(":")[-1]],
                    raw={"dex": dex, "name": n},
                )
            await asyncio.sleep(1.5)  # stay well under the IP weight budget
        first = False
        await asyncio.sleep(poll_s)


# --------------------------------------------------------------------------- Telegram (optional)
async def telegram_feed(api_id: int, api_hash: str, session: str, channels: list[str]) -> AsyncIterator[NewsItem]:
    """Subscribe to public channels with a *user* session (Telethon). Bot API cannot read channels."""
    try:
        from telethon import TelegramClient, events  # type: ignore
    except ImportError:
        log.error("telethon not installed; telegram feed disabled")
        return
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    queue: asyncio.Queue[NewsItem] = asyncio.Queue()

    @client.on(events.NewMessage(chats=channels))
    async def _handler(ev):  # noqa: ANN001
        text = ev.raw_text or ""
        if not text:
            return
        chat = await ev.get_chat()
        uname = getattr(chat, "username", None) or str(ev.chat_id)
        await queue.put(
            NewsItem(
                id=f"tg_{uname}_{ev.id}",
                source="telegram",
                channel="@" + uname,
                title=text.strip(),
                url=f"https://t.me/{uname}/{ev.id}",
                ts_ms=int(ev.date.timestamp() * 1000),
                recv_ms=now_ms(),
                account=uname,
                suggestions=[],
                raw={"chat_id": ev.chat_id, "msg_id": ev.id},
            )
        )

    log.info("telegram connected, channels=%s", channels)
    while True:
        yield await queue.get()


# --------------------------------------------------------------------------- Replay
async def replay_feed(path: str, speed: float = 0.0) -> AsyncIterator[NewsItem]:
    """Replay a JSONL file of NewsItem-like dicts. speed=0 -> as fast as possible; speed=1 -> real time."""
    prev_ts = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            ts = int(d.get("ts_ms") or d.get("time") or now_ms())
            if speed and prev_ts is not None:
                await asyncio.sleep(max(0.0, (ts - prev_ts) / 1000.0 / speed))
            prev_ts = ts
            yield NewsItem(
                id=str(d.get("id") or new_id("rp")),
                source="replay",
                channel=d.get("channel", "replay"),
                title=d["title"],
                url=d.get("url", ""),
                ts_ms=ts,
                recv_ms=now_ms(),
                account=d.get("account"),
                suggestions=[c.upper() for c in d.get("suggestions", [])],
                raw=d,
            )


# --------------------------------------------------------------------------- Google News RSS (equities / sector news)
async def google_news_feed(queries: list[str], poll_s: float = 90.0, hl: str = "en-US") -> AsyncIterator[NewsItem]:
    """Poll Google News RSS search for each query. Free, no key, publisher timestamp in pubDate.

    Why: Tree of Alpha is a crypto feed. Nikkei / Reuters / digitimes equity stories (e.g. Toshiba doubling HDD capacity,
    2026-10-01 21:59 UTC) never appear there, but are indexed by Google News within minutes. The edge on this kind of
    news is coverage and mapping, not latency: the Hyperliquid WDC perp did not react for 13 hours.
    """
    import email.utils
    import html as _html
    import urllib.parse
    import urllib.request

    gl, ceid = ("CN", "CN:zh-Hans") if hl.startswith("zh") else (("JP", "JP:ja") if hl.startswith("ja") else ("US", "US:en"))
    seen: dict[str, int] = {}
    first = True

    def fetch(q: str) -> list[tuple[int, str, str, str, str]]:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q + ' when:1d')}&hl={hl}&gl={gl}&ceid={ceid}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hl-news-bot)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "ignore")
        out = []
        for it in re.findall(r"<item>(.*?)</item>", raw, re.S):
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            p = re.search(r"<pubDate>(.*?)</pubDate>", it)
            l = re.search(r"<link>(.*?)</link>", it) or re.search(r"<guid[^>]*>(.*?)</guid>", it)
            src = re.search(r"<source[^>]*>(.*?)</source>", it)
            if not (t and p):
                continue
            try:
                ts = int(email.utils.parsedate_to_datetime(p.group(1)).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                continue
            title = _html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))).strip()
            pub = _html.unescape(src.group(1)).strip() if src else ""
            # Google appends " - Publisher" to titles; strip it since the publisher is carried separately
            if pub and title.endswith(" - " + pub):
                title = title[: -(len(pub) + 3)].rstrip()
            out.append((ts, title, l.group(1).strip() if l else "", pub, q))
        return out

    loop = asyncio.get_running_loop()
    while True:
        for q in queries:
            try:
                items = await loop.run_in_executor(None, fetch, q)
            except Exception as e:  # noqa: BLE001
                log.warning("google news fetch failed for %r: %s", q, e)
                items = []
            for ts, title, link, pub, query in sorted(items):
                key = link or (pub + "|" + title)
                if key in seen:
                    continue
                seen[key] = ts
                if first:
                    continue  # baseline: do not replay yesterday's index on startup
                yield NewsItem(
                    id="gn_" + hashlib.sha1(key.encode()).hexdigest()[:16],
                    source="google_news",
                    channel=pub or "Google News",
                    title=title,
                    url=link,
                    ts_ms=ts,
                    recv_ms=now_ms(),
                    account=pub or None,
                    suggestions=[],
                    raw={"query": query, "publisher": pub},
                )
            await asyncio.sleep(1.0)
        first = False
        if len(seen) > 5000:
            for k in sorted(seen, key=seen.get)[:2000]:
                seen.pop(k, None)
        await asyncio.sleep(poll_s)


# --------------------------------------------------------------------------- 华尔街见闻 7x24
async def wallstreetcn_feed(poll_s: float = 60.0) -> AsyncIterator[NewsItem]:
    """华尔街见闻 live feed (Chinese, ~370 items/day, no key). display_time is unix seconds.
    Chinese financial flash news often carries the same number as the English wire within the same minute, and it
    covers Asian company news (Toshiba, SK Hynix, CXMT) that English crypto feeds skip."""
    import urllib.request

    url = "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&client=pc&limit=40"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # direct: the site is reachable without the proxy
    seen: set[str] = set()
    first = True
    loop = asyncio.get_running_loop()

    def fetch() -> list[dict]:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hl-news-bot)"})
        with opener.open(req, timeout=20) as r:
            d = json.loads(r.read())
        return d.get("data", {}).get("items", [])

    while True:
        try:
            items = await loop.run_in_executor(None, fetch)
        except Exception as e:  # noqa: BLE001
            log.warning("wallstreetcn fetch failed: %s", e)
            items = []
        for it in sorted(items, key=lambda x: int(x.get("display_time", 0))):
            iid = str(it.get("id") or it.get("display_time"))
            if iid in seen:
                continue
            seen.add(iid)
            if first:
                continue
            title = (it.get("title") or "").strip()
            body = re.sub(r"<[^>]+>", "", it.get("content_text") or it.get("content") or "").strip()
            text = f"{title} {body}".strip() if title and body and not body.startswith(title) else (body or title)
            if not text:
                continue
            yield NewsItem(
                id="wscn_" + iid,
                source="wallstreetcn",
                channel="华尔街见闻",
                title=text[:600],
                url=it.get("uri") or f"https://wallstreetcn.com/livenews/{iid}",
                ts_ms=int(it.get("display_time", 0)) * 1000,
                recv_ms=now_ms(),
                account="华尔街见闻",
                suggestions=[],
                raw={"id": iid},
            )
        first = False
        if len(seen) > 5000:
            seen = set(list(seen)[-2000:])
        await asyncio.sleep(poll_s)
