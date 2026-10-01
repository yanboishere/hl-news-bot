"""News feeds. Every feed is an async generator yielding NewsItem."""
from __future__ import annotations

import asyncio
import json
import logging
import random
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
