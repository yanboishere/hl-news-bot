"""Engine: feeds -> dedup -> classify (NewsLiquid rating / BLS arithmetic / HL listing) -> per-coin template,
threshold and source policy -> risk gate -> execution -> position manager."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import deque
from typing import AsyncIterator

from .alerts import Alerter
from .classify import ClassifyContext, classify, event_class_for
from .hl import HLClient
from .models import Direction, EventClass, NewsItem, Position, Signal, new_id, now_ms
from .risk import AccountState, RiskGate
from .store import Store

log = logging.getLogger("engine")

_WS = re.compile(r"\s+")


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _fingerprint(title: str) -> str:
    """Near-duplicate key. Latin text: lowercase, strip handles/urls/punctuation, first 12 words.
    CJK text has no word boundaries: use the first 24 CJK/alnum characters plus every number in the text,
    so a Chinese flash and its re-post fold together while two different items about the same company do not."""
    t = title.lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^[^:]{0,60}\(@[a-z0-9_]+\):\s*", "", t)  # drop "Name (@handle):" prefix
    if len(_CJK_RE.findall(t)) >= 6:
        chars = re.sub(r"[^\u3040-\u30ff\u3400-\u9fffa-z0-9]", "", t)
        nums = "".join(re.findall(r"\d+(?:\.\d+)?", t)[:6])
        return hashlib.sha1((chars[:24] + "|" + nums).encode()).hexdigest()[:16]
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
    """Track (event_class, coin, direction) sightings from distinct sources so 'two sources' policies can be applied.
    The window is per event class (stock news diffuses slower than crypto news)."""

    def __init__(self, window_s: float, by_class: dict[str, float] | None = None):
        self.window_ms = int(window_s * 1000)
        self.by_class = {k: int(v * 1000) for k, v in (by_class or {}).items()}
        self.hits: dict[tuple[str, str, str], list[tuple[int, str]]] = {}

    def add(self, event_class: str, coin: str, direction: str, source_key: str, ts: int) -> int:
        k = (event_class, coin, direction)
        win = self.by_class.get(event_class, self.window_ms)
        lst = self.hits.setdefault(k, [])
        lst[:] = [(t, s) for (t, s) in lst if ts - t <= win]
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
        rating_cfg = cfg.get("rating") or {}
        self.ctx = ClassifyContext(
            symbol_map={str(k).strip().upper(): str(v) for k, v in (cfg["universe"].get("symbol_map") or {}).items()},
            universe=uni,
            min_score=float(rating_cfg.get("min_score", 0)),
            use_coin_rating=bool(rating_cfg.get("use_coin_rating", True)),
            item_rating_fallback=str(rating_cfg.get("item_rating_fallback", "single")).lower(),
        )
        # source policy: NewsLiquid newsTypes (lower-case) whose items need a second, different source agreeing on
        # (class, coin, direction) inside the window before a position opens -- a lone tweet never trades
        self.require_two_sources = {str(s).strip().lower() for s in rating_cfg.get("require_two_sources_for") or []}
        self.corr = Corroboration(float(rating_cfg.get("corroboration_window_s", 90)), rating_cfg.get("corroboration_window_by_class") or {})
        self.alerter = Alerter(cfg, store)
        self.feed_was_down: dict[str, bool] = {}
        self._alert_recent: dict[tuple[str, str, str], int] = {}
        self.alert_dedup_ms = int(float((cfg.get("alerts") or {}).get("dedup_s", 21600)) * 1000)
        self.dedup = Dedup()
        self.templates = cfg["templates"]
        eq = float(cfg["account"]["equity_usd"])
        self.state = AccountState(equity_usd=eq, day_start_equity=eq, week_start_equity=eq)
        self.risk = RiskGate(cfg["account"], hl.max_lev)
        self.queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=10000)
        self._day = time.gmtime().tm_yday
        self._week = time.gmtime().tm_yday // 7
        self._order_failures = 0
        self._last_news_ms = 0
        self.max_age_s = float(cfg["feeds"].get("max_age_s", 120))
        self.max_age_by_source = {k: float(v) for k, v in (cfg["feeds"].get("max_age_by_source") or {}).items()}
        mw = cfg.get("maintenance_window") or {}
        self.mw_enabled = bool(mw.get("enabled", True))
        self.mw_weekday = int(mw.get("weekday_utc", 4))          # 4 = Friday
        self.mw_start = str(mw.get("start_utc", "07:55"))
        self.mw_end = str(mw.get("end_utc", "09:35"))

    def add_feed(self, feed: AsyncIterator[NewsItem]) -> None:
        """Register a feed after construction (used when a feed needs a reference to the engine, e.g. for health alerts)."""
        self.feeds.append(feed)

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
    async def _settle_orphans(self) -> None:
        """Positions an earlier process left open: it was killed before it could flatten (SIGKILL, workspace stop).
        Paper records are closed as restart_abandoned (no P&L, out of the stats); live ones are only reported, since the
        position may still exist on the exchange and this process does not take it over."""
        try:
            rows = self.store.open_position_rows()
        except Exception as e:  # noqa: BLE001
            log.warning("could not read open positions left by an earlier run: %s", e)
            return
        if not rows:
            return
        paper = [r for r in rows if r.get("mode") != "live"]
        live = [r for r in rows if r.get("mode") == "live"]

        def names(rs: list[dict]) -> str:
            return "、".join(f"{r.get('coin')} {r.get('direction')}" for r in rs[:8]) + ("…" if len(rs) > 8 else "")

        if paper:
            self.store.mark_abandoned([r["id"] for r in paper], now_ms())
            log.warning("closed the records of %d paper position(s) left open by an earlier run: %s", len(paper), names(paper))
            await self.alerter.send("close", f"上次运行留下 {len(paper)} 笔模拟持仓",
                                    f"上次进程被强制结束（工作区停止或 kill -9），没来得及平仓：{names(paper)}。"
                                    "已记为 restart_abandoned，没有盈亏，不计入统计。", {"positions": [r["id"] for r in paper]})
        if live:
            log.error("%d live position(s) from an earlier run have no exit record: %s", len(live), names(live))
            await self.alerter.send("halt", f"上次运行留下 {len(live)} 笔实盘持仓没有平仓记录",
                                    f"{names(live)}。bot 重启后不会接管它们；交易所侧止损单还在。"
                                    "请到 Hyperliquid 核对并手动处理，处理完这些记录会一直显示为持仓中。",
                                    {"positions": [r["id"] for r in live]})

    async def run(self) -> None:
        await self._settle_orphans()
        tasks = [asyncio.create_task(self._pump(f)) for f in self.feeds]
        tasks.append(asyncio.create_task(self._consume()))
        tasks.append(asyncio.create_task(self._manage_positions()))
        tasks.append(asyncio.create_task(self.hl.mids_loop(float(self.cfg["hyperliquid"].get("poll_prices_s", 2.0)))))
        tasks.append(asyncio.create_task(self._equity_loop()))
        tasks.append(asyncio.create_task(self._heartbeat()))
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
            self._write_state(stopped=True)

    def _state_path(self) -> str:
        import os

        return os.path.join(os.path.dirname(self.cfg["storage"]["sqlite_path"]) or ".", "state.json")

    def _write_state(self, stopped: bool = False) -> None:
        """Small JSON heartbeat for the UI: equity, open positions, halt reason, feed liveness. Written atomically."""
        import json
        import os

        st = {
            "ts_ms": now_ms(),
            "pid": os.getpid(),
            "stopped": stopped,
            "mode": self.mode,
            "equity_usd": self.state.equity_usd,
            "day_start_equity": self.state.day_start_equity,
            "week_start_equity": self.state.week_start_equity,
            "halted_reason": self.state.halted_reason,
            "open": [p.to_row() for p in self.state.open],
            "mids": {p.coin: self.hl.mid(p.coin) for p in self.state.open},
            "mids_ms": self.hl.mids_ms,
            "last_news_ms": self._last_news_ms,
            "queue_size": self.queue.qsize(),
            "consensus": {"cpi": self.ctx.cpi_consensus, "core_cpi": self.ctx.core_cpi_consensus, "nfp": self.ctx.nfp_consensus, "fomc_bps": self.ctx.fomc_expected_bps},
        }
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            log.debug("state write failed: %s", e)

    async def _heartbeat(self) -> None:
        while True:
            self._write_state()
            await asyncio.sleep(2.0)

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
        self._last_news_ms = item.recv_ms
        if item.source != "replay" and item.age_s > self.max_age_by_source.get(item.source, self.max_age_s):
            log.debug("stale (%.1fs) %s", item.age_s, item.title[:80])
            return []
        if self.dedup.is_dup(item):
            log.debug("dup %s", item.title[:80])
            return []
        if self.in_maintenance_window():
            log.info("maintenance window, not opening: %s", item.title[:80])
            return []
        t_recv = now_ms()
        final = classify(item, self.ctx)
        self.store.classification(item.id, final, now_ms())
        if final.event_class is EventClass.NOISE or final.direction is Direction.NONE or not final.tickers:
            return []
        signals: list[Signal] = []
        src = (item.channel or "").strip().lower()
        src_key = f"{item.source}:{src}"
        for coin in final.tickers:
            if coin not in self.ctx.universe and final.event_class is not EventClass.LISTING_HL:
                continue
            # NewsLiquid rates each coin on its own when it can: BTC long 85 and ETH short 45 in one item stay separate
            pt = final.per_ticker.get(coin) or {}
            direction = Direction(pt["direction"]) if pt else final.direction
            confidence = float(pt.get("confidence", final.confidence)) if pt else final.confidence
            if direction is Direction.NONE:
                continue
            ec = event_class_for(final.event_class, coin)
            template = self.templates.get(ec.value)
            if not template:
                log.debug("no template for %s, skip %s", ec.value, coin)
                continue
            # per-class freshness: a 30-minute-old crypto headline must not enter a 60-minute template; stock news may be older
            max_news_age = template.get("max_news_age_s")
            if item.source != "replay" and max_news_age is not None and item.age_s > float(max_news_age):
                log.info("too old for %s (%.0fs > %ss): %s", ec.value, item.age_s, max_news_age, item.title[:80])
                continue
            if confidence < float(template.get("min_conf", 0.8)):
                log.debug("below %s threshold (%.2f < %s) %s: %s", ec.value, confidence, template.get("min_conf"), coin, item.title[:80])
                continue
            n_sources = self.corr.add(ec.value, coin, direction.value, src_key, item.recv_ms)
            if src in self.require_two_sources and n_sources < 2:
                from .feeds import set_hot

                # poll NewsLiquid REST fast for a while (when REST is the transport) so the second source lands quickly.
                # Only here: a hot window after every directional item would multiply the REST quota.
                set_hot(min(120.0, float(self.corr.by_class.get(ec.value, self.corr.window_ms)) / 1000.0))
                log.info("waiting for a second source (%s, %d so far) %s %s %s: %s", item.channel, n_sources, ec.value, direction.value, coin, item.title[:90])
                continue
            sig = Signal(
                id=new_id("sig"), news_id=item.id, coin=coin, direction=direction, event_class=ec,
                confidence=confidence, created_ms=now_ms(), template=template, reason=final.reason,
                news_age_s=(t_recv - item.ts_ms) / 1000.0,
            )
            self.store.signal(sig)
            signals.append(sig)
        if not signals:
            return []
        # key-news alert: once per item, and not again for the same (class, coins, direction) within alerts.dedup_s --
        # a story is re-reported by many outlets over a day
        alerting = [s for s in signals if s.template.get("alert")]
        if alerting:
            akey = (alerting[0].event_class.value, " ".join(sorted(s.coin for s in alerting)), alerting[0].direction.value)
            if now_ms() - self._alert_recent.get(akey, 0) > self.alert_dedup_ms:
                self._alert_recent[akey] = now_ms()
                trade_any = any(s.template.get("trade", True) for s in alerting)
                legs = " ".join(f"{s.coin} {s.direction.value} {s.confidence * 100:.0f}" for s in alerting)
                src_label = f"NewsLiquid · {item.channel}" if isinstance(item.raw.get("nl"), dict) else item.channel
                await self.alerter.send(
                    "key_news",
                    f"{alerting[0].event_class.value} {legs}",
                    f"{item.title[:300]}\n来源 {src_label} · {final.reason}"
                    + ("" if trade_any else "\n（仅报警，模板 trade: false）"),
                    {"news_id": item.id, "url": item.url, "coins": [s.coin for s in alerting],
                     "confidence": max(s.confidence for s in alerting), "trade": trade_any},
                )
        for sig in signals:
            if sig.template.get("trade", True) is False:
                self.store.decision(sig.id, False, "alert-only template (trade: false)", now_ms())
                continue
            await self._execute(sig)
        return signals

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
        await self.alerter.send("open", f"开仓 {sig.direction.value} {sig.coin}",
                                f"{pos.size:g} @ {pos.entry_px:.4g}，名义 ${pos.notional_usd:.0f}，止损 {pos.stop_px:.4g}，止盈 {pos.tp_px:.4g}，持有 {int(sig.template['hold_s']) // 60} 分钟\n{sig.reason}" if pos.tp_px else
                                f"{pos.size:g} @ {pos.entry_px:.4g}，名义 ${pos.notional_usd:.0f}，止损 {pos.stop_px:.4g}\n{sig.reason}",
                                {"position_id": pos.id, "signal_id": sig.id, "coin": sig.coin})

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
        await self.alerter.send("close", f"平仓 {pos.direction.value} {pos.coin}：{reason}",
                                f"@ {pos.exit_px or 0:.4g}，盈亏 {pos.pnl_usd or 0:+.2f}（含费 {pos.fee_usd:.2f}），权益 {self.state.equity_usd:.2f}",
                                {"position_id": pos.id, "coin": pos.coin, "pnl_usd": pos.pnl_usd})
        if self.state.halted_reason and self.state.halted_reason.startswith("daily"):
            await self.alerter.send("halt", "熔断", self.state.halted_reason, {})

    async def shutdown(self) -> None:
        if self.state.open:
            log.warning("flattening %d open positions", len(self.state.open))
            for p in list(self.state.open):
                await self._close(p, "shutdown")
