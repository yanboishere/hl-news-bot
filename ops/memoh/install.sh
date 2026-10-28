#!/usr/bin/env bash
# Install hl-news-bot on a Memoh cloud computer, start it, and keep it running.
#
#   curl -fsSL https://raw.githubusercontent.com/yanboishere/hl-news-bot/main/ops/memoh/install.sh | bash -s -- --plan pro
#
# Safe to re-run: it updates the code (when you have no local edits) and redoes the setup.
#
#   --dir DIR        install directory (default /data/hl-news-bot; /data survives workspace restarts)
#   --branch NAME    git branch (default main)
#   --repo-url URL   git remote (default https://github.com/yanboishere/hl-news-bot.git)
#   --plan P         your NewsLiquid plan: free | plus | pro | max (sets transport and polling in config.yaml)
#   --no-ui          no local console (default: console on 127.0.0.1:8765, open it in Memoh's Desktop tab)
#   --no-hooks --no-skill --no-start --no-smoke --no-net    skip a setup step (see `hlbot setup --help`)
#
# The NewsLiquid token is never a command-line argument. Either edit DIR/.env in Memoh's Files tab after the install
# (OPENNEWS_TOKEN=..., picked up automatically within seconds), or `export OPENNEWS_TOKEN=...` in the Terminal tab
# before running this. Trading stays paper unless you later switch it on yourself (README section 16).
set -euo pipefail

DIR=/data/hl-news-bot
DIR_SET=0
BRANCH=main
REPO_URL=https://github.com/yanboishere/hl-news-bot.git
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; DIR_SET=1; shift 2 ;;
    --dir=*) DIR="${1#*=}"; DIR_SET=1; shift ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --branch=*) BRANCH="${1#*=}"; shift ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --repo-url=*) REPO_URL="${1#*=}"; shift ;;
    -h|--help)
      echo "usage: install.sh [--dir DIR] [--branch NAME] [--repo-url URL] [--plan free|plus|pro|max] [--no-ui]"
      echo "                  [--no-hooks] [--no-skill] [--no-start] [--no-smoke] [--no-net]"
      exit 0 ;;
    *) PASS+=("$1"); shift ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf '✘ %s\n' "$*" >&2; exit 1; }

# not on Memoh (no writable /data): install under $HOME instead
if [ "$DIR_SET" = 0 ] && ! { [ -d /data ] && [ -w /data ]; }; then
  DIR="$HOME/hl-news-bot"
fi

pick_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

# 1) code
if [ -d "$DIR/.git" ] && [ -x "$DIR/.venv/bin/python" ] && [ -f "$DIR/ops/memoh/ctl.py" ]; then
  say "== 更新代码：$DIR（$BRANCH）"
  # same path as `hlbot update`: keeps local edits (config.yaml after `hlbot plan`), restarts only while flat
  "$DIR/.venv/bin/python" "$DIR/ops/memoh/ctl.py" update --branch "$BRANCH" || say "! 代码没更新成功，继续用现有版本"
elif [ -d "$DIR/.git" ]; then
  say "== 更新代码：$DIR（$BRANCH）"
  if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
    say "! 本地有改动，这次不拉新代码；装好后可以用 hlbot update"
  elif git -C "$DIR" fetch --quiet origin "$BRANCH"; then
    if [ "$(git -C "$DIR" rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
      git -C "$DIR" checkout --quiet -B "$BRANCH" FETCH_HEAD
    else
      git -C "$DIR" merge --ff-only --quiet FETCH_HEAD || say "! 不能快进，继续用现有代码"
    fi
  else
    say "! git fetch 失败，继续用现有代码"
  fi
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "$DIR 已存在且不是 git 仓库：换个 --dir，或先把它挪走"
elif command -v git >/dev/null 2>&1; then
  say "== 克隆 $REPO_URL（$BRANCH）→ $DIR"
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR"
else
  say "== 没有 git，下载压缩包（之后用重跑本脚本代替 hlbot update）"
  mkdir -p "$DIR"
  curl -fsSL "${REPO_URL%.git}/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$DIR" --strip-components=1
fi
chmod +x "$DIR/ops/memoh/hlbot" "$DIR/ops/memoh/install.sh" 2>/dev/null || true

# 2) python environment (.venv inside the repo, so it lives on /data too)
if [ ! -x "$DIR/.venv/bin/python" ]; then
  PY="$(pick_python || true)"
  if [ -n "$PY" ] && "$PY" -m venv "$DIR/.venv" 2>/dev/null && [ -x "$DIR/.venv/bin/python" ]; then
    say "== 虚拟环境：$("$DIR/.venv/bin/python" --version)"
  elif command -v uv >/dev/null 2>&1; then
    rm -rf "$DIR/.venv"
    say "== 用 uv 建虚拟环境（Python 3.12）"
    uv venv --quiet --seed --python 3.12 "$DIR/.venv" || die "uv 建虚拟环境失败"
  else
    die "需要 Python 3.10 以上（带 venv 模块）或 uv"
  fi
fi

# 3) dependencies
say "== 安装依赖（requirements.txt）"
if ! "$DIR/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  "$DIR/.venv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi
if "$DIR/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  "$DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$DIR/requirements.txt" || die "依赖安装失败"
elif command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$DIR/.venv/bin/python" -r "$DIR/requirements.txt" || die "依赖安装失败"
else
  die "虚拟环境里没有 pip，也没有 uv"
fi

# 4) .env, plan, Memoh hooks + agent skill, self-test, start
exec "$DIR/.venv/bin/python" "$DIR/ops/memoh/ctl.py" setup ${PASS[@]+"${PASS[@]}"}
