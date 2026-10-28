#!/usr/bin/env python3
"""Keep hl-news-bot running on a Memoh cloud computer (or any Linux/macOS box).

    python ops/memoh/supervisor.py --repo /data/hl-news-bot [--live] [--with-ui]

Normally started and stopped through ops/memoh/hlbot (ctl.py), not by hand. Every few seconds it:

  - starts `python -m hlnews.run --paper` (without --paper only when --live is given; config.yaml must then also say
    mode: live) and restarts it whenever it exits; quick or repeated crashes back off from 5 s up to 5 min
  - kills and restarts a hung bot: data/state.json is the bot's 2-second heartbeat, and a heartbeat older than
    --stale-s (or none at all --grace-s after start) means the process is stuck
  - watches a bot that is already running from this data directory (hlnews.lock held: an orphan of a killed
    supervisor, a manual run) instead of starting a second one, and takes over when it exits
  - keeps the local console (python -m hlnews.ui on 127.0.0.1:--ui-port) alive when --with-ui is given
  - restarts the bot when .env changes (a new OPENNEWS_TOKEN takes effect on its own), but only while no position
    is open; otherwise the change waits until the bot is flat
  - rotates data/bot.out, data/hlnews.log and data/ui.out (copy-truncate; the writers keep their file handles)
  - pushes restarts, hangs and crash loops to Telegram when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are in .env
  - holds data/supervisor.lock (one supervisor per directory) and writes data/supervisor.json for `hlbot status`

SIGTERM / SIGINT / SIGHUP stop it: the bot gets SIGINT (it flattens open positions and exits), then the console.
Standard library only; it runs under the repo's .venv python so the bot inherits that interpreter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from hlnews import instance  # noqa: E402  (stdlib-only module)

EXIT_ALREADY_RUNNING = instance.EXIT_ALREADY_RUNNING
STATUS_FILE = "supervisor.json"


def now() -> float:
    return time.time()


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def read_dotenv(path: str) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments and blank lines skipped; optional quotes; a leading `export ` is allowed."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


def file_digest(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_heartbeat(data_dir: str, pid: int | None = None) -> tuple[float | None, dict]:
    """(age in seconds, content) of data/state.json; age is None when the file is missing, unreadable, or was written
    by a different process than `pid` (a leftover from an earlier run must not count as this bot's heartbeat)."""
    try:
        with open(os.path.join(data_dir, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None, {}
    if not isinstance(st, dict):
        return None, {}
    if pid and st.get("pid") != pid:
        return None, {}
    try:
        return max(0.0, now() - float(st.get("ts_ms", 0)) / 1000.0), st
    except (TypeError, ValueError):
        return None, {}


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def copytruncate(path: str, max_bytes: int, keep: int) -> bool:
    """Rotate a file another process keeps open in append mode: copy to .1 (shifting older copies), then truncate."""
    try:
        if os.path.getsize(path) < max_bytes:
            return False
        for i in range(keep - 1, 0, -1):
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        shutil.copyfile(path, f"{path}.1")
        with open(path, "r+b") as f:
            f.truncate(0)
        return True
    except OSError:
        return False


class Supervisor:
    def __init__(self, a: argparse.Namespace):
        self.repo = os.path.abspath(a.repo)
        self.python = a.python
        self.live = a.live
        self.with_ui = a.with_ui
        self.ui_port = a.ui_port
        self.env_watch = not a.no_env_watch
        self.telegram = not a.no_telegram
        self.stale_s = a.stale_s
        self.grace_s = a.grace_s
        self.tick = a.tick
        self.backoff_min = a.backoff_min
        self.backoff_max = a.backoff_max
        self.healthy_reset_s = a.healthy_reset_s
        self.stop_timeout_s = a.stop_timeout_s
        self.bot_cmd = json.loads(a.bot_cmd) if a.bot_cmd else None   # tests replace the bot command
        self.ui_cmd = json.loads(a.ui_cmd) if a.ui_cmd else None
        self.data = os.path.join(self.repo, "data")
        os.makedirs(self.data, exist_ok=True)
        self.env_path = os.path.join(self.repo, ".env")
        self.log_path = os.path.join(self.data, "supervisor.log")
        self.lock_fh = None
        self.stopping = False
        self.stop_signal = 0
        # bot
        self.bot: subprocess.Popen | None = None
        self.bot_started = 0.0
        self.bot_env_digest = ""
        self.env_pending = False
        self.backoff = self.backoff_min
        self.next_start = 0.0
        self.quick_failures = 0
        self.exits: list[float] = []
        self.restarts = 0
        self.last_exit: int | None = None
        self.last_reason = ""
        self.adopted_pid: int | None = None
        self.adopted_since = 0.0
        # console
        self.ui: subprocess.Popen | None = None
        self.ui_started = 0.0
        self.ui_backoff = 10.0
        self.ui_next_try = 0.0
        self.ui_env_digest = ""
        self.ui_external = False
        self.started = now()
        self._last_alert: dict[str, float] = {}

    # ------------------------------------------------------------------ plumbing
    def log(self, msg: str) -> None:
        line = f"{stamp()} {msg}\n"
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 2_000_000:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        if sys.stdout.isatty():
            print(line, end="", flush=True)

    def env(self) -> dict[str, str]:
        e = dict(os.environ)
        e.update(read_dotenv(self.env_path))   # .env wins: rotating a token is an edit of that file
        e["PYTHONUNBUFFERED"] = "1"
        return e

    def notify(self, kind: str, text: str, every_s: float = 1800.0, timeout: float = 10.0) -> None:
        """Telegram push, at most one per kind per `every_s`. Never raises; never logs the token."""
        if not self.telegram or now() - self._last_alert.get(kind, 0.0) < every_s:
            return
        self._last_alert[kind] = now()
        env = self.env()
        token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
        if not (token and chat):
            return
        try:
            body = urllib.parse.urlencode({"chat_id": chat, "text": f"[hl-news-bot 守护进程] {text}"[:3900],
                                           "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
            urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            self.log(f"telegram alert failed: {type(e).__name__}")

    def write_status(self) -> None:
        own = self.bot is not None and self.bot.poll() is None
        st = {
            "pid": os.getpid(), "started": int(self.started), "updated": int(now()), "repo": self.repo,
            "live": self.live, "with_ui": self.with_ui, "ui_port": self.ui_port,
            "bot_pid": self.bot.pid if own else self.adopted_pid, "adopted": not own and self.adopted_pid is not None,
            "bot_started": int(self.bot_started) if own else None,
            "restarts": self.restarts, "last_exit": self.last_exit, "last_reason": self.last_reason,
            "backoff_s": self.backoff,
            "next_start": int(self.next_start) if self.bot is None and self.adopted_pid is None else None,
            "env_pending": self.env_pending,
            "ui_pid": self.ui.pid if self.ui is not None and self.ui.poll() is None else None,
            "ui_external": self.ui_external,
            "stopping": self.stopping,
        }
        tmp = os.path.join(self.data, STATUS_FILE + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, os.path.join(self.data, STATUS_FILE))
        except OSError:
            pass

    def tail(self, name: str = "bot.out", n: int = 8) -> str:
        try:
            with open(os.path.join(self.data, name), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 8000))
                lines = f.read().decode("utf-8", "replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    # ------------------------------------------------------------------ processes
    def bot_command(self) -> list[str]:
        if self.bot_cmd:
            return list(self.bot_cmd)
        return [self.python, "-m", "hlnews.run"] + ([] if self.live else ["--paper"])

    def spawn(self, cmd: list[str], out_name: str) -> subprocess.Popen:
        out = open(os.path.join(self.data, out_name), "ab")
        try:
            return subprocess.Popen(cmd, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    env=self.env(), start_new_session=True, close_fds=True)
        finally:
            out.close()   # the child has its own descriptor

    def start_bot(self) -> None:
        cmd = self.bot_command()
        self.bot_env_digest = file_digest(self.env_path)
        self.env_pending = False
        self.bot_started = now()
        try:
            self.bot = self.spawn(cmd, "bot.out")
        except OSError as e:   # interpreter missing (broken .venv), permissions...
            self.bot = None
            self.log(f"cannot start the bot: {e}")
            self.handle_exit(127, spawn_error=f"cannot start: {e}")
            return
        self.log(f"bot started, pid {self.bot.pid}, {'live allowed' if self.live else 'paper'}: {' '.join(cmd[1:])}")

    @staticmethod
    def stop_proc(p: subprocess.Popen | None, sig: int = signal.SIGINT, timeout_s: float = 60.0) -> int | None:
        """Graceful stop: `sig` to the process group, wait up to timeout_s, then SIGKILL. Returns the exit code."""
        if p is None:
            return None
        if p.poll() is not None:
            return p.poll()
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        t0 = now()
        while p.poll() is None and now() - t0 < timeout_s:
            time.sleep(0.1)
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return p.poll()

    @staticmethod
    def stop_pid(pid: int, timeout_s: float = 60.0) -> None:
        """Stop a bot this supervisor did not start: SIGINT, wait, then SIGKILL."""
        for sig, wait in ((signal.SIGINT, timeout_s), (signal.SIGKILL, 5.0)):
            try:
                os.kill(pid, sig)
            except OSError:
                return
            t0 = now()
            while pid_alive(pid) and now() - t0 < wait:
                time.sleep(0.1)
            if not pid_alive(pid):
                return

    def locked_by_other(self) -> int | None:
        """pid (or -1 when unknown) of a bot holding hlnews.lock that is not our child; None when nobody holds it."""
        if not instance.is_locked(self.data):
            return None
        pid = instance.holder_pid(self.data)
        if self.bot is not None and pid == self.bot.pid:
            return None
        return pid or -1

    # ------------------------------------------------------------------ bot lifecycle
    def handle_exit(self, rc: int, spawn_error: str = "") -> None:
        ran = now() - self.bot_started
        self.last_exit = rc
        self.bot = None
        if self.stopping:
            return
        self.restarts += 1
        if rc == EXIT_ALREADY_RUNNING:
            self.last_reason = "another bot already holds data/hlnews.lock"
            self.log(f"bot exited {rc}: {self.last_reason}; watching that one instead")
            self.next_start = now() + 2
            return
        self.exits = [t for t in self.exits if now() - t < 1800] + [now()]
        if ran < 60 or len(self.exits) >= 5:     # quick death, or a fifth exit within 30 minutes: crash loop
            self.quick_failures += 1
            self.backoff = self.backoff_min if self.quick_failures == 1 else min(self.backoff * 2, self.backoff_max)
        else:
            self.quick_failures = 0
            self.backoff = self.backoff_min
        self.last_reason = spawn_error or f"exit code {rc} after {ran:.0f}s"
        self.next_start = now() + self.backoff
        self.log(f"bot stopped: {self.last_reason}; restarting in {self.backoff:.0f}s")
        live_note = ("\n注意：live 模式重启后不会接管交易所上已有的持仓（交易所侧止损单还在），请到 Hyperliquid 核对。"
                     if self.live else "")
        if self.quick_failures >= 3:
            self.notify("crashloop", f"bot 连续 {self.quick_failures} 次很快退出（{self.last_reason}），"
                                     f"{self.backoff:.0f} 秒后再试。最近输出：\n{self.tail()}{live_note}")
        else:
            self.notify("restart", f"bot 退出（{self.last_reason}），{self.backoff:.0f} 秒后自动重启。{live_note}")

    def kill_hung_bot(self, reason: str) -> None:
        self.last_reason = reason
        self.log(f"bot pid {self.bot.pid} looks hung ({reason}); restarting it")
        self.notify("hang", f"bot {reason}，判定卡住，已强制重启。")
        rc = self.stop_proc(self.bot, signal.SIGINT, min(20.0, self.stop_timeout_s))
        self.restarts += 1
        self.last_exit = rc
        self.bot = None
        self.next_start = now() + self.backoff_min

    def check_env_change(self, st: dict) -> None:
        """Restart the bot after .env changed, but only while it holds no position (a stop flattens everything)."""
        if not self.env_watch or self.bot is None:
            return
        if file_digest(self.env_path) == self.bot_env_digest:
            self.env_pending = False
            return
        if st.get("open"):
            if not self.env_pending:
                self.log(f".env changed; {len(st['open'])} position(s) open, the bot restarts once it is flat")
            self.env_pending = True
            return
        self.log(".env changed; restarting the bot so it reads the new values")
        rc = self.stop_proc(self.bot, signal.SIGINT, self.stop_timeout_s)
        self.last_exit = rc
        self.last_reason = ".env changed"
        self.bot = None
        self.next_start = 0.0
        self.restarts += 1

    def step(self) -> None:
        if self.bot is not None:
            rc = self.bot.poll()
            if rc is not None:
                self.handle_exit(rc)
                return
            up = now() - self.bot_started
            age, st = read_heartbeat(self.data, self.bot.pid)
            limit = self.stale_s + (self.stop_timeout_s if st.get("stopped") else 0.0)   # flattening on the way out
            # no heartbeat yet: allow --grace-s for startup; a heartbeat that existed and went stale counts at once
            if (age is None and up > self.grace_s) or (age is not None and age > limit):
                self.kill_hung_bot("没有心跳" if age is None else f"心跳 {age:.0f} 秒没更新")
                return
            if up > self.healthy_reset_s and (self.quick_failures or self.exits or self.backoff != self.backoff_min):
                self.quick_failures, self.exits, self.backoff = 0, [], self.backoff_min
            if age is not None and not st.get("stopped"):
                self.check_env_change(st)
            return
        # no child of ours: a bot started elsewhere may hold the lock (orphan of a killed supervisor, a shell run)
        other = self.locked_by_other()
        if other is not None:
            if self.adopted_pid != other:
                self.adopted_pid = other
                self.adopted_since = now()
                self.log(f"bot pid {other} is already running from this directory; watching it instead of starting another")
            if other > 0:
                age, st = read_heartbeat(self.data, other)
                limit = self.stale_s + (self.stop_timeout_s if st.get("stopped") else 0.0)
                if (age is None and now() - self.adopted_since > self.grace_s) or (age is not None and age > limit):
                    self.log(f"watched bot pid {other} has no fresh heartbeat; stopping it")
                    self.notify("hang", f"bot（pid {other}）心跳停止，已强制重启。")
                    self.stop_pid(other, min(20.0, self.stop_timeout_s))
            return
        if self.adopted_pid is not None:
            self.log(f"watched bot pid {self.adopted_pid} is gone; starting our own")
            self.adopted_pid = None
            self.next_start = 0.0
        if now() >= self.next_start:
            self.start_bot()

    def ensure_ui(self) -> None:
        if not self.with_ui or self.stopping:
            return
        if self.env_watch and self.ui is not None and self.ui.poll() is None:
            if file_digest(self.env_path) != self.ui_env_digest:   # the console shows which keys are set
                self.log(".env changed; restarting the console too")
                self.stop_proc(self.ui, signal.SIGINT, 10.0)
                self.ui = None
                self.ui_next_try = 0.0
        if self.ui is not None:
            if self.ui.poll() is None:
                if now() - self.ui_started > 300:
                    self.ui_backoff = 10.0
                return
            ran = now() - self.ui_started
            self.ui_backoff = min(self.ui_backoff * 2, 600.0) if ran < 30 else 10.0
            self.log(f"console exited with {self.ui.poll()} after {ran:.0f}s; retry in {self.ui_backoff:.0f}s")
            self.ui = None
            self.ui_next_try = now() + self.ui_backoff
        if now() < self.ui_next_try:
            return
        if port_open(self.ui_port):
            # usually a console left behind by a supervisor that was killed with SIGKILL: it still works, so use it
            # instead of failing to bind every few minutes (hlbot stop / restart clean it up)
            if not self.ui_external:
                self.log(f"port {self.ui_port} is already served (console of an earlier supervisor?); not starting another")
            self.ui_external = True
            return
        if self.ui_external:
            self.log(f"port {self.ui_port} is free again; starting our own console")
        self.ui_external = False
        cmd = self.ui_cmd or [self.python, "-m", "hlnews.ui", "--repo", self.repo, "--port", str(self.ui_port),
                              "--python", self.python]
        self.ui_env_digest = file_digest(self.env_path)
        try:
            self.ui = self.spawn(cmd, "ui.out")
        except OSError as e:
            self.log(f"cannot start the console: {e}")
            self.ui_next_try = now() + 600
            return
        self.ui_started = now()
        self.log(f"console started, pid {self.ui.pid}, http://127.0.0.1:{self.ui_port}")

    def rotate_logs(self) -> None:
        for name, size, keep in (("bot.out", 5_000_000, 1), ("hlnews.log", 20_000_000, 2), ("ui.out", 2_000_000, 1)):
            if copytruncate(os.path.join(self.data, name), size, keep):
                self.log(f"rotated data/{name}")

    # ------------------------------------------------------------------ main loop
    def take_lock(self) -> bool:
        import fcntl

        self.lock_fh = open(instance.lock_path(self.data, instance.SUPERVISOR_LOCK), "a+", encoding="utf-8")
        for attempt in range(5):   # a status probe holds the lock for microseconds; retry before giving up
            try:
                fcntl.flock(self.lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == 4:
                    self.lock_fh.seek(0)
                    print(f"supervisor already running (pid {self.lock_fh.read().strip() or '?'})", file=sys.stderr)
                    return False
                time.sleep(0.1)
        self.lock_fh.seek(0)
        self.lock_fh.truncate()
        self.lock_fh.write(str(os.getpid()))
        self.lock_fh.flush()
        return True

    def run(self) -> int:
        if not self.take_lock():
            return EXIT_ALREADY_RUNNING

        def _stop(signum, _frame):  # noqa: ANN001
            if not self.stopping:
                self.stop_signal = signum
            self.stopping = True

        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, _stop)
        self.log(f"supervisor pid {os.getpid()} up: {self.repo}, {'live allowed' if self.live else 'paper'}, "
                 f"console {'on :' + str(self.ui_port) if self.with_ui else 'off'}, stale after {self.stale_s:.0f}s")
        self.notify("start", f"守护进程已启动（{'live 已允许' if self.live else 'paper 模式'}），正在拉起 bot。", every_s=600)
        last_rotate = 0.0
        while not self.stopping:
            try:
                self.step()
                self.ensure_ui()
                if now() - last_rotate > 60:
                    self.rotate_logs()
                    last_rotate = now()
                self.write_status()
            except Exception as e:  # noqa: BLE001 - one bad tick must not take the supervisor down
                self.log(f"tick error: {type(e).__name__}: {e}")
            t_end = now() + self.tick
            while not self.stopping and now() < t_end:
                time.sleep(min(0.1, self.tick))
        # shutdown: the bot first (it flattens open positions), then the console
        self.log(f"got signal {self.stop_signal}, stopping")
        self.write_status()
        if self.bot is not None:
            self.log(f"stopping bot pid {self.bot.pid} (SIGINT: it closes open positions first)")
            rc = self.stop_proc(self.bot, signal.SIGINT, self.stop_timeout_s)
            self.log(f"bot stopped with {rc}")
        elif self.adopted_pid and self.adopted_pid > 0 and pid_alive(self.adopted_pid):
            self.log(f"stopping watched bot pid {self.adopted_pid}")
            self.stop_pid(self.adopted_pid, self.stop_timeout_s)
        self.bot = None
        self.adopted_pid = None
        if self.ui is not None:
            self.stop_proc(self.ui, signal.SIGINT, 10.0)
            self.ui = None
        self.write_status()
        self.notify("stop", f"守护进程已停止（信号 {self.stop_signal}：工作区停止或 hlbot stop），bot 已退出。",
                    every_s=60, timeout=5)
        self.log("supervisor stopped")
        return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="keep hl-news-bot running")
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--python", default=sys.executable, help="interpreter for the bot and console (the repo's .venv)")
    ap.add_argument("--live", action="store_true", help="drop --paper; config.yaml mode decides (paper unless it says live)")
    ap.add_argument("--with-ui", action="store_true", help="also keep the local console on 127.0.0.1:--ui-port alive")
    ap.add_argument("--ui-port", type=int, default=8765)
    ap.add_argument("--no-env-watch", action="store_true", help="do not restart the bot when .env changes")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--stale-s", type=float, default=90.0, help="heartbeat age that counts as hung")
    ap.add_argument("--grace-s", type=float, default=120.0, help="startup time before a missing heartbeat counts")
    ap.add_argument("--tick", type=float, default=5.0)
    ap.add_argument("--backoff-min", type=float, default=5.0)
    ap.add_argument("--backoff-max", type=float, default=300.0)
    ap.add_argument("--healthy-reset-s", type=float, default=600.0, help="uptime after which crash backoff resets")
    ap.add_argument("--stop-timeout-s", type=float, default=60.0, help="time the bot gets to flatten and exit")
    ap.add_argument("--bot-cmd", default="", help=argparse.SUPPRESS)   # JSON list, tests only
    ap.add_argument("--ui-cmd", default="", help=argparse.SUPPRESS)    # JSON list, tests only
    return ap


def main() -> int:
    return Supervisor(build_parser().parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
