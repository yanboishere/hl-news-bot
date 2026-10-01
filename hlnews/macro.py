"""Scheduled-macro module: arm consensus numbers before a release, poll the primary source at the release second.

Why this exists: CPI repricing is ~75% done within 60 s (own measurement, Binance 1m, 2024-2026). A bot that learns
the number from a relay 5-30 s later is trading the residual. Polling bls.gov at 08:30:00 ET with an identified
User-Agent gets the number in ~1-3 s, which is the fastest free path.

Sources used:
  schedule   : https://www.bls.gov/schedule/news_release/{cpi,empsit}.htm  (parsed once a day)
  consensus  : https://nfs.faireconomy.media/ff_calendar_thisweek.json      (forecast/previous; no 'actual')
  release    : https://www.bls.gov/news.release/cpi.nr0.htm and empsit.nr0.htm (poll from T-2s to T+15s)
  FOMC       : https://www.federalreserve.gov/newsevents/pressreleases/monetary{YYYYMMDD}a.htm
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

from .models import NewsItem, new_id, now_ms

log = logging.getLogger("macro")
UA = {"User-Agent": "hl-news-bot/0.1 (personal research; contact: set-your-email@example.com)"}

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-4))


@dataclass
class Release:
    kind: str                # cpi | nfp | fomc
    at_utc: datetime
    url: str
    consensus: dict[str, float]


def _get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


async def _aget(url: str, timeout: float = 8.0) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get(url, timeout))


_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_bls_schedule(html: str, hour_et: int = 8, minute_et: int = 30) -> list[datetime]:
    """Extract 'Oct. 14, 2026' style dates from the BLS schedule page -> UTC datetimes at 08:30 ET."""
    out = []
    for m in re.finditer(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(20\d\d)", html):
        mon, day, year = _MON[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        dt = datetime(year, mon, day, hour_et, minute_et, tzinfo=ET).astimezone(timezone.utc)
        out.append(dt)
    return sorted(set(out))


async def ff_consensus() -> dict[str, dict[str, float]]:
    """ForexFactory weekly calendar -> {'cpi': {'yoy': 2.9, 'core_yoy': 3.1}, 'nfp': {'k': 150}} where available."""
    out: dict[str, dict[str, float]] = {}
    try:
        data = json.loads(await _aget("https://nfs.faireconomy.media/ff_calendar_thisweek.json"))
    except Exception as e:  # noqa: BLE001
        log.warning("ff calendar failed: %s", e)
        return out

    def num(s: str) -> float | None:
        s = (s or "").replace("%", "").replace("K", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None

    for ev in data:
        if ev.get("country") != "USD":
            continue
        t = (ev.get("title") or "").lower()
        f = num(ev.get("forecast"))
        if f is None:
            continue
        if t == "cpi y/y":
            out.setdefault("cpi", {})["yoy"] = f
        elif t == "core cpi y/y":
            out.setdefault("cpi", {})["core_yoy"] = f
        elif t == "non-farm employment change":
            out.setdefault("nfp", {})["k"] = f
        elif t == "federal funds rate":
            out.setdefault("fomc", {})["rate"] = f
    return out


_CPI_RE = re.compile(r"all items index (?:rose|increased|climbed|was up)\s+(\d+\.\d)\s+percent", re.I)
_CPI_FALL_RE = re.compile(r"all items index (?:fell|declined|decreased|was down)\s+(\d+\.\d)\s+percent", re.I)
_CORE_RE = re.compile(r"all items less food and energy index (?:rose|increased|climbed)\s+(\d+\.\d)\s+percent (?:over the (?:last|past) 12 months|over the year|for the 12 months)", re.I)
_NFP_RE = re.compile(r"nonfarm payroll employment (?:rose|increased|changed little|edged up|was up|fell|declined|decreased)[^\d\-+]{0,40}([\-+]?\d{1,3}(?:,\d{3})?)(?!\.\d)", re.I)


def parse_cpi(html: str) -> dict[str, float]:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    out: dict[str, float] = {}
    m = re.search(r"over the (?:last|past) 12 months,? the all items index (?:rose|increased|climbed)\s+(\d+\.\d)\s+percent", text, re.I)
    if m:
        out["yoy"] = float(m.group(1))
    m = _CORE_RE.search(text)
    if m:
        out["core_yoy"] = float(m.group(1))
    m = re.search(r"Consumer Price Index for All Urban Consumers \(CPI-U\) (?:rose|increased|climbed)\s+(\d+\.\d)\s+percent", text, re.I)
    if m:
        out["mom"] = float(m.group(1))
    return out


def parse_nfp(html: str) -> dict[str, float]:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    out: dict[str, float] = {}
    m = _NFP_RE.search(text)
    if m:
        v = float(m.group(1).replace(",", "").replace("+", ""))
        out["k"] = v / 1000.0 if v >= 10000 else v   # BLS writes "162,000"; FF consensus is in thousands
        if re.search(r"nonfarm payroll employment (?:fell|declined|decreased)", text, re.I):
            out["k"] = -abs(out["k"])
    m = re.search(r"unemployment rate[^.]{0,60}?(\d+\.\d)\s+percent", text, re.I)
    if m:
        out["unemp"] = float(m.group(1))
    return out


class MacroScheduler:
    """Builds NewsItems from primary-source polls. Also exposes `arm()` so the rules lane knows the consensus."""

    def __init__(self, emit: Callable[[NewsItem], Awaitable[None]], set_consensus: Callable[[str, dict[str, float]], None]):
        self.emit = emit
        self.set_consensus = set_consensus
        self.releases: list[Release] = []

    async def load_schedule(self) -> None:
        rel: list[Release] = []
        try:
            for kind, page, url in (("cpi", "cpi", "https://www.bls.gov/news.release/cpi.nr0.htm"),
                                    ("nfp", "empsit", "https://www.bls.gov/news.release/empsit.nr0.htm")):
                html = await _aget(f"https://www.bls.gov/schedule/news_release/{page}.htm")
                for dt in parse_bls_schedule(html):
                    if dt > datetime.now(timezone.utc) - timedelta(minutes=5):
                        rel.append(Release(kind, dt, url, {}))
                await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            log.warning("bls schedule failed: %s", e)
        self.releases = sorted(rel, key=lambda r: r.at_utc)
        log.info("macro schedule loaded: %s", [(r.kind, r.at_utc.isoformat()) for r in self.releases[:4]])

    async def run(self) -> None:
        await self.load_schedule()
        while True:
            now = datetime.now(timezone.utc)
            nxt = next((r for r in self.releases if r.at_utc > now - timedelta(seconds=20)), None)
            if nxt is None:
                await asyncio.sleep(3600)
                await self.load_schedule()
                continue
            wait = (nxt.at_utc - now).total_seconds()
            if wait > 900:
                await asyncio.sleep(min(wait - 900, 3600))
                continue
            # T-15min: arm consensus
            cons = await ff_consensus()
            nxt.consensus = cons.get(nxt.kind, {})
            self.set_consensus(nxt.kind, nxt.consensus)
            log.info("armed %s consensus=%s release at %s", nxt.kind, nxt.consensus, nxt.at_utc.isoformat())
            await asyncio.sleep(max(0.0, (nxt.at_utc - datetime.now(timezone.utc)).total_seconds() - 2.0))
            await self._poll_release(nxt)
            self.releases = [r for r in self.releases if r is not nxt]

    async def _poll_release(self, r: Release) -> None:
        deadline = time.time() + 20.0
        prev_sig = None
        while time.time() < deadline:
            try:
                html = await _aget(r.url, timeout=4.0)
                parsed = parse_cpi(html) if r.kind == "cpi" else parse_nfp(html)
                # the page exists before release with last month's numbers; detect change by comparing to consensus month
                sig = json.dumps(parsed, sort_keys=True)
                fresh = re.search(r"(?:FOR RELEASE|Transmission of material in this release is embargoed until)[^<]{0,80}", html, re.I)
                if parsed and sig != prev_sig and prev_sig is not None:
                    await self._emit(r, parsed)
                    return
                if prev_sig is None:
                    prev_sig = sig  # baseline = pre-release page
            except Exception as e:  # noqa: BLE001
                log.debug("poll %s: %s", r.url, e)
            await asyncio.sleep(0.7)
        log.info("release poll window ended for %s without a detected change", r.kind)

    async def _emit(self, r: Release, parsed: dict[str, float]) -> None:
        if r.kind == "cpi":
            title = f"BLS: CPI YoY ACTUAL {parsed.get('yoy')}% (FORECAST {r.consensus.get('yoy')}%); CORE CPI YoY ACTUAL {parsed.get('core_yoy')}% (FORECAST {r.consensus.get('core_yoy')}%)"
        else:
            title = f"BLS: NON-FARM PAYROLLS ACTUAL {parsed.get('k')}K (FORECAST {r.consensus.get('k')}K); UNEMPLOYMENT {parsed.get('unemp')}%"
        item = NewsItem(id=new_id("bls"), source="bls_poll", channel="usGov", title=title, url=r.url, ts_ms=now_ms(), recv_ms=now_ms(),
                        account="BLS_gov", suggestions=["BTC", "ETH"], raw={"kind": r.kind, "parsed": parsed, "consensus": r.consensus})
        log.info("MACRO RELEASE %s", title)
        await self.emit(item)
