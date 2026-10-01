"""Event-study backtester for the templates in config.yaml.

For each historical event (timestamp, coin, direction the bot *would* have taken), pull 1-minute candles from Binance,
enter at the first candle open after `entry_delay_s`, then walk candles applying stop / take-profit / time-stop with
fees and slippage. Prints per-class and aggregate statistics plus the required win rate for break-even.

Usage:
  python -m hlnews.backtest --events data/events.jsonl --delay 20 --config config.yaml
  python -m hlnews.backtest --events data/events.jsonl --delay 20 --sweep      # delay sweep 5..300 s

Event file: one JSON per line {ts_utc:"2025-02-21T15:20:00Z", coin:"ETH", direction:"short", event_class:"hack_exploit", note:"..."}
Binance symbol mapping: BTC->BTCUSDT, ETH->ETHUSDT, xyz:TSLA etc are skipped (no free 1m history for the perp) unless
a `symbol` field is given.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

import yaml

BINANCE = "https://api.binance.com/api/v3/klines"
FEE_RT = 2 * 0.00045  # taker both sides, base tier
SLIP_RT = 2 * 0.0002  # 2 bps each side, measured normal-condition HL BTC/ETH impact for ~$1k-$100k


def klines(symbol: str, start_ms: int, limit: int) -> list[list]:
    url = f"{BINANCE}?symbol={symbol}&interval=1m&startTime={start_ms}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return []


def simulate(k: list[list], side: int, entry_delay_s: int, stop_pct: float, tp_pct: float | None, hold_s: int, t0_ms: int) -> dict:
    """side=+1 long / -1 short. Returns dict(ret_pct, exit_reason, bars)."""
    entry_ms = t0_ms + entry_delay_s * 1000
    # entry = open of first candle whose open time >= entry_ms, adjusted by intra-minute linear position (approx)
    idx = next((i for i, c in enumerate(k) if c[0] >= entry_ms - 59_999), None)
    if idx is None:
        return {"ret_pct": 0.0, "exit_reason": "no_data", "bars": 0}
    c = k[idx]
    o, cl = float(c[1]), float(c[4])
    frac = min(max((entry_ms - c[0]) / 60_000, 0.0), 1.0)
    entry = o + (cl - o) * frac
    stop = entry * (1 - side * stop_pct / 100)
    tp = entry * (1 + side * tp_pct / 100) if tp_pct else None
    exit_px, reason, bars = None, "time_stop", 0
    deadline = entry_ms + hold_s * 1000
    for c in k[idx + 1 :]:
        bars += 1
        hi, lo, close = float(c[2]), float(c[3]), float(c[4])
        if side > 0:
            if lo <= stop:
                exit_px, reason = stop, "stop"
                break
            if tp and hi >= tp:
                exit_px, reason = tp, "take_profit"
                break
        else:
            if hi >= stop:
                exit_px, reason = stop, "stop"
                break
            if tp and lo <= tp:
                exit_px, reason = tp, "take_profit"
                break
        if c[0] + 60_000 >= deadline:
            exit_px = close
            break
    if exit_px is None:
        exit_px = float(k[-1][4])
        reason = "data_end"
    gross = side * (exit_px / entry - 1)
    return {"ret_pct": (gross - FEE_RT - SLIP_RT) * 100, "exit_reason": reason, "bars": bars, "entry": entry, "exit": exit_px}


def run(events: list[dict], templates: dict, delay_s: int, verbose: bool = True, rows_out: list[dict] | None = None) -> dict:
    by_class: dict[str, list[dict]] = {}
    for ev in events:
        coin = ev["coin"]
        sym = ev.get("symbol") or {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}.get(coin)
        if not sym:
            continue
        t = templates.get(ev["event_class"])
        if not t:
            continue
        t0 = int(datetime.fromisoformat(ev["ts_utc"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
        n_bars = int(t["hold_s"] / 60) + 8
        k = klines(sym, t0 - 60_000, min(n_bars, 1000))
        if not k:
            continue
        side = 1 if ev["direction"] == "long" else -1
        res = simulate(k, side, delay_s, float(t["stop_pct"]), t.get("tp_pct"), int(t["hold_s"]), t0)
        res.update({"coin": coin, "ts": ev["ts_utc"], "note": ev.get("note", ""), "direction": ev["direction"], "event_class": ev["event_class"]})
        by_class.setdefault(ev["event_class"], []).append(res)
        if rows_out is not None:
            rows_out.append(res)
        if verbose:
            print(f"{ev['ts_utc']} {ev['event_class']:14} {ev['direction']:5} {coin:4} -> {res['ret_pct']:+6.2f}%  {res['exit_reason']:11} {ev.get('note','')[:50]}")
        time.sleep(0.25)
    out = {}
    all_r = []
    for ec, rs in by_class.items():
        r = [x["ret_pct"] for x in rs]
        all_r += r
        wins = sum(1 for x in r if x > 0)
        t = templates[ec]
        be = float(t["stop_pct"]) / (float(t["stop_pct"]) + float(t.get("tp_pct") or t["stop_pct"]))
        out[ec] = {"n": len(r), "win_rate": wins / len(r), "mean_pct": statistics.mean(r), "median_pct": statistics.median(r),
                   "sum_R": sum(x / float(t["stop_pct"]) for x in r), "breakeven_win_rate_if_tp_or_stop": be,
                   "exits": {k: sum(1 for x in rs if x["exit_reason"] == k) for k in ("stop", "take_profit", "time_stop", "no_data", "data_end")}}
    if all_r:
        out["_all"] = {"n": len(all_r), "win_rate": sum(1 for x in all_r if x > 0) / len(all_r), "mean_pct": statistics.mean(all_r), "median_pct": statistics.median(all_r)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="data/events.jsonl")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--delay", type=int, default=20, help="seconds between event time and our entry")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    events = [json.loads(l) for l in open(args.events, encoding="utf-8") if l.strip() and not l.startswith("#")]
    if args.sweep:
        for d in (5, 20, 60, 180, 300):
            r = run(events, cfg["templates"], d, verbose=False)
            a = r.get("_all", {})
            print(f"delay {d:4d}s: n={a.get('n')} win={a.get('win_rate', 0):.0%} mean={a.get('mean_pct', 0):+.2f}% median={a.get('median_pct', 0):+.2f}%")
        return
    r = run(events, cfg["templates"], args.delay)
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    sys.exit(main())
