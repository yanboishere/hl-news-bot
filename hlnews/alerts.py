"""Alerts: key news, opens, closes, halts. Always to SQLite + log; optionally Telegram Bot API and a macOS notification.

Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment (create a bot with @BotFather, send it one
message, read the chat id from https://api.telegram.org/bot<TOKEN>/getUpdates). Sending needs no user session.
macOS: `osascript -e 'display notification ...'` when sys.platform == "darwin".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request

from .models import now_ms
from .store import Store

log = logging.getLogger("alerts")


class Alerter:
    def __init__(self, cfg: dict, store: Store):
        a = cfg.get("alerts") or {}
        self.on = set(a.get("on") or ["key_news", "open", "close", "halt", "feed"])
        self.telegram = bool(a.get("telegram", True))
        self.macos = bool(a.get("macos_notification", True)) and sys.platform == "darwin"
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.store = store
        self.mode = cfg.get("mode", "paper")
        if self.telegram and not (self.token and self.chat_id):
            log.info("telegram alerts off: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
            self.telegram = False

    def enabled(self, kind: str) -> bool:
        return kind in self.on

    async def send(self, kind: str, title: str, body: str, meta: dict | None = None) -> None:
        """kind: key_news | open | close | halt | feed. Never raises."""
        if not self.enabled(kind):
            return
        ts = now_ms()
        try:
            self.store.alert(ts, kind, title, body, meta or {})
        except Exception as e:  # noqa: BLE001
            log.warning("alert store failed: %s", e)
        log.info("ALERT [%s] %s | %s", kind, title, body.replace("\n", " ")[:300])
        loop = asyncio.get_running_loop()
        if self.telegram:
            await loop.run_in_executor(None, self._telegram, f"[{self.mode}] {title}\n{body}")
        if self.macos:
            await loop.run_in_executor(None, self._osascript, title, body)

    def _telegram(self, text: str) -> None:
        try:
            data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text[:3900], "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram send failed: %s", e)

    @staticmethod
    def _osascript(title: str, body: str) -> None:
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')[:200]
        try:
            subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "hl-news-bot" subtitle "{esc(title)}"'],
                           timeout=5, check=False, capture_output=True)
        except Exception as e:  # noqa: BLE001
            log.debug("osascript failed: %s", e)
