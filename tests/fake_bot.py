"""Stand-in for `python -m hlnews.run` in the keep-alive tests: takes the instance lock and writes the heartbeat.

It runs with the deployment directory as cwd (like the real bot). Behaviour comes from files under ./data, so a test
can change it between restarts:

  data/fake_mode   ok (default) | crash (exit 1 at once) | hang (heartbeat stops after FAKE_HANG_AFTER s, still
                   handles SIGINT) | hang-hard (heartbeat stops, SIGINT/SIGTERM ignored) | silent (never beats)
  data/fake_open   JSON list reported as open positions

Each start appends "<pid> <OPENNEWS_TOKEN>" to data/fake_starts. On SIGINT/SIGTERM it writes a final heartbeat with
stopped=true (as engine.run does) and exits 0.
"""
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.environ.get("HLNEWS_CODE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hlnews import instance  # noqa: E402


def read(name: str, default: str = "") -> str:
    try:
        with open(os.path.join("data", name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


mode = read("fake_mode", "ok") or "ok"
with open(os.path.join("data", "fake_starts"), "a", encoding="utf-8") as f:
    f.write(f"{os.getpid()} {os.environ.get('OPENNEWS_TOKEN', '')}\n")
if mode == "crash":
    sys.exit(1)
lock = instance.acquire("data")
stop = False


def on_signal(signum, frame):  # noqa: ANN001, ARG001
    global stop
    stop = True


if mode == "hang-hard":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)


def beat(stopped: bool = False) -> None:
    try:
        open_pos = json.loads(read("fake_open", "[]") or "[]")
    except ValueError:
        open_pos = []
    st = {"ts_ms": int(time.time() * 1000), "pid": os.getpid(), "stopped": stopped, "mode": "paper",
          "equity_usd": 10000.0, "open": open_pos, "mids": {}, "last_news_ms": None, "halted_reason": None}
    tmp = os.path.join("data", f"state.json.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, os.path.join("data", "state.json"))


t0 = time.time()
hang_after = float(os.environ.get("FAKE_HANG_AFTER", "0.6"))
while not stop:
    if mode == "silent" or (mode in ("hang", "hang-hard") and time.time() - t0 > hang_after):
        time.sleep(0.05)
        continue
    beat()
    time.sleep(0.1)
beat(stopped=True)
sys.exit(0)
