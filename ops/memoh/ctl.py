#!/usr/bin/env python3
"""hlbot: run hl-news-bot on a Memoh cloud computer and keep it running. Call it through ops/memoh/hlbot.

  hlbot setup [--plan P] [--no-ui] [--no-hooks] [--no-skill] [--no-start]   after install.sh: .env, plan, hooks, skill, checks, start
  hlbot start [--live|--paper] [--ui|--no-ui]   start the supervisor, which starts and watches the bot; clears a manual stop
  hlbot stop                                    stop bot and supervisor and keep them stopped (hooks / schedules leave it down)
  hlbot restart [--live|--paper] [--ui|--no-ui] stop, then start (stopping closes open positions)
  hlbot status [--json]                         supervisor, bot heartbeat, equity, positions, news, console, hooks
  hlbot ensure [--quiet|--hook]                 start whatever is down; what the Memoh hooks and the schedule call
  hlbot logs [bot|app|sup|ui] [-n N] [-f]       recent log lines
  hlbot doctor [--net]                          check install, .env, hooks, disk; --net also calls NewsLiquid (1 quota) and HL
  hlbot plan [free|plus|pro|max] [--poll-s N]   NewsLiquid transport and polling for your plan (edits config.yaml)
  hlbot hooks [--remove]                        add / remove our hooks in /data/.memoh/hooks.json
  hlbot skill [--remove]                        add / remove the hl-news-bot-ops skill for the Memoh agent
  hlbot update [--no-restart]                   git pull, pip install, restart (only while flat)

Paper mode unless BOTH config.yaml says `mode: live` AND the bot was started with --live. Secrets stay in .env and are
never printed. Output is Chinese because it is read by yanbo and relayed by his Memoh agent.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

CODE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.abspath(os.environ.get("HLBOT_ROOT") or CODE)   # deployment dir: config.yaml, .env, data/ (tests point it elsewhere)
DATA = os.path.join(ROOT, "data")
ENV_FILE = os.path.join(ROOT, ".env")
SUPERVISOR = os.path.join(CODE, "ops", "memoh", "supervisor.py")
HLBOT = os.path.join(CODE, "ops", "memoh", "hlbot")
SKILL_TEMPLATE = os.path.join(CODE, "ops", "memoh", "SKILL.md")
OPTS_FILE = os.path.join(DATA, "supervisor.opts.json")
DISABLED_FILE = os.path.join(DATA, "supervisor.disabled")
HOOKS_FILE = os.environ.get("MEMOH_HOOKS_FILE", "/data/.memoh/hooks.json")
SKILLS_DIR = os.environ.get("MEMOH_SKILLS_DIR", "/data/skills/user/personal")
SKILL_NAME = "hl-news-bot-ops"
HOOK_PREFIX = "hl-news-bot:"
STATUS_STALE_S = 180      # supervisor.json is rewritten every tick (5 s); this old means the supervisor is stuck

sys.path.insert(0, CODE)
from hlnews import instance  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supervisor import read_dotenv, read_heartbeat  # noqa: E402

# NewsLiquid plans -> feeds.newsliquid settings. Budgets assume 1 quota per REST call (check the call log on the
# NewsLiquid dashboard after `hlbot doctor --net`; if a call costs more, poll slower).
PLANS = {
    "free": {"transport": "rest", "poll_s": 900, "fast_poll_s": 900, "max_pages": 1, "page_size": 20,
             "note": "免费档 100 次/天：每 15 分钟一次（96 次/天），只够验证链路，不适合交易"},
    "plus": {"transport": "rest", "poll_s": 180, "fast_poll_s": 60, "max_pages": 2, "page_size": 50,
             "note": "Plus 2 万次：每 3 分钟一次，约 480 次/天 ≈ 1.5 万次/30 天"},
    "pro": {"transport": "rest", "poll_s": 20, "fast_poll_s": 5, "max_pages": 3, "page_size": 50,
            "note": "Pro 20 万次：每 20 秒一次，约 4,320 次/天 ≈ 13 万次/30 天，给热窗口和补页留余量"},
    "max": {"transport": "auto", "poll_s": 15, "fast_poll_s": 5, "max_pages": 3, "page_size": 50,
            "note": "Max：WebSocket 实时推送，REST 只做断线补漏和每 5 分钟一次的漏推检查"},
}
DEFAULT_OPTS = {"live": False, "ui": True, "ui_port": 8765}


# --------------------------------------------------------------------------- small helpers
def now() -> float:
    return time.time()


def venv_python() -> str:
    for base in (ROOT, CODE):
        p = os.path.join(base, ".venv", "bin", "python")
        if os.path.exists(p):
            return p
    return sys.executable


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "从未"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} 秒前"
    if s < 5400:
        return f"{s // 60} 分钟前"
    if s < 172800:
        return f"{s // 3600} 小时 {s % 3600 // 60} 分前"
    return f"{s // 86400} 天前"


def dur(seconds: float) -> str:
    s = int(max(0, seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d} 天 {h} 小时" if d else (f"{h} 小时 {m} 分" if h else f"{m} 分 {s % 60} 秒")


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_opts() -> dict:
    opts = dict(DEFAULT_OPTS)
    opts.update(load_json(OPTS_FILE, {}) or {})
    return opts


def save_opts(update: dict) -> None:
    """Merge into data/supervisor.opts.json (live, ui, ui_port, plan) without dropping keys that are not in `update`."""
    write_json(OPTS_FILE, {**(load_json(OPTS_FILE, {}) or {}), **update})


def drain_stdin(timeout: float = 1.0) -> None:
    """Hooks pass the event JSON on stdin; read it so the writer never blocks, but never wait for EOF forever."""
    import select

    try:
        if sys.stdin is None or sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        end = now() + timeout
        while now() < end:
            ready, _, _ = select.select([fd], [], [], max(0.0, end - now()))
            if not ready or not os.read(fd, 65536):
                return
    except (OSError, ValueError):
        return


def wait_until(pred, timeout: float, step: float = 0.2) -> bool:
    end = now() + timeout
    while now() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


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


def load_cfg() -> dict:
    import yaml

    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# --------------------------------------------------------------------------- state
def sup_info() -> dict:
    alive = instance.is_locked(DATA, instance.SUPERVISOR_LOCK)
    st = load_json(os.path.join(DATA, "supervisor.json"), {}) or {}
    pid = instance.holder_pid(DATA, instance.SUPERVISOR_LOCK) if alive else None
    age = now() - float(st["updated"]) if alive and st.get("updated") and st.get("pid") == pid else None
    return {"alive": alive, "pid": pid, "status": st if alive else {}, "status_age": age}


def bot_info() -> dict:
    locked = instance.is_locked(DATA)
    pid = instance.holder_pid(DATA) if locked else None
    age, st = read_heartbeat(DATA, pid) if pid else (None, {})
    return {"running": locked, "pid": pid, "age": age, "state": st}


def env_summary() -> dict:
    env = read_dotenv(ENV_FILE)

    def real(k: str) -> bool:
        v = env.get(k, "")
        return bool(v) and v not in ("0x...", "...", "changeme")

    return {"exists": os.path.exists(ENV_FILE), "OPENNEWS_TOKEN": real("OPENNEWS_TOKEN"),
            "HL_AGENT_KEY": real("HL_AGENT_KEY"), "HL_ACCOUNT": real("HL_ACCOUNT"),
            "TELEGRAM": real("TELEGRAM_BOT_TOKEN") and real("TELEGRAM_CHAT_ID")}


def hooks_installed(path: str = "") -> dict:
    path = path or HOOKS_FILE
    cfg = load_json(path)
    if not isinstance(cfg, dict):
        return {"file": os.path.exists(path), "enabled": False, "ours": []}
    ours = [h.get("event") for h in cfg.get("hooks") or [] if isinstance(h, dict)
            and str(h.get("name", "")).startswith(HOOK_PREFIX) and h.get("enabled", True)]
    return {"file": True, "enabled": cfg.get("enabled", True) is not False, "ours": ours}


def collect() -> dict:
    sup, bot = sup_info(), bot_info()
    return {"root": ROOT, "opts": load_opts(), "disabled": (load_json(DISABLED_FILE, {}) or {}) if os.path.exists(DISABLED_FILE) else None,
            "supervisor": sup, "bot": bot, "env": env_summary(), "hooks": hooks_installed(),
            "skill": os.path.exists(os.path.join(SKILLS_DIR, SKILL_NAME, "SKILL.md"))}


def health(c: dict) -> tuple[str, str]:
    """(level, one line). Levels: OK, STOPPED (by hlbot stop), DOWN, DEGRADED, STARTING."""
    sup, bot = c["supervisor"], c["bot"]
    if c["disabled"] is not None and not sup["alive"] and not bot["running"]:
        return "STOPPED", "已用 hlbot stop 手动停止（hooks 和定时任务不会拉起）；恢复请运行 hlbot start"
    if not sup["alive"]:
        if bot["running"]:
            return "DEGRADED", f"bot 在跑（pid {bot['pid']}）但没有守护进程，挂了不会自动重启；运行 hlbot ensure"
        return "DOWN", "守护进程和 bot 都没在运行"
    if sup["status_age"] is not None and sup["status_age"] > STATUS_STALE_S:
        return "DEGRADED", f"守护进程 pid {sup['pid']} 卡住了（状态 {sup['status_age']:.0f} 秒没更新）；hlbot ensure 会重启它"
    st = sup["status"]
    if not bot["running"]:
        nxt = st.get("next_start")
        wait = f"，{max(0, int(nxt - now()))} 秒后重试" if nxt else ""
        reason = st.get("last_reason") or "启动中"
        level = "DEGRADED" if st.get("restarts", 0) >= 3 else "STARTING"
        return level, f"守护进程 pid {sup['pid']} 在跑，bot 未运行（{reason}{wait}）"
    age, s = bot["age"], bot["state"]
    if age is None:
        return "STARTING", f"bot pid {bot['pid']} 正在启动（还没有心跳）"
    mode = s.get("mode", "?")
    parts = [f"bot pid {bot['pid']}（{mode}）心跳 {ago(age)}"]
    if s.get("equity_usd") is not None:
        parts.append(f"权益 ${float(s['equity_usd']):,.2f}")
    parts.append(f"持仓 {len(s.get('open') or [])}")
    last_news = s.get("last_news_ms")
    parts.append(f"最近新闻 {ago(now() - last_news / 1000) if last_news else '还没有'}")
    if s.get("halted_reason"):
        parts.append(f"已暂停开仓：{s['halted_reason']}")
    if not c["env"]["OPENNEWS_TOKEN"]:
        parts.append("没填 OPENNEWS_TOKEN，新闻源关闭")
    if age > 60:
        return "DEGRADED", "，".join(parts) + "（心跳偏旧，守护进程会处理）"
    return "OK", "，".join(parts)


# --------------------------------------------------------------------------- supervisor control
def launch_supervisor(opts: dict) -> int:
    py = venv_python()
    cmd = [py, SUPERVISOR, "--repo", ROOT, "--python", py]
    if opts.get("live"):
        cmd.append("--live")
    if opts.get("ui", True):
        cmd += ["--with-ui", "--ui-port", str(int(opts.get("ui_port", 8765)))]
    extra = os.environ.get("HLBOT_SUPERVISOR_ARGS")   # tests: short intervals, fake bot
    if extra:
        cmd += json.loads(extra)
    os.makedirs(DATA, exist_ok=True)
    out = open(os.path.join(DATA, "supervisor.out"), "ab")
    try:
        # own session, no inherited pipes: it outlives the hook / exec call that started it
        p = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
    finally:
        out.close()
    return p.pid


def stop_all(timeout: float, mark: bool, force: bool = True) -> tuple[bool, str]:
    """Stop supervisor (which stops bot and console), then any bot left holding the lock. Returns (stopped, note)."""
    if mark:
        write_json(DISABLED_FILE, {"ts": int(now()), "by": "hlbot stop"})
    end = now() + timeout
    sup = sup_info()
    notes = []
    if sup["alive"] and sup["pid"]:
        try:
            os.kill(sup["pid"], signal.SIGTERM)
        except OSError:
            pass
        if not wait_until(lambda: not instance.is_locked(DATA, instance.SUPERVISOR_LOCK), max(1.0, end - now())):
            notes.append(f"守护进程 pid {sup['pid']} 在 {timeout:.0f} 秒内没退出")
            if force:
                try:
                    os.kill(sup["pid"], signal.SIGKILL)
                except OSError:
                    pass
    for pid in stray_consoles():   # consoles of a supervisor that was SIGKILLed outlive it (own session)
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    bot = bot_info()
    if bot["running"] and bot["pid"]:
        try:
            os.kill(bot["pid"], signal.SIGINT)   # the bot flattens open positions on SIGINT
        except OSError:
            pass
        if not wait_until(lambda: not instance.is_locked(DATA), max(1.0, end - now())):
            if force:
                notes.append(f"bot pid {bot['pid']} 没有按时退出，已强制结束（未平的仓位以交易所为准）")
                try:
                    os.kill(bot["pid"], signal.SIGKILL)
                except OSError:
                    pass
                wait_until(lambda: not instance.is_locked(DATA), 5)
            else:
                notes.append(f"bot pid {bot['pid']} 还在退出中")
    stopped = not instance.is_locked(DATA) and not instance.is_locked(DATA, instance.SUPERVISOR_LOCK)
    return stopped, "；".join(notes)


def stray_consoles() -> list[int]:
    """pids of `python -m hlnews.ui --repo ROOT` processes (only called once our supervisor is gone)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        argv = args.split()
        if "hlnews.ui" in argv and "--repo" in argv and argv.index("--repo") + 1 < len(argv) \
                and os.path.abspath(argv[argv.index("--repo") + 1]) == ROOT and pid.isdigit() and int(pid) != os.getpid():
            pids.append(int(pid))
    return pids


