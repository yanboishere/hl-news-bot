"""hlbot control CLI (ops/memoh/ctl.py): hooks merge, skill, plan presets, .env creation, ensure/stop/start cycle."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "ops", "memoh")
sys.path.insert(0, ROOT)
sys.path.insert(0, OPS)

import ctl  # noqa: E402

from hlnews import instance  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_bot.py")
FAST = ["--tick", "0.1", "--stale-s", "1.0", "--grace-s", "1.5", "--backoff-min", "0.2", "--backoff-max", "1.6",
        "--stop-timeout-s", "2", "--no-telegram", "--bot-cmd", json.dumps([sys.executable, FAKE])]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


def wait_for(cond, timeout: float = 10.0, step: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


# --------------------------------------------------------------------------- hooks.json
def test_hooks_merge_keeps_user_hooks_and_is_idempotent(tmp_path):
    path = str(tmp_path / "hooks.json")
    user_hook = {"name": "review shell commands", "event": "PreToolUse", "matcher": "^exec$",
                 "actions": [{"type": "command", "command": "python3 /data/.memoh/review.py", "on_error": "block"}]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "enabled": True, "defaults": {"timeout": "10s", "on_error": "fail"},
                   "env": {"HOOK_LOG": "/data/.memoh/hooks.log"}, "hooks": [user_hook]}, f)
    for _ in range(2):
        ok, msg = ctl.install_hooks(path)
        assert ok, msg
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["defaults"] == {"timeout": "10s", "on_error": "fail"} and cfg["env"] == {"HOOK_LOG": "/data/.memoh/hooks.log"}
    assert cfg["hooks"][0] == user_hook
    ours = [h for h in cfg["hooks"] if h["name"].startswith(ctl.HOOK_PREFIX)]
    assert sorted(h["event"] for h in ours) == ["SessionStart", "WorkspaceStart", "WorkspaceStop"]
    for h in ours:
        (act,) = h["actions"]
        assert act["type"] == "command" and act["on_error"] == "ignore" and act["work_dir"] == ctl.ROOT
        assert act["command"].startswith("bash ") and os.path.join("ops", "memoh", "hlbot") in act["command"]
        assert act["command"].endswith("ensure --hook" if h["event"] != "WorkspaceStop" else "stop --hook")
    assert os.path.exists(path + ".bak-hlbot")
    ok, _ = ctl.install_hooks(path, remove=True)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["hooks"] == [user_hook]


def test_hooks_refuses_to_touch_invalid_json(tmp_path):
    path = str(tmp_path / "hooks.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    ok, msg = ctl.install_hooks(path)
    assert not ok and "没有改动" in msg
    with open(path, encoding="utf-8") as f:
        assert f.read() == "{ not json"


def test_hooks_file_created_when_missing(tmp_path):
    path = str(tmp_path / ".memoh" / "hooks.json")
    ok, _ = ctl.install_hooks(path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    assert ok and cfg["version"] == 1 and cfg["enabled"] is True and len(cfg["hooks"]) == 3


# --------------------------------------------------------------------------- skill, .env
def test_skill_is_rendered_with_paths(tmp_path):
    ok, _ = ctl.install_skill(str(tmp_path))
    with open(tmp_path / ctl.SKILL_NAME / "SKILL.md", encoding="utf-8") as f:
        text = f.read()
    assert ok and "{{" not in text and text.startswith("---\nname: hl-news-bot-ops\n")
    assert ctl.ROOT in text and "ensure --quiet" in text
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == ctl.SKILL_NAME and len(front["description"]) > 20


def test_prepare_env_blanks_placeholders_and_never_echoes_the_token(tmp_path, monkeypatch):
    shutil.copy(os.path.join(ROOT, ".env.example"), tmp_path / ".env.example")
    monkeypatch.setattr(ctl, "ROOT", str(tmp_path))
    monkeypatch.setattr(ctl, "ENV_FILE", str(tmp_path / ".env"))
    msg = ctl.prepare_env("secret-token-123")
    assert "secret-token-123" not in msg
    assert oct(os.stat(tmp_path / ".env").st_mode & 0o777) == "0o600"
    env = ctl.read_dotenv(str(tmp_path / ".env"))
    assert env["OPENNEWS_TOKEN"] == "secret-token-123" and env["HL_AGENT_KEY"] == "" and env["HL_ACCOUNT"] == ""
    assert ctl.prepare_env("") == ".env 已存在，未改动"
    assert ctl.read_dotenv(str(tmp_path / ".env"))["OPENNEWS_TOKEN"] == "secret-token-123"


# --------------------------------------------------------------------------- CLI in a scratch deployment
@pytest.fixture
def deploy(tmp_path):
    shutil.copy(os.path.join(ROOT, "config.yaml"), tmp_path / "config.yaml")
    os.makedirs(tmp_path / "data")
    with open(tmp_path / "data" / "supervisor.opts.json", "w", encoding="utf-8") as f:
        json.dump({"ui": False}, f)     # the real console needs the full repo; not under test here
    env = dict(os.environ, HLBOT_ROOT=str(tmp_path), HLNEWS_CODE=ROOT, HLBOT_SUPERVISOR_ARGS=json.dumps(FAST),
               MEMOH_HOOKS_FILE=str(tmp_path / "hooks.json"), MEMOH_SKILLS_DIR=str(tmp_path / "skills"))
    env.pop("OPENNEWS_TOKEN", None)

    def hlbot(*args: str, stdin: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", os.path.join(OPS, "hlbot"), *args], env=env, input=stdin, capture_output=True,
                              text=True, timeout=timeout)

    hlbot.path = str(tmp_path)
    hlbot.data = str(tmp_path / "data")
    yield hlbot
    hlbot("stop", "--timeout", "10")
    try:
        with open(tmp_path / "data" / "fake_starts", encoding="utf-8") as f:
            for row in f.read().splitlines():
                try:
                    os.kill(int(row.split()[0]), signal.SIGKILL)
                except OSError:
                    pass
    except OSError:
        pass


def test_plan_presets_edit_config_in_place(deploy):
    before = open(os.path.join(deploy.path, "config.yaml"), encoding="utf-8").read()
    r = deploy("plan", "pro")
    assert r.returncode == 0, r.stdout + r.stderr
    after = open(os.path.join(deploy.path, "config.yaml"), encoding="utf-8").read()
    nl = yaml.safe_load(after)["feeds"]["newsliquid"]
    assert (nl["transport"], nl["poll_s"], nl["fast_poll_s"], nl["max_pages"], nl["page_size"]) == ("rest", 20, 5, 3, 50)
    assert after.count("#") == before.count("#")             # comments survive
    assert "REST 每 20 秒一次" in r.stdout
    r = deploy("plan", "max")
    nl = yaml.safe_load(open(os.path.join(deploy.path, "config.yaml"), encoding="utf-8"))["feeds"]["newsliquid"]
    assert nl["transport"] == "auto" and nl["poll_s"] == 15
    with open(os.path.join(deploy.data, "supervisor.opts.json"), encoding="utf-8") as f:
        assert json.load(f)["plan"] == "max"
    assert deploy("start").returncode == 0 and deploy("stop").returncode == 0    # start rewrites the opts file
    with open(os.path.join(deploy.data, "supervisor.opts.json"), encoding="utf-8") as f:
        opts = json.load(f)
    assert opts["plan"] == "max" and opts["ui"] is False      # `hlbot update` re-applies the plan from here


def test_ensure_stop_start_cycle(deploy):
    r = deploy("ensure")
    assert r.returncode == 0 and r.stdout.startswith("RESTARTED"), r.stdout + r.stderr
    assert wait_for(lambda: instance.is_locked(deploy.data))
    assert wait_for(lambda: deploy("ensure", "--quiet").stdout.startswith("OK: bot pid"))

    r = deploy("stop")
    assert r.returncode == 0 and "已停止" in r.stdout, r.stdout + r.stderr
    assert os.path.exists(os.path.join(deploy.data, "supervisor.disabled"))
    assert not instance.is_locked(deploy.data) and not instance.is_locked(deploy.data, instance.SUPERVISOR_LOCK)

    r = deploy("ensure", "--hook", stdin='{"event":"SessionStart"}\n')   # a manual stop stays stopped
    assert r.returncode == 0 and r.stdout.startswith("STOPPED")
    time.sleep(0.5)
    assert not instance.is_locked(deploy.data, instance.SUPERVISOR_LOCK)

    r = deploy("start")
    assert r.returncode == 0 and "OK: bot pid" in r.stdout, r.stdout + r.stderr
    assert not os.path.exists(os.path.join(deploy.data, "supervisor.disabled"))
    st = json.loads(deploy("status", "--json").stdout)
    assert st["health"]["level"] == "OK" and st["supervisor"]["alive"] and st["bot"]["running"]

    r = deploy("stop", "--hook", stdin="{}\n")            # workspace stop: down, but not marked as a manual stop
    assert r.returncode == 0 and not os.path.exists(os.path.join(deploy.data, "supervisor.disabled"))
    assert not instance.is_locked(deploy.data)
    r = deploy("ensure", "--hook", stdin="{}\n")          # workspace start brings it back
    assert r.stdout.startswith("RESTARTED")
    assert wait_for(lambda: instance.is_locked(deploy.data))


def test_hook_mode_returns_fast_and_never_fails(deploy):
    t0 = time.time()
    r = deploy("ensure", "--hook", stdin=json.dumps({"event": "WorkspaceStart", "extra": {"x": "y" * 5000}}) + "\n")
    assert r.returncode == 0 and r.stdout.startswith("RESTARTED") and time.time() - t0 < 5
    assert wait_for(lambda: instance.is_locked(deploy.data))


def test_ensure_replaces_a_stuck_supervisor(deploy):
    assert deploy("start").returncode == 0
    sup_pid = instance.holder_pid(deploy.data, instance.SUPERVISOR_LOCK)
    bot_pid = instance.holder_pid(deploy.data)
    os.kill(sup_pid, signal.SIGSTOP)              # frozen: holds the lock, writes nothing
    try:
        path = os.path.join(deploy.data, "supervisor.json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st["updated"] = int(time.time()) - 1000
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        r = deploy("ensure")
        assert r.stdout.startswith("RESTARTED") and "卡住" in r.stdout, r.stdout + r.stderr
        new_sup = instance.holder_pid(deploy.data, instance.SUPERVISOR_LOCK)
        assert new_sup and new_sup != sup_pid
        # the bot kept running (own session) and the new supervisor watches it instead of starting another
        assert wait_for(lambda: json.load(open(path, encoding="utf-8")).get("adopted"))
        assert instance.holder_pid(deploy.data) == bot_pid
    finally:
        try:
            os.kill(sup_pid, signal.SIGKILL)
        except OSError:
            pass


def test_status_reports_down_and_setup_files(deploy):
    r = deploy("status")
    assert r.returncode == 1 and r.stdout.startswith("DOWN")
    assert "token 未填" in r.stdout
    assert deploy("hooks").returncode == 0 and deploy("skill").returncode == 0
    r = deploy("status")
    assert "Memoh hooks：已安装" in r.stdout and "agent 技能：已安装" in r.stdout


# --------------------------------------------------------------------------- hlbot update (git)
def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A tiny origin repo (stub hlnews package, config.yaml) and a clone of it as the deployment."""
    origin, work = tmp_path / "origin", tmp_path / "work"
    os.makedirs(origin / "hlnews")
    for name in ("__init__.py", "run.py", "ui.py"):
        (origin / "hlnews" / name).write_text("X = 1\n")
    (origin / "requirements.txt").write_text("")
    (origin / "config.yaml").write_text("mode: paper\nfeeds:\n  newsliquid:\n    poll_s: 15   # comment\n"
                                        + "".join(f"k{i}: {i}\n" for i in range(8)) + "other: 1\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(origin))
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(origin, "add", "-A")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "v1")
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    os.makedirs(work / "data")
    env = dict(os.environ, HLBOT_ROOT=str(work))

    def commit(path: str, text: str, msg: str) -> str:
        (origin / path).write_text(text)
        _git(origin, "add", "-A")
        _git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
        return _git(origin, "rev-parse", "HEAD")

    def update(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, os.path.join(OPS, "ctl.py"), "update", *args], env=env,
                              capture_output=True, text=True, timeout=120)

    return origin, work, commit, update


