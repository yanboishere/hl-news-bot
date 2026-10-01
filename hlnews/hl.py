"""Hyperliquid market data + execution. One class, two modes (paper / live).

Paper mode: fills at the live mid +/- a slippage model, charges taker fees, tracks positions locally.
Live mode: uses the official SDK with an API (agent) wallet. IOC limit with a tight slippage cap,
reduce-only stop as a trigger order on the exchange, time stop handled by the engine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any

from .models import Direction, Position, now_ms

log = logging.getLogger("hl")

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"

# fee assumptions for paper mode (base tier, taker). HIP-3 growth mode = 10% of standard HIP-3 fee.
TAKER_FEE_MAIN = 0.00045
TAKER_FEE_HIP3_GROWTH = 0.00009
TAKER_FEE_HIP3_STD = 0.0009


def _round_px(px: float, sz_decimals: int) -> float:
    """5 significant figures and at most 6 - szDecimals decimals (perps)."""
    if px <= 0:
        return px
    sig = float(f"{px:.5g}")
    return round(sig, max(0, 6 - sz_decimals))


def _round_sz(sz: float, sz_decimals: int) -> float:
    f = 10 ** sz_decimals
    return math.floor(sz * f) / f


class HLClient:
    def __init__(self, cfg: dict, mode: str):
        self.cfg = cfg
        self.mode = mode
        self.base_url = TESTNET if cfg.get("network") == "testnet" else MAINNET
        self.perp_dexs: list[str] = list(cfg.get("perp_dexs") or ["", "xyz"])
        self.slippage = float(cfg.get("market_slippage", 0.003))
        self.mids: dict[str, float] = {}
        self.mids_ms: int = 0
        self.sz_decimals: dict[str, int] = {}
        self.max_lev: dict[str, int] = {}
        self.growth_mode: dict[str, bool] = {}
        self.only_isolated: dict[str, bool] = {}
        self._info = None
        self._exchange = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        from hyperliquid.info import Info

        loop = asyncio.get_running_loop()
        self._info = await loop.run_in_executor(None, lambda: Info(self.base_url, skip_ws=True, perp_dexs=self.perp_dexs, timeout=10))
        # meta per dex for szDecimals / maxLeverage / growthMode
        for dex in self.perp_dexs:
            body = {"type": "meta"} if dex == "" else {"type": "meta", "dex": dex}
            meta = await self.info_post(body)
            for u in meta.get("universe", []):
                name = u["name"]
                self.sz_decimals[name] = int(u["szDecimals"])
                self.max_lev[name] = int(u.get("maxLeverage", 1))
                self.growth_mode[name] = u.get("growthMode") == "enabled"
                self.only_isolated[name] = bool(u.get("onlyIsolated")) or u.get("marginMode") in ("noCross", "strictIsolated")
            await asyncio.sleep(1.0)
        await self.refresh_mids()
        if self.mode == "live":
            self._setup_live()
        log.info("HL client ready mode=%s dexs=%s coins=%d", self.mode, self.perp_dexs, len(self.sz_decimals))

    def _setup_live(self) -> None:
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        key = os.environ.get("HL_AGENT_KEY")
        acct = os.environ.get("HL_ACCOUNT")
        if not key or not acct:
            raise RuntimeError("live mode needs HL_AGENT_KEY (API wallet private key) and HL_ACCOUNT (master address) in env")
        wallet = Account.from_key(key)
        self._exchange = Exchange(wallet, self.base_url, account_address=acct, perp_dexs=self.perp_dexs, timeout=10)
        self.account_address = acct

    # ------------------------------------------------------------------ info
    async def info_post(self, body: dict) -> Any:
        import urllib.request

        def _do():
            req = urllib.request.Request(self.base_url + "/info", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    async def refresh_mids(self) -> None:
        mids: dict[str, float] = {}
        for dex in self.perp_dexs:
            body = {"type": "allMids"} if dex == "" else {"type": "allMids", "dex": dex}
            try:
                d = await self.info_post(body)
                mids.update({k: float(v) for k, v in d.items()})
            except Exception as e:  # noqa: BLE001
                log.warning("allMids failed dex=%r: %s", dex, e)
            if len(self.perp_dexs) > 1:
                await asyncio.sleep(0.6)
        if mids:
            self.mids = mids
            self.mids_ms = now_ms()

    async def mids_loop(self, every_s: float) -> None:
        while True:
            try:
                await self.refresh_mids()
            except Exception as e:  # noqa: BLE001
                log.warning("mids loop: %s", e)
            await asyncio.sleep(every_s)

    def mid(self, coin: str) -> float | None:
        return self.mids.get(coin)

    def taker_fee(self, coin: str) -> float:
        if ":" in coin:
            return TAKER_FEE_HIP3_GROWTH if self.growth_mode.get(coin) else TAKER_FEE_HIP3_STD
        return TAKER_FEE_MAIN

    async def l2_slippage_bps(self, coin: str, notional_usd: float, is_buy: bool) -> float:
        """Walk the visible book for an estimate of market impact; falls back to a flat 2 bps."""
        try:
            book = await self.info_post({"type": "l2Book", "coin": coin})
            side = book["levels"][1] if is_buy else book["levels"][0]
            mid = self.mid(coin) or float(side[0]["px"])
            remaining = notional_usd
            cost = 0.0
            filled = 0.0
            for lv in side:
                px, sz = float(lv["px"]), float(lv["sz"])
                take = min(remaining, px * sz)
                cost += take
                filled += take / px
                remaining -= take
                if remaining <= 0:
                    break
            if filled <= 0 or remaining > 0:
                return 25.0  # not enough visible depth -> treat as expensive
            avg = cost / filled
            return abs(avg / mid - 1) * 1e4
        except Exception:  # noqa: BLE001
            return 2.0

    # ------------------------------------------------------------------ account
    async def equity_usd(self) -> float | None:
        """Account value. Standard abstraction mode: each perp dex has its own balance, so sum them.
        Unified account mode: per-dex states are not meaningful; the main clearinghouseState already covers everything."""
        if self.mode != "live":
            return None
        unified = bool(self.cfg.get("unified_account", False))
        dexs = [""] if unified else self.perp_dexs
        total = 0.0
        for dex in dexs:
            body = {"type": "clearinghouseState", "user": self.account_address}
            if dex:
                body["dex"] = dex
            st = await self.info_post(body)
            total += float(st["marginSummary"]["accountValue"])
            await asyncio.sleep(0.6)
        return total

    # ------------------------------------------------------------------ orders
    async def open_position(self, pos: Position) -> Position:
        """Fill `pos` (size/entry are provisional). Returns the position with real entry/fees."""
        is_buy = pos.direction is Direction.LONG
        szd = self.sz_decimals.get(pos.coin, 3)
        pos.size = _round_sz(pos.size, szd)
        if pos.size <= 0:
            raise ValueError(f"size rounds to zero for {pos.coin}")
        mid = self.mid(pos.coin)
        if mid is None:
            raise RuntimeError(f"no mid for {pos.coin}")
        if self.mode == "paper":
            slip_bps = await self.l2_slippage_bps(pos.coin, pos.size * mid, is_buy)
            fill = mid * (1 + slip_bps / 1e4) if is_buy else mid * (1 - slip_bps / 1e4)
            pos.entry_px = fill
            pos.slippage_bps = slip_bps
            pos.fee_usd = pos.size * fill * self.taker_fee(pos.coin)
            pos.entry_ms = now_ms()
            pos.notional_usd = pos.size * fill
            return pos
        # live: IOC limit at mid * (1 +/- slippage)
        limit_px = _round_px(mid * (1 + self.slippage) if is_buy else mid * (1 - self.slippage), szd)
        loop = asyncio.get_running_loop()
        async with self._lock:
            # Exchange-side leverage setting is the *margin* leverage, not the position's share of equity.
            # Use the template cap (pos.leverage carries max_lev from the risk gate) so two positions can coexist;
            # HIP-3 names that are onlyIsolated must be isolated, BTC/ETH use cross so margin is shared.
            lev = max(1, min(int(round(pos.leverage)), self.max_lev.get(pos.coin, 1)))
            is_cross = ":" not in pos.coin and not self.only_isolated.get(pos.coin, False)
            await loop.run_in_executor(None, lambda: self._exchange.update_leverage(lev, pos.coin, is_cross))
            res = await loop.run_in_executor(
                None, lambda: self._exchange.order(pos.coin, is_buy, pos.size, limit_px, {"limit": {"tif": "Ioc"}}, reduce_only=False)
            )
        st = res["response"]["data"]["statuses"][0]
        if "filled" not in st:
            raise RuntimeError(f"order not filled: {st}")
        pos.size = float(st["filled"]["totalSz"])
        pos.entry_px = float(st["filled"]["avgPx"])
        pos.entry_ms = now_ms()
        pos.notional_usd = pos.size * pos.entry_px
        pos.slippage_bps = abs(pos.entry_px / mid - 1) * 1e4
        pos.fee_usd = pos.notional_usd * self.taker_fee(pos.coin)
        # exchange-side stop (reduce-only trigger, market on trigger). Time stop/TP are managed by the engine.
        stop_px = _round_px(pos.stop_px, szd)
        async with self._lock:
            await loop.run_in_executor(
                None,
                lambda: self._exchange.order(
                    pos.coin, not is_buy, pos.size, stop_px,
                    {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True,
                ),
            )
        return pos

    async def close_position(self, pos: Position, reason: str) -> Position:
        is_buy = pos.direction is Direction.SHORT  # closing a short = buying
        mid = self.mid(pos.coin) or pos.entry_px
        if self.mode == "paper":
            slip_bps = await self.l2_slippage_bps(pos.coin, pos.size * mid, is_buy)
            fill = mid * (1 + slip_bps / 1e4) if is_buy else mid * (1 - slip_bps / 1e4)
            pos.exit_px = fill
        else:
            loop = asyncio.get_running_loop()
            async with self._lock:
                # cancel resting stop(s) for this coin first, then market_close (IOC, reduce-only)
                try:
                    oo = await loop.run_in_executor(None, lambda: self._info.frontend_open_orders(self.account_address, self._dex_of(pos.coin)))
                    for o in oo:
                        if o.get("coin") == pos.coin:
                            await loop.run_in_executor(None, lambda oid=o["oid"]: self._exchange.cancel(pos.coin, oid))
                except Exception as e:  # noqa: BLE001
                    log.warning("cancel stops failed: %s", e)
                res = await loop.run_in_executor(None, lambda: self._exchange.market_close(pos.coin, pos.size, None, self.slippage))
            try:
                st = res["response"]["data"]["statuses"][0]
                pos.exit_px = float(st["filled"]["avgPx"])
            except Exception:  # noqa: BLE001
                pos.exit_px = mid
        pos.exit_ms = now_ms()
        pos.exit_reason = reason
        sign = 1.0 if pos.direction is Direction.LONG else -1.0
        gross = sign * (pos.exit_px - pos.entry_px) * pos.size
        pos.fee_usd += pos.size * pos.exit_px * self.taker_fee(pos.coin)
        pos.pnl_usd = gross - pos.fee_usd
        return pos

    def _dex_of(self, coin: str) -> str:
        return coin.split(":")[0] if ":" in coin else ""

    async def flatten_all(self, positions: list[Position], reason: str) -> list[Position]:
        out = []
        for p in positions:
            try:
                out.append(await self.close_position(p, reason))
            except Exception as e:  # noqa: BLE001
                log.error("flatten %s failed: %s", p.coin, e)
        return out