def live_preflight(opts: dict) -> list[str]:
    """Reasons a --live start would not trade live or would crash-loop."""
    if not opts.get("live"):
        return []
    errs = []
    try:
        mode = load_cfg().get("mode", "paper")
    except Exception as e:  # noqa: BLE001
        return [f"config.yaml 读不了：{e}"]
    if mode != "live":
        errs.append("config.yaml 里 mode 不是 live：带 --live 也仍然按 paper 运行（两把钥匙都要打开才会实盘）")
    env = env_summary()
    if mode == "live" and not (env["HL_AGENT_KEY"] and env["HL_ACCOUNT"]):
        errs.append("实盘需要 .env 里的 HL_AGENT_KEY（API 钱包私钥）和 HL_ACCOUNT（主账户地址）")
    return errs


def apply_flags(opts: dict, a: argparse.Namespace) -> dict:
    opts = dict(opts)
    if getattr(a, "live", False):
        opts["live"] = True
    if getattr(a, "paper", False):
        opts["live"] = False
    if getattr(a, "ui", None) is not None:
        opts["ui"] = a.ui
    if getattr(a, "ui_port", None):
        opts["ui_port"] = a.ui_port
    return opts


def start(opts: dict, wait_s: float = 30.0, quiet: bool = False) -> int:
    errs = live_preflight(opts)
    if errs and opts.get("live"):
        hard = [e for e in errs if "HL_AGENT_KEY" in e or "读不了" in e]
        for e in errs:
            print(("✘ " if e in hard else "! ") + e)
        if hard:
            return 1
    save_opts({k: opts[k] for k in ("live", "ui", "ui_port") if k in opts})
    if os.path.exists(DISABLED_FILE):
        os.remove(DISABLED_FILE)
    sup = sup_info()
    if sup["alive"]:
        cur = sup["status"]
        if cur and (bool(cur.get("live")) != bool(opts.get("live")) or bool(cur.get("with_ui")) != bool(opts.get("ui"))
                    or (opts.get("ui") and int(cur.get("ui_port") or 0) != int(opts.get("ui_port", 8765)))):
            print("守护进程已在运行，但用的是旧设置；运行 hlbot restart 应用新设置（重启会平掉所有持仓）")
        elif not quiet:
            print(f"守护进程已在运行（pid {sup['pid']}）")
        level, line = health(collect())
        print(f"{level}: {line}")
        return 0
    pid = launch_supervisor(opts)
    if not wait_until(lambda: instance.is_locked(DATA, instance.SUPERVISOR_LOCK) or not pid_alive(pid), 10):
        print(f"✘ 守护进程没起来，看 {os.path.join(DATA, 'supervisor.out')}")
        return 1
    if not instance.is_locked(DATA, instance.SUPERVISOR_LOCK):
        print(f"✘ 守护进程启动后马上退出了：\n{tail_file(os.path.join(DATA, 'supervisor.out'), 15)}")
        return 1
    if not quiet:
        print(f"守护进程已启动（pid {pid}），{'live 已允许' if opts.get('live') else 'paper 模式'}"
              + (f"，控制台 http://127.0.0.1:{opts.get('ui_port', 8765)}" if opts.get("ui", True) else ""))
    if wait_s > 0:
        wait_until(lambda: bot_info()["age"] is not None, wait_s, 0.5)
    level, line = health(collect())
    print(f"{level}: {line}")
    if level == "STARTING":
        print("bot 还在启动（连 Hyperliquid 拉行情需要几秒到几十秒），稍后运行 hlbot status 再看")
    return 0


