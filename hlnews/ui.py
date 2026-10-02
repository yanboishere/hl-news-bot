"""Local web UI backend for hl-news-bot.

    python -m hlnews.ui              # http://127.0.0.1:8765
    python -m hlnews.ui --port 9000 --repo /path/to/hl-news-bot

Serves the single-page console from hlnews/ui_static/index.html and a small JSON API over the SQLite store,
the state.json heartbeat and the log file. It can also start/stop the bot as a child process, run the replay
fixture, run the event backtester, and edit config.yaml.

Binds to 127.0.0.1 only. No authentication: this is meant to be opened by the macOS shell app (a WKWebView)
or a browser on the same machine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "ui_static")


# --------------------------------------------------------------------------- process management
class BotProcess:
    """Owns at most one child `python -m hlnews.run` (the bot) and at most one one-shot child (replay/backtest)."""

    def __init__(self, repo: str, python: str):
        self.repo = repo
        self.python = python
        self.proc: subprocess.Popen | None = None
        self.started_ms = 0
        self.oneshot: subprocess.Popen | None = None
        self.oneshot_kind = ""
        self.oneshot_out = ""
        self.oneshot_started_ms = 0
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, extra_args: list[str] | None = None) -> dict:
        with self.lock:
            if self.running():
                return {"ok": False, "error": "bot already running", "pid": self.proc.pid}
            os.makedirs(os.path.join(self.repo, "data"), exist_ok=True)
            out = open(os.path.join(self.repo, "data", "bot.out"), "ab")
            cmd = [self.python, "-m", "hlnews.run", *(extra_args or [])]
            self.proc = subprocess.Popen(cmd, cwd=self.repo, stdout=out, stderr=subprocess.STDOUT, env=os.environ.copy(),
                                         start_new_session=True)
            self.started_ms = int(time.time() * 1000)
            return {"ok": True, "pid": self.proc.pid, "cmd": cmd}

    def stop(self, timeout_s: float = 15.0) -> dict:
        with self.lock:
            if not self.running():
                self.proc = None
                return {"ok": True, "note": "not running"}
            p = self.proc
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGINT)  # SIGINT -> engine.shutdown() flattens positions
            except ProcessLookupError:
                pass
            t0 = time.time()
            while p.poll() is None and time.time() - t0 < timeout_s:
                time.sleep(0.2)
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(1.0)
            if p.poll() is None:
                p.kill()
            rc = p.poll()
            self.proc = None
            return {"ok": True, "returncode": rc}

    def status(self) -> dict:
        return {
            "running": self.running(),
            "pid": self.proc.pid if self.running() else None,
            "started_ms": self.started_ms if self.running() else None,
            "oneshot": {"kind": self.oneshot_kind, "running": self.oneshot is not None and self.oneshot.poll() is None,
                        "started_ms": self.oneshot_started_ms} if self.oneshot_kind else None,
            "env": {"HL_AGENT_KEY": bool(os.environ.get("HL_AGENT_KEY")), "HL_ACCOUNT": bool(os.environ.get("HL_ACCOUNT")),
                    "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")), "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY"))},
            "python": self.python, "repo": self.repo,
        }

    def run_oneshot(self, kind: str, args: list[str]) -> dict:
        with self.lock:
            if self.oneshot is not None and self.oneshot.poll() is None:
                return {"ok": False, "error": f"{self.oneshot_kind} still running"}
            if kind == "replay" and self.running():
                return {"ok": False, "error": "stop the bot before replaying (both would write the same database)"}
            self.oneshot_kind = kind
            self.oneshot_out = ""
            self.oneshot_started_ms = int(time.time() * 1000)
            self.oneshot = subprocess.Popen([self.python, "-m", *args], cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, env=os.environ.copy())
            threading.Thread(target=self._drain, args=(self.oneshot,), daemon=True).start()
            return {"ok": True, "pid": self.oneshot.pid}

    def _drain(self, p: subprocess.Popen) -> None:
        assert p.stdout is not None
        for line in p.stdout:
            self.oneshot_out += line
            if len(self.oneshot_out) > 400_000:
                self.oneshot_out = self.oneshot_out[-300_000:]

    def oneshot_result(self) -> dict:
        running = self.oneshot is not None and self.oneshot.poll() is None
        return {"kind": self.oneshot_kind, "running": running, "output": self.oneshot_out,
                "returncode": None if running or self.oneshot is None else self.oneshot.poll()}


def _yaml_scalar(value) -> str:
    """Render a scalar the way it would appear inline in YAML (no document markers)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if value is None:
        return "null"
    s = str(value)
    return s if re.match(r"^[A-Za-z0-9_./:-]+$", s) else json.dumps(s, ensure_ascii=False)