def test_update_keeps_local_edits_and_rolls_back_broken_code(upstream):
    origin, work, commit, update = upstream
    assert "已是最新" in update().stdout
    # a local edit on another line survives a fast-forward
    (work / "config.yaml").write_text((work / "config.yaml").read_text().replace("poll_s: 15", "poll_s: 20"))
    head = commit("config.yaml", (origin / "config.yaml").read_text().replace("other: 1", "other: 2"), "v2")
    r = update()
    assert r.returncode == 0 and "本地修改已保留" in r.stdout, r.stdout + r.stderr
    assert _git(work, "rev-parse", "HEAD") == head
    text = (work / "config.yaml").read_text()
    assert "poll_s: 20" in text and "other: 2" in text
    # same line changed upstream: the local version wins, the new commit is still checked out
    head = commit("config.yaml", (origin / "config.yaml").read_text().replace("poll_s: 15", "poll_s: 30"), "v3")
    r = update()
    assert "保留了你的版本" in r.stdout and _git(work, "rev-parse", "HEAD") == head, r.stdout + r.stderr
    assert "poll_s: 20" in (work / "config.yaml").read_text() and "<<<<" not in (work / "config.yaml").read_text()
    # a release that does not import is rolled back, local edits included
    good = head
    commit("hlnews/run.py", "raise ImportError('broken release')\n", "v4 broken")
    r = update()
    assert r.returncode == 1 and "已回滚" in r.stdout, r.stdout + r.stderr
    assert _git(work, "rev-parse", "HEAD") == good and "poll_s: 20" in (work / "config.yaml").read_text()
    # switching branches (install.sh --branch) works the same way
    _git(origin, "checkout", "-q", "-b", "feat", good)
    feat = commit("hlnews/ui.py", "X = 2\n", "feat")
    r = update("--branch", "feat")
    assert r.returncode == 0 and "已切换到 feat" in r.stdout, r.stdout + r.stderr
    assert _git(work, "rev-parse", "HEAD") == feat and _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "feat"
    assert "poll_s: 20" in (work / "config.yaml").read_text()