def tail_file(path: str, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200 * n - 4096))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


# --------------------------------------------------------------------------- commands
def cmd_start(a) -> int:
    return start(apply_flags(load_opts(), a))


def cmd_stop(a) -> int:
    if a.hook:
        drain_stdin()
    timeout = a.timeout or (45.0 if a.hook else 90.0)
    c = collect()
    if not c["supervisor"]["alive"] and not c["bot"]["running"]:
        if not a.hook:
            write_json(DISABLED_FILE, {"ts": int(now()), "by": "hlbot stop"})
        print("本来就没在运行" + ("" if a.hook else "；已记为手动停止，hooks 和定时任务不会再拉起它（hlbot start 恢复）"))
        return 0
    open_n = len((c["bot"]["state"] or {}).get("open") or [])
    stopped, note = stop_all(timeout, mark=not a.hook, force=not a.hook)
    msg = "已停止" if stopped else "停止未完成"
    if open_n:
        msg += f"（停止前有 {open_n} 个持仓，bot 退出时会市价平掉）"
    if not a.hook:
        msg += "；hooks 和定时任务不会再拉起它，hlbot start 恢复"
    print(msg + (f"。{note}" if note else ""))
    return 0 if stopped or a.hook else 1


def cmd_restart(a) -> int:
    opts = apply_flags(load_opts(), a)
    c = collect()
    if c["supervisor"]["alive"] or c["bot"]["running"]:
        stopped, note = stop_all(90.0, mark=False)
        if not stopped:
            print(f"✘ 没能停下旧进程：{note}")
            return 1
        print("旧进程已停止" + (f"（{note}）" if note else ""))
    return start(opts)


def cmd_ensure(a) -> int:
    if a.hook:
        drain_stdin()
    if os.path.exists(DISABLED_FILE):
        print("STOPPED: 已用 hlbot stop 手动停止，不自动拉起；恢复请运行 hlbot start")
        return 0
    sup = sup_info()
    note = ""
    if sup["alive"] and sup["status_age"] is not None and sup["status_age"] > STATUS_STALE_S and sup["pid"]:
        note = f"守护进程 pid {sup['pid']} 卡住（{sup['status_age']:.0f} 秒没更新），已强制结束并重启；"
        try:
            os.kill(sup["pid"], signal.SIGKILL)
        except OSError:
            pass
        wait_until(lambda: not instance.is_locked(DATA, instance.SUPERVISOR_LOCK), 5)
        sup = sup_info()
    if sup["alive"]:
        level, line = health(collect())
        started = (sup["status"] or {}).get("started")
        if started and now() - started < 900:   # a fresh supervisor means something (a hook, a person) just revived it
            line += f"；守护进程 {ago(now() - started)}刚（重新）启动"
        if level in ("OK", "STARTING"):
            print(f"OK: {line}")
            return 0
        print(f"{level}: {line}")
        if not a.quiet and not a.hook:
            print(tail_file(os.path.join(DATA, "supervisor.log"), 5))
        return 0 if a.hook else 2
    opts = load_opts()
    pid = launch_supervisor(opts)
    if a.hook:
        print(f"RESTARTED: {note}守护进程不在运行，已拉起（pid {pid}）")
        return 0
    ok = wait_until(lambda: instance.is_locked(DATA, instance.SUPERVISOR_LOCK) or not pid_alive(pid), 10)
    if not ok or not instance.is_locked(DATA, instance.SUPERVISOR_LOCK):
        print(f"FAILED: 守护进程拉不起来：\n{tail_file(os.path.join(DATA, 'supervisor.out'), 10)}")
        return 1
    print(f"RESTARTED: {note}守护进程不在运行，已拉起（pid {pid}，{'live 已允许' if opts.get('live') else 'paper'}）")
    return 0


