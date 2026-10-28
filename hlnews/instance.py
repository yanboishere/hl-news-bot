"""One bot per data directory.

Two `python -m hlnews.run` processes on the same data directory would trade twice, share the SQLite file and, in
live mode, collide on the API wallet's nonces. Whoever starts the bot (the console's start button, the Memoh
supervisor in ops/memoh, a shell) goes through this lock, so the second one refuses to start.

The lock is an flock on <data_dir>/hlnews.lock, held for the life of the process and released by the kernel when the
process exits, even on SIGKILL. The file also carries the holder's pid for messages and for ops/memoh/hlbot.
ops/memoh/supervisor.py holds <data_dir>/supervisor.lock the same way; `is_locked(data_dir, SUPERVISOR_LOCK)` probes it.
"""
from __future__ import annotations

import os
import sys
import time
from typing import IO

LOCK_NAME = "hlnews.lock"
SUPERVISOR_LOCK = "supervisor.lock"
EXIT_ALREADY_RUNNING = 3


def lock_path(data_dir: str, name: str = LOCK_NAME) -> str:
    return os.path.join(data_dir or ".", name)


def holder_pid(data_dir: str, name: str = LOCK_NAME) -> int | None:
    """pid written by the current (or last) lock holder, or None."""
    try:
        with open(lock_path(data_dir, name), encoding="utf-8") as f:
            s = f.read().strip()
        return int(s) if s.isdigit() else None
    except OSError:
        return None


def is_locked(data_dir: str, name: str = LOCK_NAME) -> bool:
    """True while some process holds the lock (non-blocking probe; the probe itself never keeps it)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return False
    path = lock_path(data_dir, name)
    if not os.path.exists(path):
        return False
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


def acquire(data_dir: str) -> IO[str] | None:
    """Take the instance lock or exit with EXIT_ALREADY_RUNNING. Keep the returned handle alive for the process."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows has no flock; single instance is not enforced there
        return None
    os.makedirs(data_dir or ".", exist_ok=True)
    fh = open(lock_path(data_dir), "a+", encoding="utf-8")
    for attempt in range(5):   # a status probe (is_locked) holds the lock for microseconds; do not mistake it for a bot
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if attempt < 4:
                time.sleep(0.1)
    else:
        fh.seek(0)
        other = fh.read().strip() or "?"
        fh.close()
        print(f"hl-news-bot is already running from {os.path.abspath(data_dir or '.')} (pid {other}); "
              f"refusing to start a second instance", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_ALREADY_RUNNING)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh
