"""Keep-alive supervisor (ops/memoh/supervisor.py) against tests/fake_bot.py, with second-scale timings."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hlnews import instance  # noqa: E402

SUP = os.path.join(ROOT, "ops", "memoh", "supervisor.py")
FAKE = os.path.join(ROOT, "tests", "fake_bot.py")
FAST = ["--tick", "0.1", "--stale-s", "1.0", "--grace-s", "1.5", "--backoff-min", "0.2", "--backoff-max", "1.6",
        "--healthy-reset-s", "60", "--stop-timeout-s", "2", "--no-telegram"]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and flock")


def wait_for(cond, timeout: float = 10.0, step: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:   # a zombie (exited, not yet reaped by its parent) counts as gone
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return True


class Deploy:
    def __init__(self, path):
        self.path = path
        self.data = os.path.join(path, "data")
        os.makedirs(self.data, exist_ok=True)
        self.procs: list[subprocess.Popen] = []

    def file(self, name: str) -> str:
        return os.path.join(self.data, name)

    def write(self, name: str, text: str) -> None:
        with open(self.file(name), "w", encoding="utf-8") as f:
            f.write(text)

    def env_file(self, text: str) -> None:
        with open(os.path.join(self.path, ".env"), "w", encoding="utf-8") as f:
            f.write(text)

    def starts(self) -> list[list[str]]:
        try:
            with open(self.file("fake_starts"), encoding="utf-8") as f:
                return [ln.split(" ") for ln in f.read().splitlines()]
        except OSError:
            return []

    def beat_pid(self) -> int | None:
        try:
            with open(self.file("state.json"), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return None
        return st.get("pid") if not st.get("stopped") and time.time() * 1000 - st.get("ts_ms", 0) < 800 else None

    def status(self) -> dict:
        try:
            with open(self.file("supervisor.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def log(self) -> str:
        try:
            with open(self.file("supervisor.log"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def env(self) -> dict:
        e = dict(os.environ, HLNEWS_CODE=ROOT)
        e.pop("OPENNEWS_TOKEN", None)
        return e

    def supervisor(self, *extra: str) -> subprocess.Popen:
        cmd = [sys.executable, SUP, "--repo", self.path, "--python", sys.executable, *FAST,
               "--bot-cmd", json.dumps([sys.executable, FAKE]), *extra]
        p = subprocess.Popen(cmd, env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, start_new_session=True)
        self.procs.append(p)
        return p

    def bot(self) -> subprocess.Popen:
        """A bot started outside the supervisor (shell run, orphan)."""
        p = subprocess.Popen([sys.executable, FAKE], cwd=self.path, env=self.env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        return p

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        for row in self.starts():   # anything the supervisor started and did not stop
            pid = int(row[0])
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def dep(tmp_path):
    d = Deploy(str(tmp_path))
    yield d
    d.cleanup()


def test_starts_the_bot_and_restarts_it_after_a_kill(dep):
    dep.supervisor()
    p1 = wait_for(dep.beat_pid)
    assert p1 and instance.is_locked(dep.data) and instance.holder_pid(dep.data) == p1
    os.kill(p1, signal.SIGKILL)
    p2 = wait_for(lambda: (dep.beat_pid() or 0) not in (0, p1) and dep.beat_pid())
    assert p2 and p2 != p1
    st = dep.status()
    assert st["restarts"] >= 1 and st["last_exit"] == -signal.SIGKILL and st["bot_pid"] == p2
    assert instance.is_locked(dep.data, instance.SUPERVISOR_LOCK)


def test_hung_bot_is_restarted(dep):
    dep.write("fake_mode", "hang")
    dep.supervisor()
    p1 = wait_for(dep.beat_pid)
    assert p1
    dep.write("fake_mode", "ok")   # the replacement is healthy
    p2 = wait_for(lambda: (dep.beat_pid() or 0) not in (0, p1) and dep.beat_pid(), timeout=12)
    assert p2 and not alive(p1)
    assert "looks hung" in dep.log()


def test_stale_heartbeat_counts_before_the_startup_grace_ends(dep):
    """Grace is for a bot that has not beaten yet; one that beat and then froze is hung right away."""
    dep.write("fake_mode", "hang")
    dep.supervisor("--grace-s", "60")
    p1 = wait_for(dep.beat_pid)
    assert p1
    dep.write("fake_mode", "ok")
    p2 = wait_for(lambda: (dep.beat_pid() or 0) not in (0, p1) and dep.beat_pid(), timeout=12)
    assert p2, dep.log()


def test_hung_bot_that_ignores_sigint_is_killed(dep):
    dep.write("fake_mode", "hang-hard")
    dep.supervisor()
    p1 = wait_for(dep.beat_pid)
    assert p1
    dep.write("fake_mode", "ok")
    p2 = wait_for(lambda: (dep.beat_pid() or 0) not in (0, p1) and dep.beat_pid(), timeout=15)
    assert p2 and not alive(p1)


def test_leftover_stopped_heartbeat_does_not_hide_a_silent_bot(dep):
    """state.json from the previous run says stopped=true; the new bot never beats. It must still count as hung."""
    dep.write("state.json", json.dumps({"ts_ms": int(time.time() * 1000), "pid": 999999, "stopped": True, "open": []}))
    dep.write("fake_mode", "silent")
    dep.supervisor()
    assert wait_for(lambda: len(dep.starts()) >= 2, timeout=12), dep.log()
    assert "looks hung" in dep.log()


def test_crash_loop_backs_off(dep):
    dep.write("fake_mode", "crash")
    dep.supervisor()
    assert wait_for(lambda: dep.status().get("backoff_s") == 1.6, timeout=15), dep.status()
    n = len(dep.starts())
    time.sleep(1.0)
    assert len(dep.starts()) - n <= 1    # at most one start per 1.6 s once backed off
    dep.write("fake_mode", "ok")
    assert wait_for(dep.beat_pid, timeout=10)


def test_watches_a_running_bot_instead_of_starting_a_second(dep):
    orphan = dep.bot()
    assert wait_for(lambda: dep.beat_pid() == orphan.pid)
    dep.supervisor()
    assert wait_for(lambda: dep.status().get("adopted") and dep.status().get("bot_pid") == orphan.pid)
    time.sleep(0.5)
    assert len(dep.starts()) == 1
    orphan.send_signal(signal.SIGINT)
    orphan.wait(timeout=10)
    own = wait_for(lambda: (dep.beat_pid() or 0) not in (0, orphan.pid) and dep.beat_pid())
    assert own and len(dep.starts()) == 2 and not dep.status().get("adopted")


def test_env_change_restarts_only_when_flat(dep):
    dep.env_file("OPENNEWS_TOKEN=tok-a\n")
    dep.supervisor()
    p1 = wait_for(dep.beat_pid)
    assert p1 and dep.starts()[-1] == [str(p1), "tok-a"]
    dep.write("fake_open", json.dumps([{"coin": "BTC"}]))
    time.sleep(0.3)
    dep.env_file("OPENNEWS_TOKEN=tok-b\n")
    assert wait_for(lambda: dep.status().get("env_pending"), timeout=5)
    time.sleep(0.5)
    assert dep.beat_pid() == p1          # a position is open: no restart yet
    dep.write("fake_open", "[]")
    p2 = wait_for(lambda: (dep.beat_pid() or 0) not in (0, p1) and dep.beat_pid())
    assert p2 and dep.starts()[-1] == [str(p2), "tok-b"]
    assert not dep.status().get("env_pending")


def test_sigterm_stops_the_bot_gracefully(dep):
    sup = dep.supervisor()
    p1 = wait_for(dep.beat_pid)
    assert p1
    sup.send_signal(signal.SIGTERM)
    assert sup.wait(timeout=10) == 0
    assert not alive(p1)
    with open(dep.file("state.json"), encoding="utf-8") as f:
        assert json.load(f)["stopped"] is True    # the bot ran its shutdown path (SIGINT), not a SIGKILL
    assert not instance.is_locked(dep.data) and not instance.is_locked(dep.data, instance.SUPERVISOR_LOCK)


def test_second_supervisor_refuses(dep):
    dep.supervisor()
    assert wait_for(dep.beat_pid)
    second = dep.supervisor()
    assert second.wait(timeout=10) == instance.EXIT_ALREADY_RUNNING
    assert b"already running" in second.stderr.read()
    assert len(dep.starts()) == 1


def test_watched_bot_that_hangs_is_stopped_and_replaced(dep):
    dep.write("fake_mode", "hang-hard")       # beats for 0.6 s, then freezes and ignores SIGINT
    orphan = dep.bot()
    assert wait_for(lambda: dep.beat_pid() == orphan.pid)
    dep.supervisor()
    assert wait_for(lambda: dep.status().get("adopted"))
    dep.write("fake_mode", "ok")
    own = wait_for(lambda: (dep.beat_pid() or 0) not in (0, orphan.pid) and dep.beat_pid(), timeout=15)
    assert own, dep.log()
    assert orphan.wait(timeout=5) == -signal.SIGKILL     # SIGINT was ignored, so it took the SIGKILL
    assert "watched bot pid" in dep.log() and "no fresh heartbeat" in dep.log()
