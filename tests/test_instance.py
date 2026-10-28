"""One bot per data directory: hlnews/instance.py."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hlnews import instance  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")

HOLDER = "import sys,time; sys.path.insert(0, sys.argv[1]); from hlnews import instance; h = instance.acquire(sys.argv[2]); print('locked', flush=True); time.sleep(30)"
SECOND = "import sys; sys.path.insert(0, sys.argv[1]); from hlnews import instance; instance.acquire(sys.argv[2]); print('got it')"


def test_lock_is_exclusive_and_released_on_kill(tmp_path):
    data = str(tmp_path / "data")
    assert not instance.is_locked(data)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, ROOT, data], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert instance.is_locked(data)
        assert instance.holder_pid(data) == holder.pid
        second = subprocess.run([sys.executable, "-c", SECOND, ROOT, data], capture_output=True, text=True, timeout=20)
        assert second.returncode == instance.EXIT_ALREADY_RUNNING
        assert "already running" in second.stderr and str(holder.pid) in second.stderr
        assert "got it" not in second.stdout
    finally:
        holder.kill()   # SIGKILL: the kernel still drops the flock
        holder.wait()
    assert not instance.is_locked(data)
    third = subprocess.run([sys.executable, "-c", SECOND, ROOT, data], capture_output=True, text=True, timeout=20)
    assert third.returncode == 0 and "got it" in third.stdout


def test_probe_does_not_block_a_starting_bot(tmp_path):
    """The console polls is_locked every 2 s; a bot starting at that instant must still get the lock."""
    data = str(tmp_path / "data")
    os.makedirs(data)
    stop = time.time() + 1.5
    prober = subprocess.Popen([sys.executable, "-c",
                               "import sys,time; sys.path.insert(0, sys.argv[1]); from hlnews import instance\n"
                               "end = time.time() + float(sys.argv[3])\n"
                               "while time.time() < end: instance.is_locked(sys.argv[2])", ROOT, data, "1.5"])
    try:
        while time.time() < stop:
            r = subprocess.run([sys.executable, "-c", SECOND, ROOT, data], capture_output=True, text=True, timeout=20)
            assert r.returncode == 0, r.stderr
    finally:
        prober.wait(timeout=10)


def test_other_lock_names(tmp_path):
    data = str(tmp_path)
    assert not instance.is_locked(data, instance.SUPERVISOR_LOCK)
    assert instance.lock_path(data, instance.SUPERVISOR_LOCK).endswith("supervisor.lock")