# --------------------------------------------------------------------------- data access
class Data:
    def __init__(self, repo: str):
        self.repo = repo
        self.cfg_path = os.path.join(repo, "config.yaml")

    def cfg(self) -> dict:
        with open(self.cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def db_path(self) -> str:
        p = self.cfg()["storage"]["sqlite_path"]
        return p if os.path.isabs(p) else os.path.join(self.repo, p)

    def log_path(self) -> str:
        p = self.cfg()["storage"]["log_path"]
        return p if os.path.isabs(p) else os.path.join(self.repo, p)

    def conn(self) -> sqlite3.Connection | None:
        p = self.db_path()
        if not os.path.exists(p):
            return None
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
        c.row_factory = sqlite3.Row
        return c

    def state(self) -> dict:
        p = os.path.join(os.path.dirname(self.db_path()), "state.json")
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            st = {}
        now = int(time.time() * 1000)
        st["age_ms"] = now - int(st.get("ts_ms") or 0) if st.get("ts_ms") else None
        st["alive"] = bool(st.get("ts_ms")) and not st.get("stopped") and (now - st["ts_ms"]) < 10_000
        return st

    # ---- queries
    def summary(self) -> dict:
        c = self.conn()
        if c is None:
            return {"empty": True}
        day0 = int(time.time() // 86400 * 86400 * 1000)  # UTC midnight, matches the bot's daily reset
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "today": {
                "news": q("select count(*) from news where recv_ms>=?", day0),
                "directional": q("select count(distinct news_id) from classifications where created_ms>=? and direction!='none'", day0),
                "signals": q("select count(*) from signals where created_ms>=?", day0),
                "accepted": q("select count(*) from decisions where created_ms>=? and accepted=1", day0),
                "closed": q("select count(*) from positions where exit_ms>=?", day0),
                "pnl": q("select coalesce(sum(pnl_usd),0) from positions where exit_ms>=?", day0),
            },
            "all": {
                "news": q("select count(*) from news"), "signals": q("select count(*) from signals"),
                "closed": q("select count(*) from positions where exit_ms is not null"),
                "wins": q("select count(*) from positions where exit_ms is not null and pnl_usd>0"),
                "pnl": q("select coalesce(sum(pnl_usd),0) from positions where exit_ms is not null"),
                "fees": q("select coalesce(sum(fee_usd),0) from positions where exit_ms is not null"),
            },
            "rejections": [dict(r) for r in c.execute(
                "select reason, count(*) n from decisions where accepted=0 group by reason order by n desc limit 12")],
            "by_class": [dict(r) for r in c.execute(
                "select event_class, count(*) n, sum(case when pnl_usd>0 then 1 else 0 end) wins, round(sum(pnl_usd),2) pnl, "
                "sum(case when exit_reason='stop' then 1 else 0 end) stops, sum(case when exit_reason='take_profit' then 1 else 0 end) tps, "
                "sum(case when exit_reason='time_stop' then 1 else 0 end) times from positions where exit_ms is not null group by 1 order by n desc")],
            "sources": [dict(r) for r in c.execute(
                "select channel, count(*) n, round(avg(age_s),2) avg_age, round(max(age_s),1) max_age from news where recv_ms>=? group by 1 order by n desc",
                (int(time.time() * 1000) - 86_400_000,))],
        }
        c.close()
        return out

    def tape(self, limit: int = 150, before_ms: int | None = None, only_directional: bool = False) -> list[dict]:
        c = self.conn()
        if c is None:
            return []
        where = "where 1=1"
        args: list = []
        if before_ms:
            where += " and n.recv_ms < ?"
            args.append(before_ms)
        if only_directional:
            where += " and exists (select 1 from classifications c where c.news_id=n.id and c.direction!='none')"
        rows = [dict(r) for r in c.execute(
            f"select n.id, n.source, n.channel, n.account, n.title, n.url, n.ts_ms, n.recv_ms, n.age_s, n.suggestions from news n {where} "
            f"order by n.recv_ms desc limit ?", (*args, limit))]
        ids = [r["id"] for r in rows]
        if not ids:
            c.close()
            return []
        ph = ",".join("?" for _ in ids)
        cls: dict[str, list] = {}
        for r in c.execute(f"select * from classifications where news_id in ({ph}) order by created_ms", ids):
            d = dict(r)
            d["tickers"] = json.loads(d.get("tickers") or "[]")
            cls.setdefault(d["news_id"], []).append(d)
        sigs: dict[str, list] = {}
        for r in c.execute(f"select * from signals where news_id in ({ph}) order by created_ms", ids):
            d = dict(r)
            d["template"] = json.loads(d.get("template") or "{}")
            sigs.setdefault(d["news_id"], []).append(d)
        sig_ids = [s["id"] for lst in sigs.values() for s in lst]
        decs: dict[str, list] = {}
        poss: dict[str, dict] = {}
        if sig_ids:
            ph2 = ",".join("?" for _ in sig_ids)
            for r in c.execute(f"select * from decisions where signal_id in ({ph2}) order by created_ms", sig_ids):
                decs.setdefault(r["signal_id"], []).append(dict(r))
            for r in c.execute(f"select * from positions where signal_id in ({ph2})", sig_ids):
                poss[r["signal_id"]] = dict(r)
        for r in rows:
            r["suggestions"] = json.loads(r.get("suggestions") or "[]")
            r["classifications"] = cls.get(r["id"], [])
            r["signals"] = sigs.get(r["id"], [])
            for s in r["signals"]:
                s["decisions"] = decs.get(s["id"], [])
                s["position"] = poss.get(s["id"])
            final = r["classifications"][-1] if r["classifications"] else None
            r["verdict"] = {
                "event_class": final["event_class"] if final else None,
                "direction": final["direction"] if final else None,
                "confidence": final["confidence"] if final else None,
                "lane": final["lane"] if final else None,
                "reason": final["reason"] if final else None,
                "is_rumor": bool(final["is_rumor"]) if final else False,
                "outcome": self._outcome(r),
            }
        c.close()
        return rows

    @staticmethod
    def _outcome(r: dict) -> str:
        if not r["classifications"]:
            return "未分类"
        f = r["classifications"][-1]
        if f["event_class"] == "noise":
            return "噪音"
        if f["is_rumor"]:
            return "谣言，跳过"
        if f["direction"] == "none":
            return "无方向"
        if not r["signals"]:
            return "等待佐证或无模板"
        for s in r["signals"]:
            if s.get("position"):
                p = s["position"]
                return "已开仓" if p.get("exit_ms") is None else f"已平仓（{p.get('exit_reason')}）"
        for s in r["signals"]:
            for d in s.get("decisions", []):
                if not d["accepted"]:
                    if d["reason"].startswith("alert-only"):
                        return "已报警（模板不交易）"
                    return f"风控拒绝：{d['reason']}"
        return "信号"

    def positions(self, status: str = "open", limit: int = 100) -> list[dict]:
        c = self.conn()
        if c is None:
            return []
        if status == "open":
            rows = c.execute("select * from positions where exit_ms is null order by entry_ms desc limit ?", (limit,)).fetchall()
        else:
            rows = c.execute("select * from positions where exit_ms is not null order by exit_ms desc limit ?", (limit,)).fetchall()
        out = [dict(r) for r in rows]
        c.close()
        return out

    def equity(self, hours: float = 24.0) -> list[dict]:
        c = self.conn()
        if c is None:
            return []
        since = int(time.time() * 1000) - int(hours * 3600 * 1000)
        rows = [dict(r) for r in c.execute("select ts_ms, equity_usd, open_positions from equity where ts_ms>=? order by ts_ms", (since,))]
        c.close()
        return rows

    def alerts(self, limit: int = 50) -> list[dict]:
        c = self.conn()
        if c is None:
            return []
        try:
            rows = [dict(r) for r in c.execute("select * from alerts order by ts_ms desc limit ?", (limit,))]
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            try:
                r["meta"] = json.loads(r.get("meta") or "{}")
            except Exception:  # noqa: BLE001
                pass
        c.close()
        return rows

    def log_tail(self, lines: int = 200, grep: str = "") -> list[str]:
        p = self.log_path()
        if not os.path.exists(p):
            return []
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 512_000)
            f.seek(size - chunk)
            text = f.read().decode("utf-8", "ignore")
        out = text.splitlines()
        if grep:
            g = grep.lower()
            out = [l for l in out if g in l.lower()]
        return out[-lines:]

    # ---- config
    def config_text(self) -> str:
        with open(self.cfg_path, encoding="utf-8") as f:
            return f.read()

    def save_config_text(self, text: str) -> dict:
        try:
            parsed = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"YAML 解析失败：{e}"}
        for k in ("mode", "hyperliquid", "account", "universe", "feeds", "classifier", "templates", "storage"):
            if k not in parsed:
                return {"ok": False, "error": f"缺少顶层键 {k}"}
        bak = self.cfg_path + ".bak"
        try:
            with open(self.cfg_path, encoding="utf-8") as f, open(bak, "w", encoding="utf-8") as b:
                b.write(f.read())
        except Exception:  # noqa: BLE001
            pass
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(text)
        return {"ok": True}

    def patch_config(self, patch: dict) -> dict:
        """Apply a shallow set of edits to config.yaml by rewriting scalar lines in place so comments survive.
        Supported keys: mode, account.*, classifier.llm.enabled, classifier.llm.model, templates.<class>.<field>,
        feeds.<name>.enabled, hyperliquid.network, classifier.rules.trusted_accounts (list)."""
        import re

        text = self.config_text()
        lines = text.splitlines()

        def set_scalar(path: list[str], value) -> bool:
            # walk indentation-based structure
            depth = 0
            idx = 0
            i = 0
            while i < len(lines) and idx < len(path):
                line = lines[i]
                stripped = line.lstrip(" ")
                ind = len(line) - len(stripped)
                if stripped.startswith("#") or not stripped:
                    i += 1
                    continue
                if ind == depth * 2 and re.match(rf"^{re.escape(path[idx])}\s*:", stripped):
                    if idx == len(path) - 1:
                        # scalar or inline map on this line
                        m = re.match(r"^(\s*" + re.escape(path[idx]) + r"\s*:\s*)([^#]*?)(\s*#.*)?$", line)
                        if m:
                            lines[i] = f"{m.group(1)}{_yaml_scalar(value)}{m.group(3) or ''}"
                            return True
                        return False
                    depth += 1
                    idx += 1
                i += 1
            return False

        def set_template_field(cls: str, field: str, value) -> bool:
            for i, line in enumerate(lines):
                m = re.match(rf"^(\s*{re.escape(cls)}\s*:\s*)(\{{.*\}})(\s*#.*)?$", line)
                if m:
                    d = yaml.safe_load(m.group(2))
                    d[field] = value
                    body = "{" + ", ".join(f"{k}: {v}" for k, v in d.items()) + "}"
                    lines[i] = f"{m.group(1)}{body}{m.group(3) or ''}"
                    return True
            return False

        failed = []
        for key, value in patch.items():
            parts = key.split(".")
            ok = False
            if parts[0] == "templates" and len(parts) == 3:
                ok = set_template_field(parts[1], parts[2], value)
            elif key == "classifier.rules.trusted_accounts" and isinstance(value, list):
                # rewrite the list block
                start = next((i for i, l in enumerate(lines) if re.match(r"^\s{4}trusted_accounts\s*:", l)), None)
                if start is not None:
                    j = start + 1
                    while j < len(lines) and (lines[j].startswith("      - ") or not lines[j].strip() or lines[j].strip().startswith("#") and lines[j].startswith("      ")):
                        j += 1
                    new = [f"      - {v}" for v in value]
                    lines[start + 1 : j] = new
                    ok = True
            else:
                ok = set_scalar(parts, value)
            if not ok:
                failed.append(key)
        new_text = "\n".join(lines) + "\n"
        try:
            yaml.safe_load(new_text)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"修改后 YAML 无效：{e}", "failed": failed}
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        return {"ok": not failed, "failed": failed}


