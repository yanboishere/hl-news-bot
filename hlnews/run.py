"""Entry point.

  python -m hlnews.run                       # paper mode, live feeds (NewsLiquid + HL listings + BLS macro)
  python -m hlnews.run --replay data/replay.jsonl --speed 0   # replay NewsLiquid records through the full pipeline
  python -m hlnews.run --summary             # print P&L summary from the SQLite store

News needs a NewsLiquid token: export OPENNEWS_TOKEN (https://6551.io/mcp). Live trading additionally needs
mode: live in config.yaml and HL_AGENT_KEY / HL_ACCOUNT in the environment. Never put the master key here.

Only one bot runs per data directory (hlnews/instance.py); a second start exits with code 3. On a Memoh cloud
computer, start and keep it alive with ops/memoh/hlbot instead of running this directly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

import yaml

from . import feeds as F
from . import instance
from .engine import Engine
from .hl import HLClient
from .newsliquid import newsliquid_feed
from .store import Store

# feed health -> alerts. Only transitions are pushed: one alert when a problem starts, one when it clears.
_NL_PROBLEM = {"ws_down": "WebSocket 断开", "ws_refused": "WebSocket 不可用（未开通实时推送或 token 无效），改用 REST 轮询",
               "rest_fail": "REST 连续失败", "auth_fail": "token 缺失或无效", "quota": "额度用完或被限流"}
_NL_RECOVER = {"ws_up": "WebSocket 已连接", "rest_ok": "REST 轮询恢复"}


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")])


async def main_async(cfg: dict, args: argparse.Namespace) -> None:
    store = Store(cfg["storage"]["sqlite_path"])
    if args.summary:
        print(json.dumps(store.summary(), indent=2, ensure_ascii=False))
        return
    mode = cfg.get("mode", "paper")
    if args.paper:
        mode = "paper"
    cfg["mode"] = mode
    if mode == "live" and args.replay:
        raise SystemExit("refusing to replay into live mode")
    hl = HLClient(cfg["hyperliquid"], mode)
    await hl.start()

    feed_list = []
    fc = cfg["feeds"]
    if args.replay:
        feed_list.append(F.replay_feed(args.replay, speed=args.speed))
    elif (fc.get("hl_listings") or {}).get("enabled"):
        feed_list.append(F.hl_listings_feed(hl.info_post, poll_s=float(fc["hl_listings"].get("poll_s", 5)), dexs=tuple(cfg["hyperliquid"]["perp_dexs"])))
    engine = Engine(cfg, store, hl, feed_list)
    nlc = fc.get("newsliquid") or {}
    if not args.replay and nlc.get("enabled", True):
        async def _nl_health(status: str, detail: str) -> None:
            down = engine.feed_was_down
            if status in _NL_PROBLEM:
                if down.get(f"newsliquid:{status}"):
                    return          # already reported, wait for recovery
                down[f"newsliquid:{status}"] = True
                down["newsliquid"] = True
                await engine.alerter.send("feed", f"NewsLiquid：{_NL_PROBLEM[status]}", detail, {"source": "newsliquid", "status": status})
            elif status in _NL_RECOVER and down.pop("newsliquid", False):
                for k in [k for k in down if k.startswith("newsliquid:")]:
                    down.pop(k, None)
                await engine.alerter.send("feed", f"NewsLiquid：{_NL_RECOVER[status]}", detail, {"source": "newsliquid", "status": status})
        engine.add_feed(newsliquid_feed(nlc, on_health=_nl_health))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass
    runner = asyncio.create_task(engine.run())
    if args.replay and args.duration <= 0:
        args.duration = 20  # give the replay a short window for time stops to resolve
    if args.duration > 0:
        async def _timer():
            await asyncio.sleep(args.duration)
            stop.set()
        asyncio.create_task(_timer())
    await stop.wait()
    runner.cancel()
    try:
        await runner
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await engine.shutdown()
    await asyncio.sleep(0.3)  # let cancelled transports finish closing before the loop goes away
    print(json.dumps(store.summary(), indent=2, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--replay", default=None, help="JSONL file of headlines to replay through the pipeline")
    ap.add_argument("--speed", type=float, default=0.0, help="replay speed; 0 = as fast as possible, 1 = real time")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds to run, 0 = until Ctrl-C")
    ap.add_argument("--paper", action="store_true", help="force paper mode regardless of config")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    cfg = load_cfg(args.config)
    # one bot per data directory: a second start (console button, supervisor, shell) exits with code 3
    instance_lock = None if args.summary else instance.acquire(os.path.dirname(cfg["storage"]["sqlite_path"]) or ".")
    setup_logging(cfg["storage"]["log_path"])
    try:
        asyncio.run(main_async(cfg, args))
    finally:
        if instance_lock is not None:
            instance_lock.close()


if __name__ == "__main__":
    main()