def plan_estimate(nl: dict) -> str:
    transport = str(nl.get("transport", "auto"))
    poll = float(nl.get("poll_s", 15))
    if transport == "rest":
        per_day = 86400 / poll
        return f"REST 每 {poll:g} 秒一次，基础约 {per_day:,.0f} 次/天（≈{per_day * 30 / 10000:.1f} 万次/30 天，另加热窗口加速和补页）"
    audit = float(nl.get("ws_audit_s", 300))
    tail = f"，漏推检查约 {86400 / audit:,.0f} 次/天" if audit > 0 else ""
    return (f"{transport}：先连 WebSocket（Max 档），连不上就 REST 每 {poll:g} 秒一次"
            f"（约 {86400 / poll:,.0f} 次/天）{tail}")


def cmd_status(a) -> int:
    c = collect()
    level, line = health(c)
    if a.json:
        c["health"] = {"level": level, "line": line}
        print(json.dumps(c, ensure_ascii=False, indent=2, default=str))
        return 0
    sup, bot, opts = c["supervisor"], c["bot"], c["opts"]
    st = sup["status"]
    print(f"{level}: {line}")
    print(f"目录：{ROOT}；启动方式：{'live 已允许（还要 config.yaml mode: live）' if opts.get('live') else 'paper'}")
    if sup["alive"]:
        started = st.get("started")
        restarts = st.get("restarts", 0)
        last = f"，上次：{st.get('last_reason')}" if st.get("last_reason") else ""
        print(f"守护进程：pid {sup['pid']}，已运行 {dur(now() - started) if started else '?'}，重启过 {restarts} 次{last}")
        if st.get("env_pending"):
            print("  .env 改过了：有持仓，等空仓后自动重启生效")
        if st.get("adopted"):
            print(f"  bot pid {st.get('bot_pid')} 不是它启动的（接管监视中）")
    else:
        print("守护进程：未运行")
    if bot["running"]:
        s = bot["state"]
        mids = s.get("mids") or {}
        for p in (s.get("open") or [])[:10]:
            if isinstance(p, dict):
                side = {"long": "多", "short": "空"}.get(str(p.get("direction")), str(p.get("direction")))
                mid = mids.get(p.get("coin"))
                print(f"  持仓 {p.get('coin')} {side} 开仓价 {p.get('entry_px')} 现价 {mid if mid else '?'} "
                      f"止损 {p.get('stop_px')} 名义 ${float(p.get('notional_usd') or 0):,.0f}（{p.get('event_class')}）")
    if opts.get("ui", True):
        url = f"http://127.0.0.1:{opts.get('ui_port', 8765)}"
        if sup["alive"] and st.get("ui_pid"):
            where = f"{url}（pid {st['ui_pid']}）"
        elif sup["alive"] and st.get("ui_external"):
            where = f"{url}（由之前留下的控制台进程提供）"
        else:
            where = "未运行"
        print(f"控制台：{where}；在 Memoh 的 Desktop 标签页打开浏览器访问")
    env = c["env"]
    try:
        nl = (load_cfg().get("feeds") or {}).get("newsliquid") or {}
        print(f"NewsLiquid：token {'已填' if env['OPENNEWS_TOKEN'] else '未填（新闻源关闭）'}；{plan_estimate(nl)}")
    except Exception as e:  # noqa: BLE001
        print(f"NewsLiquid：config.yaml 读不了：{e}")
    print(f"Telegram 告警：{'已配置' if env['TELEGRAM'] else '未配置'}")
    hk = c["hooks"]
    want = {"WorkspaceStart", "SessionStart", "WorkspaceStop"}
    if not hk["file"]:
        print("Memoh hooks：未安装（hlbot hooks 安装；不在 Memoh 上可忽略）")
    else:
        miss = want - set(hk["ours"])
        print(f"Memoh hooks：{'已安装' if not miss else '缺 ' + '/'.join(sorted(miss))}"
              f"{'' if hk['enabled'] else '（hooks.json 顶层 enabled=false，全部不会运行）'}；"
              f"agent 技能：{'已安装' if c['skill'] else '未安装'}")
    if c["disabled"] is not None:
        print("手动停止标记：有（hlbot start 清除）")
    return {"OK": 0, "STOPPED": 0, "STARTING": 0}.get(level, 1 if level == "DOWN" else 2)


def cmd_logs(a) -> int:
    files = {"bot": "bot.out", "app": "hlnews.log", "sup": "supervisor.log", "ui": "ui.out"}
    names = [a.which] if a.which != "all" else ["sup", "bot"]
    if a.follow:
        path = os.path.join(DATA, files[names[0]])
        print(tail_file(path, a.n))
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    chunk = f.read()
                    if chunk:
                        sys.stdout.write(chunk.decode("utf-8", "replace"))
                        sys.stdout.flush()
                    else:
                        if os.path.getsize(path) < f.tell():   # rotated (copy-truncate)
                            f.seek(0)
                        time.sleep(0.5)
        except KeyboardInterrupt:
            return 0
        except OSError as e:
            print(f"读不了 {path}：{e}")
            return 1
    for name in names:
        path = os.path.join(DATA, files[name])
        print(f"==> data/{files[name]} <==")
        print(tail_file(path, a.n) or "（空）")
    return 0


def set_plan(name: str, poll_s: float | None = None) -> tuple[bool, str]:
    sys.path.insert(0, ROOT)
    from hlnews.ui import Data   # text-preserving config.yaml edits (comments survive)

    p = dict(PLANS[name])
    note = p.pop("note")
    if poll_s:
        p["poll_s"] = poll_s
    patch = {f"feeds.newsliquid.{k}": v for k, v in p.items()}
    r = Data(ROOT).patch_config(patch)
    if not r.get("ok"):
        return False, f"改 config.yaml 失败：{r.get('error') or r.get('failed')}"
    save_opts({"plan": name})
    return True, note


def cmd_plan(a) -> int:
    if a.name:
        ok, note = set_plan(a.name, a.poll_s)
        print(("已设为 " + a.name + "：" + note) if ok else "✘ " + note)
        if not ok:
            return 1
    nl = (load_cfg().get("feeds") or {}).get("newsliquid") or {}
    print(f"当前：{plan_estimate(nl)}")
    if a.name and (sup_info()["alive"] or bot_info()["running"]):
        print("bot 正在运行：hlbot restart 后生效（重启会平掉持仓）")
    if not a.name:
        for k, v in PLANS.items():
            print(f"  {k:<5} {v['note']}")
        print("额度按“每次调用 1 次”估算；跑过 hlbot doctor --net 后，到 NewsLiquid 控制台的调用记录核对实际扣费")
    return 0