# --------------------------------------------------------------------------- backtest (in-process, threaded)
class BacktestRunner:
    def __init__(self, repo: str):
        self.repo = repo
        self.lock = threading.Lock()
        self.running = False
        self.result: dict | None = None
        self.error: str | None = None
        self.started_ms = 0

    def start(self, events_path: str, delay: int, sweep: bool) -> dict:
        with self.lock:
            if self.running:
                return {"ok": False, "error": "backtest already running"}
            self.running = True
            self.result = None
            self.error = None
            self.started_ms = int(time.time() * 1000)
        threading.Thread(target=self._run, args=(events_path, delay, sweep), daemon=True).start()
        return {"ok": True}

    def _run(self, events_path: str, delay: int, sweep: bool) -> None:
        try:
            sys.path.insert(0, self.repo)
            from hlnews import backtest as bt

            cfg = yaml.safe_load(open(os.path.join(self.repo, "config.yaml"), encoding="utf-8"))
            p = events_path if os.path.isabs(events_path) else os.path.join(self.repo, events_path)
            events = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip() and not l.startswith("#")]
            out: dict = {"events_file": events_path, "n_events": len(events)}
            if sweep:
                out["sweep"] = []
                for d in (5, 20, 60, 180, 300):
                    r = bt.run(events, cfg["templates"], d, verbose=False)
                    a = r.get("_all", {})
                    out["sweep"].append({"delay_s": d, **a})
            rows: list[dict] = []
            r = bt.run(events, cfg["templates"], delay, verbose=False, rows_out=rows)
            out["delay_s"] = delay
            out["by_class"] = {k: v for k, v in r.items() if k != "_all"}
            out["all"] = r.get("_all", {})
            out["rows"] = rows
            self.result = out
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False

    def status(self) -> dict:
        return {"running": self.running, "result": self.result, "error": self.error, "started_ms": self.started_ms}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    data: Data
    bot: BotProcess
    bt: BacktestRunner

    def log_message(self, fmt, *args):  # quiet
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if p == "/api/state":
                return self._json({"state": self.data.state(), "bot": self.bot.status()})
            if p == "/api/summary":
                return self._json(self.data.summary())
            if p == "/api/tape":
                return self._json(self.data.tape(limit=int(q.get("limit", 150)), before_ms=int(q["before_ms"]) if q.get("before_ms") else None,
                                                 only_directional=q.get("directional") == "1"))
            if p == "/api/positions":
                return self._json(self.data.positions(status=q.get("status", "open"), limit=int(q.get("limit", 100))))
            if p == "/api/equity":
                return self._json(self.data.equity(hours=float(q.get("hours", 24))))
            if p == "/api/log":
                return self._json(self.data.log_tail(lines=int(q.get("lines", 200)), grep=q.get("grep", "")))
            if p == "/api/config":
                return self._json({"text": self.data.config_text(), "parsed": self.data.cfg()})
            if p == "/api/oneshot":
                return self._json(self.bot.oneshot_result())
            if p == "/api/backtest":
                return self._json(self.bt.status())
            if p == "/api/alerts":
                return self._json(self.data.alerts(limit=int(q.get("limit", 50))))
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):  # noqa: N802
        p = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if p == "/api/bot/start":
                args = []
                if body.get("paper"):
                    args.append("--paper")
                return self._json(self.bot.start(args))
            if p == "/api/bot/stop":
                return self._json(self.bot.stop())
            if p == "/api/replay":
                dur = int(body.get("duration", 15))
                return self._json(self.bot.run_oneshot("replay", ["hlnews.run", "--replay", body.get("path", "data/replay.jsonl"), "--duration", str(dur), "--paper"]))
            if p == "/api/backtest":
                return self._json(self.bt.start(body.get("events", "data/events.jsonl"), int(body.get("delay", 20)), bool(body.get("sweep", False))))
            if p == "/api/config/text":
                return self._json(self.data.save_config_text(body.get("text", "")))
            if p == "/api/config/patch":
                return self._json(self.data.patch_config(body.get("patch", {})))
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--repo", default=os.path.dirname(HERE), help="repository root (contains config.yaml)")
    ap.add_argument("--python", default=sys.executable, help="python used to launch the bot")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    Handler.data = Data(repo)
    Handler.bot = BotProcess(repo, args.python)
    Handler.bt = BacktestRunner(repo)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"hl-news-bot console: http://127.0.0.1:{args.port}  (repo {repo})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.bot.stop()


if __name__ == "__main__":
    main()
