"""Entry point.

  python -m hlnews.run                       # paper mode, live feeds (Tree of Alpha + HL listings)
  python -m hlnews.run --replay data/replay.jsonl --speed 0   # replay a JSONL of headlines through the full pipeline
  python -m hlnews.run --summary             # print P&L summary from the SQLite store

Live mode: set mode: live in config.yaml and export HL_AGENT_KEY / HL_ACCOUNT. Never put the master key here.
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
from .engine import Engine
from .hl import HLClient
from .store import Store


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
    else:
        if fc["treeofalpha"]["enabled"]:
            feed_list.append(F.treeofalpha_feed(fc["treeofalpha"]["ws_url"], fc["treeofalpha"].get("api_key", ""), float(fc["treeofalpha"].get("max_age_s", 20))))
        if fc["hl_listings"]["enabled"]:
            feed_list.append(F.hl_listings_feed(hl.info_post, poll_s=float(fc["hl_listings"].get("poll_s", 5)), dexs=tuple(cfg["hyperliquid"]["perp_dexs"])))
        if fc["telegram"]["enabled"]:
            tg = fc["telegram"]
            feed_list.append(F.telegram_feed(int(tg["api_id"]), tg["api_hash"], tg["session"], list(tg["channels"])))
    engine = Engine(cfg, store, hl, feed_list)

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
    setup_logging(cfg["storage"]["log_path"])
    asyncio.run(main_async(cfg, args))


if __name__ == "__main__":
    main()