def hlbot_cmd() -> str:
    """How hooks, the skill and the docs call us: through bash, so a lost executable bit cannot break keep-alive."""
    env = f"HLBOT_ROOT={shlex.quote(ROOT)} " if ROOT != CODE else ""
    return f"{env}bash {shlex.quote(HLBOT)}"


def hook_defs() -> list[dict]:
    def hook(name: str, event: str, cmd: str, timeout: str) -> dict:
        return {"name": f"{HOOK_PREFIX} {name}", "event": event, "enabled": True,
                "actions": [{"type": "command", "command": f"{hlbot_cmd()} {cmd}", "work_dir": ROOT,
                             "timeout": timeout, "on_error": "ignore"}]}

    return [hook("start with workspace", "WorkspaceStart", "ensure --hook", "30s"),
            hook("check on new session", "SessionStart", "ensure --hook", "20s"),
            hook("stop with workspace", "WorkspaceStop", "stop --hook", "60s")]


def install_hooks(path: str, remove: bool = False) -> tuple[bool, str]:
    cfg = {"version": 1, "enabled": True, "hooks": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            cfg = json.loads(text) if text.strip() else cfg
        except (OSError, ValueError) as e:
            return False, f"{path} 不是合法 JSON，没有改动它（先在 Memoh 的 Hooks 标签页修好）：{e}"
        if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks", []), list):
            return False, f"{path} 的结构不认识（需要顶层对象和 hooks 数组），没有改动它"
        shutil.copyfile(path, path + ".bak-hlbot")
    cfg.setdefault("version", 1)
    cfg.setdefault("enabled", True)
    hooks = [h for h in cfg.get("hooks") or [] if not (isinstance(h, dict) and str(h.get("name", "")).startswith(HOOK_PREFIX))]
    if not remove:
        hooks += hook_defs()
    cfg["hooks"] = hooks
    try:
        write_json(path, cfg)
    except OSError as e:
        return False, f"写不了 {path}：{e}"
    if remove:
        return True, f"已从 {path} 移除 hl-news-bot 的 hooks"
    warn = "；注意：hooks.json 顶层 enabled=false，所有 hooks 都不会运行" if cfg.get("enabled") is False else ""
    return True, f"已写入 {path}：WorkspaceStart / SessionStart 时 hlbot ensure，WorkspaceStop 时 hlbot stop --hook{warn}"


def cmd_hooks(a) -> int:
    ok, msg = install_hooks(a.file or HOOKS_FILE, a.remove)
    print(("" if ok else "✘ ") + msg)
    return 0 if ok else 1


def install_skill(skills_dir: str, remove: bool = False) -> tuple[bool, str]:
    dest = os.path.join(skills_dir, SKILL_NAME)
    if remove:
        shutil.rmtree(dest, ignore_errors=True)
        return True, f"已删除 {dest}"
    try:
        with open(SKILL_TEMPLATE, encoding="utf-8") as f:
            text = f.read().replace("{{ROOT}}", ROOT).replace("{{HLBOT}}", hlbot_cmd())
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        return False, f"写不了 {dest}：{e}"
    return True, f"已安装 agent 技能 {os.path.join(dest, 'SKILL.md')}"


def cmd_skill(a) -> int:
    ok, msg = install_skill(a.dir or SKILLS_DIR, a.remove)
    print(("" if ok else "✘ ") + msg)
    return 0 if ok else 1


# --------------------------------------------------------------------------- checks
def check_newsliquid_rest(token: str, nl: dict) -> tuple[str, str]:
    sys.path.insert(0, ROOT)
    from hlnews.newsliquid import NLAuthError, NLQuotaError, make_rest_fetch

    try:
        rows = make_rest_fetch(str(nl.get("api_base") or "https://ai.6551.io"), token, timeout=15)({"limit": 1, "page": 1, "hasCoin": True})
    except NLAuthError as e:
        return "✘", f"NewsLiquid 拒绝了 token（{str(e)[:80]}）：token 错了、被刷新作废，或套餐不含 news_search"
    except NLQuotaError as e:
        return "✘", f"NewsLiquid 额度用完或被限流：{str(e)[:80]}"
    except Exception as e:  # noqa: BLE001
        return "!", f"NewsLiquid REST 调不通：{type(e).__name__}: {str(e)[:100]}"
    return "✔", f"NewsLiquid token 有效（REST 返回 {len(rows)} 条，消耗 1 次额度）"


def check_newsliquid_ws(token: str, nl: dict) -> tuple[str, str]:
    import asyncio
    import urllib.parse

    sys.path.insert(0, ROOT)
    from hlnews.newsliquid import _http_status

    url = str(nl.get("ws_url") or "wss://ai.6551.io/open/news_wss")
    url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token, safe="")

    async def probe() -> tuple[str, str]:
        import websockets

        try:
            async with websockets.connect(url, open_timeout=12, close_timeout=3, ping_interval=None) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "news.subscribe", "params": {"hasCoin": True}}))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    return "✔", "WebSocket 握手成功（8 秒内没有消息，属正常）"
                if isinstance(msg, (bytes, bytearray)):
                    msg = msg.decode("utf-8", "replace")
                if '"error"' in str(msg)[:400]:
                    return "!", f"WebSocket 连上了但订阅被拒：{str(msg)[:120]}"
                return "✔", "WebSocket 可用（实时推送，Max 档）"
        except Exception as e:  # noqa: BLE001
            code = _http_status(e)
            if code == 401:
                return "✘", "WebSocket 拒绝 token（401）"
            if code:
                return "!", f"WebSocket 不可用（HTTP {code}，多半是套餐不含实时推送）：bot 会自动用 REST 轮询"
            return "!", f"WebSocket 连不上：{type(e).__name__}: {str(e)[:80]}"

    return asyncio.run(probe())


