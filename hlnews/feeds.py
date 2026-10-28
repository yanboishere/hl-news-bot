"""Feeds. Every feed is an async generator yielding NewsItem.

News comes from NewsLiquid (newsliquid.py). This module keeps the two first-party data lanes and replay:
  hl_listings_feed  diff of the Hyperliquid perp universe -> LISTING_HL items
  replay_feed       JSONL replay: NewsLiquid item records, BLS macro records, or plain headlines (unrated)
The BLS macro poller lives in macro.py.

The self-built news crawlers (Tree of Alpha, Telegram, Google News, 华尔街见闻, 金十, 财联社) were removed when
NewsLiquid took over news collection and rating; see git history before commit
"Replace self-built news feeds and classifier with NewsLiquid".
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from .models import NewsItem, new_id, now_ms

log = logging.getLogger("feeds")

# --------------------------------------------------------------------------- hot window
# While "hot", polling feeds tighten their interval to fast_poll_s: around scheduled macro releases (macro.py) and
# while a single-source item waits for its second source (engine.py). Today the only polling news feed is
# NewsLiquid REST (used when its WebSocket is unavailable).
_HOT_UNTIL_MS = 0


def set_hot(duration_s: float) -> None:
    global _HOT_UNTIL_MS
    until = now_ms() + int(duration_s * 1000)
    if until > _HOT_UNTIL_MS:
        _HOT_UNTIL_MS = until
        log.info("hot window until +%.0fs: polling feeds switch to fast interval", duration_s)


def is_hot() -> bool:
    return now_ms() < _HOT_UNTIL_MS


def poll_interval(normal_s: float, fast_s: float) -> float:
    return min(fast_s, normal_s) if is_hot() else normal_s


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
                coin = n  # HIP-3 names already carry the "dex:" prefix
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


# --------------------------------------------------------------------------- Replay
def _is_newsliquid_record(d: dict) -> bool:
    return any(k in d for k in ("aiRating", "newsType", "engineType", "engine_type")) or ("score" in d and "signal" in d)


async def replay_feed(path: str, speed: float = 0.0) -> AsyncIterator[NewsItem]:
    """Replay a JSONL file through the full pipeline. speed=0 -> as fast as possible; speed=1 -> real time.

    Accepted lines:
      NewsLiquid records  any shape newsliquid.normalize() reads (WS push params, news_search rows, free_hot rows);
                          they are classified by the NewsLiquid lane exactly like live items
      BLS macro records   {"kind": "cpi"|"nfp", "parsed": {...}, "consensus": {...}, "title": "..."}
      plain headlines     {"title": ..., "ts_ms": ...}: stored for the tape, never directional (nothing rates them)
    """
    from .newsliquid import normalize, to_news_item   # local import: newsliquid imports this module

    prev_ts = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            nl = normalize(d) if _is_newsliquid_record(d) else None
            ts = (nl or {}).get("ts_ms") or int(d.get("ts_ms") or d.get("time") or now_ms())
            if speed and prev_ts is not None:
                await asyncio.sleep(max(0.0, (ts - prev_ts) / 1000.0 / speed))
            prev_ts = ts
            if nl is not None:
                yield to_news_item(nl, now_ms(), source="replay")
                continue
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
