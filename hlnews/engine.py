"""Engine: feeds -> dedup -> rules lane (+ optional LLM lane) -> fusion -> risk gate -> execution -> position manager."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import deque
from typing import AsyncIterator

from .classify import RuleContext, classify_llm, classify_rules
from .hl import HLClient
from .models import Classification, Direction, EventClass, NewsItem, Position, Signal, new_id, now_ms
from .risk import AccountState, RiskGate
from .store import Store

log = logging.getLogger("engine")

_WS = re.compile(r"\s+")


def _fingerprint(title: str) -> str:
    """Near-duplicate key: lowercase, strip handles/urls/punctuation, first 12 words."""
    t = title.lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^[^:]{0,60}\(@[a-z0-9_]+\):\s*", "", t)  # drop "Name (@handle):" prefix
    t = re.sub(r"[^a-z0-9\s%$.]", " ", t)
    words = _WS.sub(" ", t).strip().split(" ")[:12]
    return hashlib.sha1(" ".join(words).encode()).hexdigest()[:16]


class Dedup:
    def __init__(self, window_s: float = 600.0, maxlen: int = 5000):
        self.window_ms = int(window_s * 1000)
        self.seen: deque[tuple[int, str]] = deque(maxlen=maxlen)
        self.index: dict[str, int] = {}

    def is_dup(self, item: NewsItem) -> bool:
        fp = _fingerprint(item.title)
        now = item.recv_ms
        while self.seen and now - self.seen[0][0] > self.window_ms:
            _, old = self.seen.popleft()
            self.index.pop(old, None)
        if fp in self.index:
            return True
        self.index[fp] = now
        self.seen.append((now, fp))
        return False


class Corroboration:
    """Track (event_class, coin, direction) sightings so 'two sources' rules can be applied."""

    def __init__(self, window_s: float):
        self.window_ms = int(window_s * 1000)
        self.hits: dict[tuple[str, str, str], list[tuple[int, str]]] = {}

    def add(self, c: Classification, coin: str, source_key: str, ts: int) -> int:
        k = (c.event_class.value, coin, c.direction.value)
        lst = self.hits.setdefault(k, [])
        lst[:] = [(t, s) for (t, s) in lst if ts - t <= self.window_ms]
        if all(s != source_key for _, s in lst):
            lst.append((ts, source_key))
        return len({s for _, s in lst})


class Engine:
    def __init__(self, cfg: dict, store: Store, hl: HLClient, feeds: list[AsyncIterator[NewsItem]]):
        self.cfg = cfg
        self.store = store
        self.hl = hl
        self.feeds = feeds
        self.mode = cfg.get("mode", "paper")
        uni = set(cfg["universe"]["crypto"]) | set(cfg["universe"]["stocks"])
        rules_cfg = cfg["classifier"]["rules"]
        self.ctx = RuleContext(
            aliases={k.lower(): v for k, v in cfg["universe"]["ticker_aliases"].items()},
            universe=uni,
            trusted_accounts=set(rules_cfg.get("trusted_accounts", [])),
        )
        self.require_two = set(rules_cfg.get("require_two_sources_for", []))
        self.corr = Corroboration(float(rules_cfg.get("corroboration_window_s", 90)))
        self.dedup = Dedup()
        self.templates = cfg["templates"]
        self.llm_cfg = cfg["classifier"]["llm"]
        eq = float(cfg["account"]["equity_usd"])
        self.state = AccountState(equity_usd=eq, day_start_equity=eq, week_start_equity=eq)
        self.risk = RiskGate(cfg["account"], hl.max_lev)
        self.queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=10000)
        self._day = time.gmtime().tm_yday
        self._week = time.gmtime().tm_yday // 7
        self._order_failures = 0
        self.max_age_s = float(cfg["feeds"].get("max_age_s", 20))
        mw = cfg.get("maintenance_window") or {}
        self.mw_enabled = bool(mw.get("enabled", True))
        self.mw_weekday = int(mw.get("weekday_utc", 4))          # 4 = Friday
        self.mw_start = str(mw.get("start_utc", "07:55"))
        self.mw_end = str(mw.get("end_utc", "09:35"))

    def in_maintenance_window(self, t: float | None = None) -> bool:
        """Hyperliquid weekly upgrades (Fri 08:00-09:30 UTC in Aug-Sep 2026) run a post-only window: IOC does not fill
        and exchange-side market stops do not execute. Flatten before, do not open during."""
        if not self.mw_enabled:
            return False
        g = time.gmtime(t or time.time())
        if g.tm_wday != self.mw_weekday:
            return False
        hm = f"{g.tm_hour:02d}:{g.tm_min:02d}"
        return self.mw_start <= hm <= self.mw_end

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._pump(f)) for f in self.feeds]
        tasks.append(asyncio.create_task(self._consume()))
        tasks.append(asyncio.create_task(self._manage_positions()))
        tasks.append(asyncio.create_task(self.hl.mids_loop(float(self.cfg["hyperliquid"].get("poll_prices_s", 2.0)))))
        tasks.append(asyncio.create_task(self._equity_loop()))
        if self.cfg["feeds"].get("macro", {}).get("enabled"):
            from .macro import MacroScheduler

            sched = MacroScheduler(emit=self.queue.put, set_consensus=self.set_consensus)
            tasks.append(asyncio.create_task(sched.run()))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    def set_consensus(self, kind: str, cons: dict) -> None:
        if kind == "cpi":
            self.ctx.cpi_consensus = cons.get("yoy")
            self.ctx.core_cpi_consensus = cons.get("core_yoy")
        elif kind == "nfp":
            self.ctx.nfp_consensus = cons.get("k")
        elif kind == "fomc":
            self.ctx.fomc_expected_bps = cons.get("bps")

    async def _pump(self, feed: AsyncIterator[NewsItem]) -> None:
        async for item in feed:
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                log.error("queue full, dropping %s", item.id)

    async def _equity_loop(self) -> None:
        while True:
            if self.mode == "live":
                try:
                    eq = await self.hl.equity_usd()
                    if eq:
                        self.state.equity_usd = eq
                except Exception as e:  # noqa: BLE001
                    log.warning("equity read failed: %s", e)
            self._roll_periods()
            self.store.equity(now_ms(), self.state.equity_usd, len(self.state.open))
            await asyncio.sleep(60)

    def _roll_periods(self) -> None:
        g = time.gmtime()
        if g.tm_yday != self._day:
            self._day = g.tm_yday
            self.state.day_start_equity = self.state.equity_usd
            if self.state.halted_reason and self.state.halted_reason.startswith("daily"):
                self.state.halted_reason = None
        if g.tm_yday // 7 != self._week:
            self._week = g.tm_yday // 7
            self.state.week_start_equity = self.state.equity_usd

    # ------------------------------------------------------------------ pipeline
    async def _consume(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                await self.handle(item)
            except Exception as e:  # noqa: BLE001
                log.exception("handle failed for %s: %s", item.id, e)

    async def handle(self, item: NewsItem) -> list[Signal]:
        self.store.news(item)
        if item.source != "replay" and item.age_s > self.max_age_s:
            log.debug("stale (%.1fs) %s", item.age_s, item.title[:80])
            return []
        if self.dedup.is_dup(item):
            log.debug("dup %s", item.title[:80])
            return []
        if self.in_maintenance_window():
            log.info("maintenance window, not opening: %s", item.title[:80])
            return []
        t_recv = now_ms()
        rc = classify_rules(item, self.ctx)
        self.store.classification(item.id, rc, now_ms())
        final = rc
        # LLM lane: only when rules found nothing actionable or are not confident. Runs concurrently with a
        # price refresh so the book is fresh by the time the LLM answers.
        if self.llm_cfg.get("enabled") and (rc.event_class is EventClass.NOISE or rc.direction is Direction.NONE or rc.confidence < 0.8):
            lc, _ = await asyncio.gather(classify_llm(item, self.llm_cfg), self.hl.refresh_mids(), return_exceptions=False)
            if lc:
                self.store.classification(item.id, lc, now_ms())
                final = self._fuse(rc, lc)
        if final.event_class is EventClass.NOISE or final.direction is Direction.NONE or not final.tickers:
            return []
        if final.is_rumor:
            log.info("rumor, skip: %s", item.title[:100])
            return []
        template = self.templates.get(final.event_class.value)
        if not template:
            return []
        signals: list[Signal] = []
        for coin in final.tickers:
            if coin not in self.ctx.universe and final.event_class is not EventClass.LISTING_HL:
                continue
            src_key = f"{item.source}:{item.account or item.channel}"
            n_sources = self.corr.add(final, coin, src_key, item.recv_ms)
            trusted = (item.account or "").lower() in {a.lower() for a in self.ctx.trusted_accounts} or item.channel in {"Binance EN", "Upbit", "Bithumb", "usGov"}
            if final.event_class.value in self.require_two and not trusted and n_sources < 2:
                log.info("waiting for corroboration (%d source) %s %s: %s", n_sources, final.event_class.value, coin, item.title[:90])
                continue
            sig = Signal(
                id=new_id("sig"), news_id=item.id, coin=coin, direction=final.direction, event_class=final.event_class,
                confidence=final.confidence, created_ms=now_ms(), template=template, reason=final.reason,
                news_age_s=(t_recv - item.ts_ms) / 1000.0,
            )
            self.store.signal(sig)
            signals.append(sig)
            await self._execute(sig)
        return signals

    @staticmethod
    def _fuse(rc: Classification, lc: Classification) -> Classification:
        """Rules decide when they are confident; otherwise the LLM decides but is capped at 0.85 and
        must agree with the rules when both have a direction."""
        if rc.direction is not Direction.NONE and rc.confidence >= 0.8:
            return rc
        if rc.direction is not Direction.NONE and lc.direction is not Direction.NONE and rc.direction != lc.direction:
            return Classification(lane="fused", event_class=rc.event_class, direction=Direction.NONE, confidence=0.0,
                                  tickers=rc.tickers or lc.tickers, reason=f"lanes disagree: {rc.reason} / {lc.reason}", is_rumor=rc.is_rumor or lc.is_rumor)
        conf = min(lc.confidence, 0.85)
        tickers = rc.tickers or lc.tickers
        ec = lc.event_class if rc.event_class is EventClass.NOISE else rc.event_class
        return Classification(lane="fused", event_class=ec, direction=lc.direction, confidence=conf, tickers=tickers,
                              reason=f"llm: {lc.reason}", is_rumor=rc.is_rumor or lc.is_rumor, latency_ms=rc.latency_ms + lc.latency_ms)

    # ------------------------------------------------------------------ execution
    async def _execute(self, sig: Signal) -> None:
        mid = self.hl.mid(sig.coin)
        if mid is None or now_ms() - self.hl.mids_ms > 15000:
            self.store.decision(sig.id, False, "stale/no price", now_ms())
            return
        # LISTING_HL: direction follows the first ticks, not the headline
        if sig.event_class is EventClass.LISTING_HL:
            await asyncio.sleep(3.0)
            await self.hl.refresh_mids()
            mid2 = self.hl.mid(sig.coin)
            if mid2 is None:
                self.store.decision(sig.id, False, "no price for new listing", now_ms())
                return
            sig.direction = Direction.LONG if mid2 > mid else Direction.SHORT
            mid = mid2
        is_buy = sig.direction is Direction.LONG
        prelim_notional = self.state.equity_usd * float(sig.template.get("risk_pct", 0.5)) / float(sig.template["stop_pct"])
        slip = await self.hl.l2_slippage_bps(sig.coin, min(prelim_notional, float(self.cfg["account"]["max_notional_per_trade_usd"])), is_buy)
        dec = self.risk.check(sig, mid, self.state, slip, self.hl.taker_fee(sig.coin))
        self.store.decision(sig.id, dec.accepted, dec.reason, now_ms())
        if not dec.accepted:
            log.info("REJECT %s %s %s: %s", sig.event_class.value, sig.direction.value, sig.coin, dec.reason)
            return
        pos = Position(
            id=new_id("pos"), signal_id=sig.id, coin=sig.coin, direction=sig.direction, event_class=sig.event_class,
            size=dec.size, entry_px=mid, entry_ms=now_ms(), stop_px=dec.stop_px, tp_px=dec.tp_px, time_exit_ms=dec.time_exit_ms,
            notional_usd=dec.notional_usd, leverage=dec.leverage, mode=self.mode,
        )
        try:
            pos = await self.hl.open_position(pos)
        except Exception as e:  # noqa: BLE001
            log.error("open failed %s: %s", sig.coin, e)
            self.store.decision(sig.id, False, f"open failed: {e}", now_ms())
            self._order_failures += 1
            if self._order_failures >= 3:
                self.state.halted_reason = f"{self._order_failures} consecutive order failures"
                log.warning("HALTED: %s", self.state.halted_reason)
            return
        self._order_failures = 0
        self.state.open.append(pos)
        self.state.last_trade_ms_by_coin[sig.coin] = now_ms()
        self.store.position(pos)
        log.info("OPEN %s %s %s size=%.5f @ %.4f notional=$%.0f (%.2fx equity, margin lev %dx) stop=%.4f tp=%s hold=%ss news_age=%.1fs slip=%.1fbps",
                 self.mode.upper(), sig.direction.value, sig.coin, pos.size, pos.entry_px, pos.notional_usd, dec.equity_ratio, int(pos.leverage),
                 pos.stop_px, f"{pos.tp_px:.4f}" if pos.tp_px else None, sig.template["hold_s"], sig.news_age_s, pos.slippage_bps)

    async def _manage_positions(self) -> None:
        """Paper: emulate stop/TP on mid. Live: TP + time stop here, stop is on-exchange (we still mirror it).
        Also flattens everything 5 minutes before the weekly maintenance window."""
        while True:
            await asyncio.sleep(1.0)
            if not self.state.open:
                continue
            now = now_ms()
            if self.in_maintenance_window(time.time() + 300):
                for pos in list(self.state.open):
                    await self._close(pos, "pre_maintenance")
                continue
            for pos in list(self.state.open):
                mid = self.hl.mid(pos.coin)
                if mid is None:
                    continue
                reason = None
                long = pos.direction is Direction.LONG
                if (long and mid <= pos.stop_px) or (not long and mid >= pos.stop_px):
                    reason = "stop"
                elif pos.tp_px and ((long and mid >= pos.tp_px) or (not long and mid <= pos.tp_px)):
                    reason = "take_profit"
                elif now >= pos.time_exit_ms:
                    reason = "time_stop"
                if reason:
                    await self._close(pos, reason)

    async def _close(self, pos: Position, reason: str) -> None:
        try:
            pos = await self.hl.close_position(pos, reason)
        except Exception as e:  # noqa: BLE001
            log.error("close failed %s: %s", pos.coin, e)
            return
        if pos in self.state.open:
            self.state.open.remove(pos)
        self.store.position(pos)
        if self.mode == "paper":
            self.state.equity_usd += pos.pnl_usd or 0.0
        if (pos.pnl_usd or 0) < 0:
            self.state.last_loss_ms = now_ms()
        day_loss = (self.state.day_start_equity - self.state.equity_usd) / max(self.state.day_start_equity, 1e-9) * 100
        if day_loss >= float(self.cfg["account"]["daily_loss_limit_pct"]):
            self.state.halted_reason = f"daily loss {day_loss:.1f}%"
            log.warning("HALTED: %s", self.state.halted_reason)
        log.info("CLOSE %s %s %s @ %.4f reason=%s pnl=$%.2f fees=$%.2f equity=$%.2f", self.mode.upper(), pos.direction.value, pos.coin,
                 pos.exit_px or 0.0, reason, pos.pnl_usd or 0.0, pos.fee_usd, self.state.equity_usd)

    async def shutdown(self) -> None:
        if self.state.open:
            log.warning("flattening %d open positions", len(self.state.open))
            for p in list(self.state.open):
                await self._close(p, "shutdown")