def check_hyperliquid(cfg: dict) -> tuple[str, str]:
    import urllib.request

    base = "https://api.hyperliquid-testnet.xyz" if (cfg.get("hyperliquid") or {}).get("network") == "testnet" else "https://api.hyperliquid.xyz"
    req = urllib.request.Request(base + "/info", data=b'{"type":"allMids"}', headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            n = len(json.loads(r.read() or b"{}"))
    except Exception as e:  # noqa: BLE001
        return "✘", f"Hyperliquid API 调不通：{type(e).__name__}: {str(e)[:100]}"
    ms = (time.time() - t0) * 1000
    hint = "（HL 验证节点在东京，延迟越低越好）" if ms > 300 else ""
    return "✔", f"Hyperliquid API 正常，allMids {n} 个币，往返 {ms:.0f} ms{hint}"


def check_location() -> tuple[str, str]:
    """Where the cloud computer's traffic leaves from (Hyperliquid's terms restrict some regions). Three free lookups,
    first answer wins: ipinfo.io rate-limits shared egress IPs, so the others are fallbacks."""
    import urllib.request

    def get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.81.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    providers = (
        ("https://ipinfo.io/json", lambda d: (d.get("country"), d.get("region"), d.get("city"), d.get("org"))),
        ("https://ipwho.is/?fields=success,country_code,region,city,connection",
         lambda d: (d.get("country_code"), d.get("region"), d.get("city"), (d.get("connection") or {}).get("org"))
         if d.get("success", True) else (None,) * 4),
        ("https://api.country.is/", lambda d: (d.get("country"), None, None, None)),
    )
    errors = []
    for url, parse in providers:
        try:
            country, region, city, org = parse(get(url))
        except Exception as e:  # noqa: BLE001
            errors.append(type(e).__name__)
            continue
        if not country:
            continue
        where = ", ".join(x for x in (city, region, country) if x) + (f"（{org}）" if org else "")
        if country in ("US", "CU", "IR", "KP", "SY") or (country == "CA" and (region or "") in ("Ontario", "")):
            return "!", (f"云电脑出口 IP 在 {where}：Hyperliquid 条款限制美国、加拿大安大略和受制裁地区的用户，"
                         "paper 模式只读行情，实盘前请自己确认合规")
        return "✔", f"云电脑出口 IP 在 {where}"
    return "!", f"查不到出口 IP 位置（{', '.join(errors) or '无结果'}）"


def smoke_test() -> tuple[str, str]:
    """Replay the bundled NewsLiquid fixture through the full pipeline in a scratch directory (mainnet prices)."""
    import yaml

    replay = os.path.join(ROOT, "data", "replay.jsonl")
    if not os.path.exists(replay):
        return "!", "没有 data/replay.jsonl，跳过回放自检"
    tmp = tempfile.mkdtemp(prefix="hlbot-smoke-")
    try:
        cfg = load_cfg()
        cfg["mode"] = "paper"
        cfg["storage"] = {"sqlite_path": os.path.join(tmp, "smoke.db"), "log_path": os.path.join(tmp, "smoke.log")}
        feeds = cfg.setdefault("feeds", {})
        for k in ("hl_listings", "macro", "newsliquid"):
            feeds.setdefault(k, {})["enabled"] = False
        if isinstance(cfg.get("alerts"), dict):
            cfg["alerts"]["telegram"] = False
            cfg["alerts"]["macos_notification"] = False
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "HL_", "OPENNEWS"))}
        try:
            r = subprocess.run([venv_python(), "-m", "hlnews.run", "--config", path, "--replay", replay, "--speed", "0",
                                "--duration", "12", "--paper"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return "✘", "回放自检超时（120 秒）"
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            return "✘", f"回放自检失败（退出码 {r.returncode}）：\n" + "\n".join(out.strip().splitlines()[-8:])
        lines = (r.stdout or "").splitlines()   # run.py ends with the summary JSON; nested dicts are indented
        try:
            i = max(k for k, x in enumerate(lines) if x == "{")
            j = max(k for k, x in enumerate(lines) if x == "}")
            summary = json.loads("\n".join(lines[i:j + 1]))
        except ValueError:
            summary = {}
        return "✔", (f"回放自检通过：{summary.get('news', '?')} 条新闻、{summary.get('signals', '?')} 个信号、"
                     f"{summary.get('closed', '?')} 笔模拟平仓（用的是主网实时价格）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_doctor(a) -> int:
    rows: list[tuple[str, str]] = []
    py = venv_python()
    v = subprocess.run([py, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"], capture_output=True, text=True)
    ver = v.stdout.strip()
    ok_ver = ver and tuple(int(x) for x in ver.split(".")) >= (3, 10)
    rows.append(("✔" if ok_ver else "✘", f"Python {ver or '?'}（{py}）" + ("" if ok_ver else "：需要 3.10 以上")))
    imp = subprocess.run([py, "-c", "import hyperliquid, websockets, yaml, eth_account"], capture_output=True, text=True)
    rows.append(("✔", "依赖已装") if imp.returncode == 0 else
                ("✘", "依赖缺失：" + (imp.stderr.strip().splitlines() or ["?"])[-1] + "；运行 .venv/bin/pip install -r requirements.txt"))
    try:
        cfg = load_cfg()
        nl = (cfg.get("feeds") or {}).get("newsliquid") or {}
        rows.append(("✔", f"config.yaml 可读：mode {cfg.get('mode', 'paper')}；NewsLiquid {plan_estimate(nl)}"))
    except Exception as e:  # noqa: BLE001
        cfg, nl = {}, {}
        rows.append(("✘", f"config.yaml 读不了：{e}"))
    env = env_summary()
    if not env["exists"]:
        rows.append(("✘", ".env 不存在：运行 hlbot setup，或从 .env.example 复制"))
    else:
        mode_bits = os.stat(ENV_FILE).st_mode & 0o077
        rows.append(("✔" if not mode_bits else "!", ".env 存在" + ("（权限 600）" if not mode_bits else "，但其他用户可读：chmod 600 .env")))
        rows.append(("✔", "OPENNEWS_TOKEN 已填") if env["OPENNEWS_TOKEN"] else
                    ("✘", "OPENNEWS_TOKEN 没填：在 Memoh 的 Files 标签页编辑 .env，填 OPENNEWS_TOKEN=...（保存后自动生效）"))
        if cfg.get("mode") == "live" or load_opts().get("live"):
            rows.append(("✔", "实盘密钥已填") if env["HL_AGENT_KEY"] and env["HL_ACCOUNT"] else
                        ("!", "实盘需要 HL_AGENT_KEY 和 HL_ACCOUNT（现在会按 paper 运行或启动失败）"))
        rows.append(("✔" if env["TELEGRAM"] else "!", "Telegram 告警" + ("已配置" if env["TELEGRAM"] else "未配置：重启、卡死等不会推送给你")))
    hk = hooks_installed()
    if os.path.isdir(os.path.dirname(HOOKS_FILE)) or hk["file"]:
        miss = {"WorkspaceStart", "SessionStart", "WorkspaceStop"} - set(hk["ours"])
        rows.append(("✔" if not miss and hk["enabled"] else "!", "Memoh hooks " + ("已安装" if not miss else "缺 " + "/".join(sorted(miss)) + "：运行 hlbot hooks")
                     + ("" if hk["enabled"] else "；hooks.json 顶层 enabled=false")))
        skill = os.path.exists(os.path.join(SKILLS_DIR, SKILL_NAME, "SKILL.md"))
        rows.append(("✔" if skill else "!", "agent 技能 " + ("已安装" if skill else "未安装：运行 hlbot skill")))
    else:
        rows.append(("!", "不在 Memoh 工作区（没有 /data/.memoh）：hooks 和技能跳过，靠守护进程本身保活"))
    du = shutil.disk_usage(DATA if os.path.isdir(DATA) else ROOT)
    db = os.path.join(DATA, "hlnews.db")
    db_mb = os.path.getsize(db) / 1e6 if os.path.exists(db) else 0
    rows.append(("✔" if du.free > 1e9 else "!", f"磁盘剩余 {du.free / 1e9:.1f} GB；数据库 {db_mb:.1f} MB"))
    level, line = health(collect())
    rows.append(({"OK": "✔", "STOPPED": "!", "STARTING": "!"}.get(level, "✘"), f"运行状态 {level}：{line}"))
    log = tail_file(os.path.join(DATA, "hlnews.log"), 400).splitlines()
    errs = [x for x in log if " ERROR " in x or "Traceback" in x]
    if errs:
        rows.append(("!", f"最近日志里有 {len(errs)} 行错误，最后一行：{errs[-1][:200]}"))
    if a.net:
        token = read_dotenv(ENV_FILE).get(str(nl.get("token_env") or "OPENNEWS_TOKEN"), "")
        if token:
            rows.append(check_newsliquid_rest(token, nl))
            if str(nl.get("transport", "auto")) in ("auto", "ws"):
                rows.append(check_newsliquid_ws(token, nl))
        else:
            rows.append(("✘", "没有 token，跳过 NewsLiquid 连通检查"))
        rows.append(check_hyperliquid(cfg))
        rows.append(check_location())
    for mark, text in rows:
        print(f"{mark} {text}")
    return 1 if any(m == "✘" for m, _ in rows) else 0


# --------------------------------------------------------------------------- setup / update
def prepare_env(token_from_env: str) -> str:
    """Create .env from .env.example (placeholders blanked, mode 600); put OPENNEWS_TOKEN in when it was passed in."""
    created = False
    if not os.path.exists(ENV_FILE):
        lines = []
        example = os.path.join(ROOT, ".env.example")
        if os.path.exists(example):
            with open(example, encoding="utf-8") as f:
                for line in f.read().splitlines():
                    k, _, v = line.partition("=")
                    lines.append(f"{k}=" if not line.lstrip().startswith("#") and _ and v.strip() in ("0x...", "...") else line)
        else:
            lines = ["OPENNEWS_TOKEN=", "HL_AGENT_KEY=", "HL_ACCOUNT=", "TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID="]
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        created = True
    os.chmod(ENV_FILE, 0o600)
    if token_from_env:
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        done = False
        for i, line in enumerate(lines):
            if line.strip().startswith("OPENNEWS_TOKEN="):
                lines[i] = f"OPENNEWS_TOKEN={token_from_env}"
                done = True
        if not done:
            lines.append(f"OPENNEWS_TOKEN={token_from_env}")
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return "已创建 .env 并写入 OPENNEWS_TOKEN（没有打印出来）" if created else "已把 OPENNEWS_TOKEN 写入 .env（没有打印出来）"
    return "已创建 .env（权限 600）" if created else ".env 已存在，未改动"


def on_memoh() -> bool:
    return os.path.isdir("/data/.memoh") or bool(os.environ.get("MEMOH_BOT_ID"))


def cmd_setup(a) -> int:
    print(f"== hl-news-bot 部署：{ROOT}")
    os.makedirs(DATA, exist_ok=True)
    print("✔ " + prepare_env(os.environ.get("OPENNEWS_TOKEN", "").strip()))
    if a.plan:
        ok, note = set_plan(a.plan, a.poll_s)
        print(("✔ NewsLiquid 套餐 " + a.plan + "：" + note) if ok else "✘ " + note)
    opts = load_opts()
    if a.no_ui:
        opts["ui"] = False
    save_opts({k: opts[k] for k in ("live", "ui", "ui_port")})
    memoh = on_memoh() or bool(a.hooks_file)
    if not a.no_hooks:
        if memoh:
            ok, msg = install_hooks(a.hooks_file or HOOKS_FILE)
            print(("✔ " if ok else "✘ ") + msg)
        else:
            print("! 不在 Memoh 工作区，跳过 hooks（守护进程照样保活；Memoh 上请重跑 hlbot hooks）")
    if not a.no_skill:
        if memoh or a.skills_dir:
            ok, msg = install_skill(a.skills_dir or SKILLS_DIR)
            print(("✔ " if ok else "✘ ") + msg)
        else:
            print("! 不在 Memoh 工作区，跳过 agent 技能")
    for link_dir in ("/usr/local/bin", os.path.expanduser("~/.local/bin")):
        link = os.path.join(link_dir, "hlbot")
        try:
            if os.path.isdir(link_dir) and os.access(link_dir, os.W_OK):
                if os.path.islink(link) or not os.path.exists(link):
                    if os.path.islink(link):
                        os.remove(link)
                    os.symlink(HLBOT, link)
                    print(f"✔ 命令 hlbot → {link}（重建工作区后可能失效，hooks 用的是绝对路径 {HLBOT}）")
                    break
        except OSError:
            continue
    if not a.no_smoke:
        print("… 回放自检（用主网实时价格跑一遍内置的 NewsLiquid 样本，约 20 秒）")
        mark, text = smoke_test()
        print(f"{mark} {text}")
        if mark == "✘":
            print("自检失败，先不启动；修好后运行 hlbot start")
            return 1
    token = env_summary()["OPENNEWS_TOKEN"]
    if not a.no_net:
        nl = (load_cfg().get("feeds") or {}).get("newsliquid") or {}
        if token:
            for mark, text in (check_newsliquid_rest(read_dotenv(ENV_FILE).get("OPENNEWS_TOKEN", ""), nl), check_location()):
                print(f"{mark} {text}")
        else:
            print(f"! {check_location()[1]}")
    if not token:
        print("! 还没填 OPENNEWS_TOKEN：bot 会先跑起来（只有 HL 上新和宏观数据）。在 Memoh 的 Files 标签页打开\n"
              f"  {ENV_FILE}，填 OPENNEWS_TOKEN=你的 token 并保存，守护进程几秒内自动重启 bot 生效")
    rc = 0
    if not a.no_start:
        rc = start(opts)
    print("\n下一步：")
    print(f"  · 查看状态：{hlbot_cmd()} status（装了 hlbot 命令就直接 hlbot status）")
    print("  · 日志：hlbot logs    · 全面检查：hlbot doctor --net（调用一次 NewsLiquid，扣 1 次额度）")
    print("  · 一定要让 Memoh agent 建一个每小时的定时任务（README 第 16 节）：工作区被重启后，靠它触发的新会话把 bot 拉起来")
    print("  · 在 Workspace 标签页点 Stop 之前先运行 hlbot stop：那个按钮会直接杀进程，不会先平仓")
    return rc


def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=check)


def cmd_update(a) -> int:
    """Fast-forward to origin/<branch> (or switch to --branch), keeping local edits; on a conflict the local version of
    that file wins. pip install, import check (rolls back on failure), then restart while flat."""
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        print("✘ 不是 git 仓库（用压缩包装的？）：重跑 install.sh 更新")
        return 1
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    target = a.branch or branch
    old = git("rev-parse", "HEAD").stdout.strip()
    f = git("fetch", "--quiet", "origin", target)
    if f.returncode != 0:
        print(f"✘ git fetch 失败：{f.stderr.strip()[:300]}")
        return 1
    new = git("rev-parse", "FETCH_HEAD").stdout.strip()
    switching = target != branch
    if new == old and not switching:
        print(f"已是最新（{branch} @ {old[:7]}）")
        return 0
    if not switching and git("merge-base", "--is-ancestor", old, new).returncode != 0:
        print("✘ 本地有自己的提交，不能快进；请手动合并")
        return 1
    dirty = [ln[3:] for ln in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines() if ln.strip()]
    backup = os.path.join(DATA, "backup", time.strftime("%Y%m%d-%H%M%S"))
    for rel in dirty:
        os.makedirs(os.path.dirname(os.path.join(backup, rel)), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), os.path.join(backup, rel))

    def restore_backup() -> None:
        for rel in dirty:
            shutil.copy2(os.path.join(backup, rel), os.path.join(ROOT, rel))

    kept: list[str] = []
    if dirty:
        git("stash", "push", "--quiet", "-m", "hlbot update")
    m = git("checkout", "--quiet", "-B", target, new) if switching else git("merge", "--ff-only", "--quiet", new)
    if m.returncode != 0:
        if dirty:
            git("stash", "pop", "--quiet")
        print(f"✘ 更新失败：{m.stderr.strip()[:300]}")
        return 1
    if dirty and git("stash", "pop", "--quiet").returncode != 0:
        # conflict: take the new version, then put the local files back (their settings win)
        git("reset", "--hard", "--quiet", new)
        restore_backup()
        kept = list(dirty)
        git("stash", "drop", "--quiet")
    pip = subprocess.run([venv_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                          os.path.join(ROOT, "requirements.txt")], capture_output=True, text=True)
    if pip.returncode != 0:
        print(f"! pip install 出错：{pip.stderr.strip()[-300:]}")
    chk = subprocess.run([venv_python(), "-c", "import hlnews.run, hlnews.ui"], cwd=ROOT, capture_output=True, text=True)
    if chk.returncode != 0:
        if switching:
            git("checkout", "--quiet", "-f", "-B", branch, old)
        else:
            git("reset", "--hard", "--quiet", old)
        restore_backup()
        print(f"✘ 新版本导入失败，已回滚到 {old[:7]}：{chk.stderr.strip()[-300:]}")
        return 1
    if switching:
        print(f"✔ 已切换到 {target} @ {new[:7]}（原来是 {branch} @ {old[:7]}）")
    else:
        log = git("log", "--oneline", f"{old}..{new}").stdout.strip()
        print(f"✔ 已更新 {old[:7]} → {new[:7]}：\n{log}")
    if kept:
        print(f"! 这些文件你本地改过且和新版冲突，保留了你的版本（新版对它们的改动没合进来，备份在 {backup}）：{', '.join(kept)}")
    elif dirty:
        print(f"✔ 本地修改已保留（{', '.join(dirty)}）")
    plan = (load_json(OPTS_FILE, {}) or {}).get("plan")
    if plan in PLANS and not kept:
        set_plan(plan)
    c = collect()
    if not (c["supervisor"]["alive"] or c["bot"]["running"]):
        print("bot 没在运行，未重启")
        return 0
    if a.no_restart:
        print("未重启（--no-restart）：hlbot restart 后新代码生效")
        return 0
    open_n = len((c["bot"]["state"] or {}).get("open") or [])
    if open_n:
        print(f"有 {open_n} 个持仓：为了不被平仓没有自动重启；空仓后运行 hlbot restart")
        return 0
    print("重启守护进程和 bot 以加载新代码（当前空仓）")
    return cmd_restart(argparse.Namespace(live=False, paper=False, ui=None, ui_port=None))


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hlbot", description="run hl-news-bot on a Memoh cloud computer and keep it alive")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def mode_flags(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--live", action="store_true", help="allow live trading (config.yaml must also say mode: live)")
        g.add_argument("--paper", action="store_true", help="force paper mode (default)")
        u = p.add_mutually_exclusive_group()
        u.add_argument("--ui", dest="ui", action="store_const", const=True, default=None, help="keep the console running")
        u.add_argument("--no-ui", dest="ui", action="store_const", const=False, help="no console")
        p.add_argument("--ui-port", type=int, default=None)

    p = sub.add_parser("setup", help="first-time setup (install.sh runs this)")
    p.add_argument("--plan", choices=sorted(PLANS))
    p.add_argument("--poll-s", type=float, default=None)
    p.add_argument("--no-ui", action="store_true")
    p.add_argument("--no-hooks", action="store_true")
    p.add_argument("--no-skill", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-smoke", action="store_true")
    p.add_argument("--no-net", action="store_true")
    p.add_argument("--hooks-file", default="")
    p.add_argument("--skills-dir", default="")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("start")
    mode_flags(p)
    p.set_defaults(fn=cmd_start)
    p = sub.add_parser("stop")
    p.add_argument("--hook", action="store_true", help="called by the WorkspaceStop hook: do not mark as stopped by the user")
    p.add_argument("--timeout", type=float, default=0.0)
    p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("restart")
    mode_flags(p)
    p.set_defaults(fn=cmd_restart)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("ensure")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--hook", action="store_true", help="called by a Memoh hook: return at once, always exit 0")
    p.set_defaults(fn=cmd_ensure)
    p = sub.add_parser("logs")
    p.add_argument("which", nargs="?", default="all", choices=["all", "bot", "app", "sup", "ui"])
    p.add_argument("-n", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("doctor")
    p.add_argument("--net", action="store_true", help="also call NewsLiquid (1 quota), Hyperliquid and ipinfo.io")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("plan")
    p.add_argument("name", nargs="?", choices=sorted(PLANS))
    p.add_argument("--poll-s", type=float, default=None)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("hooks")
    p.add_argument("--remove", action="store_true")
    p.add_argument("--file", default="")
    p.set_defaults(fn=cmd_hooks)
    p = sub.add_parser("skill")
    p.add_argument("--remove", action="store_true")
    p.add_argument("--dir", default="")
    p.set_defaults(fn=cmd_skill)
    p = sub.add_parser("update")
    p.add_argument("--branch", default="", help="switch to this branch (default: stay on the current one)")
    p.add_argument("--no-restart", action="store_true")
    p.set_defaults(fn=cmd_update)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        if getattr(a, "hook", False):   # a hook must never fail loudly
            print(f"hlbot {a.cmd} error: {type(e).__name__}: {e}")
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main())
