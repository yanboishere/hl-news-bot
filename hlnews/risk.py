"""Risk gate. Pure function of (signal, template, account state, time) -> accept/reject + sizing."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import Direction, Position, Signal


@dataclass
class AccountState:
    equity_usd: float
    day_start_equity: float
    week_start_equity: float
    open: list[Position] = field(default_factory=list)
    last_loss_ms: int = 0
    last_trade_ms_by_coin: dict[str, int] = field(default_factory=dict)
    halted_reason: str | None = None

    @property
    def gross_notional(self) -> float:
        return sum(p.notional_usd for p in self.open)


@dataclass
class Decision:
    accepted: bool
    reason: str
    size: float = 0.0
    notional_usd: float = 0.0
    leverage: float = 0.0        # exchange margin leverage to set (template cap), not notional/equity
    stop_px: float = 0.0
    tp_px: float | None = None
    time_exit_ms: int = 0
    equity_ratio: float = 0.0    # notional / equity, for logging


class RiskGate:
    def __init__(self, acct_cfg: dict, max_lev_by_coin: dict[str, int]):
        self.c = acct_cfg
        self.max_lev_by_coin = max_lev_by_coin

    def check(self, sig: Signal, mid: float, st: AccountState, slippage_bps: float, taker_fee: float, now_ms: int | None = None) -> Decision:
        now = now_ms or int(time.time() * 1000)
        t = sig.template
        if st.halted_reason:
            return Decision(False, f"halted: {st.halted_reason}")
        if sig.direction is Direction.NONE:
            return Decision(False, "no direction")
        if sig.confidence < float(t.get("min_conf", 0.8)):
            return Decision(False, f"confidence {sig.confidence:.2f} < {t.get('min_conf')}")
        # loss limits
        day_loss = (st.day_start_equity - st.equity_usd) / max(st.day_start_equity, 1e-9) * 100
        if day_loss >= float(self.c["daily_loss_limit_pct"]):
            return Decision(False, f"daily loss limit hit ({day_loss:.1f}%)")
        week_loss = (st.week_start_equity - st.equity_usd) / max(st.week_start_equity, 1e-9) * 100
        if week_loss >= float(self.c["weekly_loss_limit_pct"]):
            return Decision(False, f"weekly loss limit hit ({week_loss:.1f}%)")
        if st.last_loss_ms and now - st.last_loss_ms < int(self.c["cooldown_after_loss_s"]) * 1000:
            return Decision(False, "cooldown after loss")
        lt = st.last_trade_ms_by_coin.get(sig.coin, 0)
        if lt and now - lt < int(self.c["per_coin_cooldown_s"]) * 1000:
            return Decision(False, f"per-coin cooldown {sig.coin}")
        if len(st.open) >= int(self.c["max_open_positions"]):
            return Decision(False, "max open positions")
        if any(p.coin == sig.coin for p in st.open):
            return Decision(False, f"already in {sig.coin}")
        # cost sanity: round-trip fee + slippage must be a small fraction of the stop distance
        stop_pct = float(t["stop_pct"])
        rt_cost_pct = (2 * taker_fee * 100) + (2 * slippage_bps / 100)
        if rt_cost_pct > 0.35 * stop_pct:
            return Decision(False, f"cost {rt_cost_pct:.2f}% too large vs stop {stop_pct:.2f}%")
        # sizing: risk budget / stop distance
        risk_pct = min(float(t.get("risk_pct", 0.5)), float(self.c["max_risk_per_trade_pct"]))
        risk_usd = st.equity_usd * risk_pct / 100
        notional = risk_usd / (stop_pct / 100)
        notional = min(notional, float(self.c["max_notional_per_trade_usd"]))
        # leverage caps: template, coin, gross
        lev_cap = min(float(t.get("max_lev", 2)), float(self.max_lev_by_coin.get(sig.coin, 1)))
        notional = min(notional, st.equity_usd * lev_cap)
        room = st.equity_usd * float(self.c["max_gross_leverage"]) - st.gross_notional
        notional = min(notional, max(room, 0.0))
        if notional < 12:  # HL minimum order value is $10
            return Decision(False, f"notional {notional:.0f} below minimum")
        size = notional / mid
        lev = notional / st.equity_usd
        sign = 1 if sig.direction is Direction.LONG else -1
        stop_px = mid * (1 - sign * stop_pct / 100)
        tp_px = mid * (1 + sign * float(t["tp_pct"]) / 100) if t.get("tp_pct") else None
        return Decision(True, "ok", size=size, notional_usd=notional, leverage=lev_cap, stop_px=stop_px, tp_px=tp_px,
                        time_exit_ms=now + int(t["hold_s"]) * 1000, equity_ratio=max(lev, 0.0))
