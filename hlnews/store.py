"""SQLite persistence: every news item, classification, signal, decision and position."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from .models import Classification, NewsItem, Position, Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
  id TEXT PRIMARY KEY, source TEXT, channel TEXT, account TEXT, title TEXT, url TEXT,
  ts_ms INTEGER, recv_ms INTEGER, age_s REAL, suggestions TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
  news_id TEXT, lane TEXT, event_class TEXT, direction TEXT, confidence REAL, tickers TEXT,
  reason TEXT, is_rumor INTEGER, latency_ms INTEGER, created_ms INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, news_id TEXT, coin TEXT, direction TEXT, event_class TEXT, confidence REAL,
  created_ms INTEGER, template TEXT, reason TEXT, news_age_s REAL
);
CREATE TABLE IF NOT EXISTS decisions (
  signal_id TEXT, accepted INTEGER, reason TEXT, created_ms INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
  id TEXT PRIMARY KEY, signal_id TEXT, coin TEXT, direction TEXT, event_class TEXT, size REAL,
  entry_px REAL, entry_ms INTEGER, stop_px REAL, tp_px REAL, time_exit_ms INTEGER, notional_usd REAL,
  leverage REAL, mode TEXT, fee_usd REAL, slippage_bps REAL, exit_px REAL, exit_ms INTEGER,
  exit_reason TEXT, pnl_usd REAL
);
CREATE TABLE IF NOT EXISTS equity (
  ts_ms INTEGER, equity_usd REAL, open_positions INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (
  ts_ms INTEGER, kind TEXT, title TEXT, body TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts_ms);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts_ms);
CREATE INDEX IF NOT EXISTS idx_pos_entry ON positions(entry_ms);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def news(self, n: NewsItem) -> None:
        self._exec(
            "INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (n.id, n.source, n.channel, n.account, n.title, n.url, n.ts_ms, n.recv_ms, n.age_s,
             json.dumps(n.suggestions), json.dumps(n.raw, ensure_ascii=False)[:20000]),
        )

    def classification(self, news_id: str, c: Classification, created_ms: int) -> None:
        self._exec(
            "INSERT INTO classifications VALUES (?,?,?,?,?,?,?,?,?,?)",
            (news_id, c.lane, c.event_class.value, c.direction.value, c.confidence, json.dumps(c.tickers),
             c.reason, int(c.is_rumor), c.latency_ms, created_ms),
        )

    def signal(self, s: Signal) -> None:
        self._exec(
            "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s.id, s.news_id, s.coin, s.direction.value, s.event_class.value, s.confidence, s.created_ms,
             json.dumps(s.template), s.reason, s.news_age_s),
        )

    def decision(self, signal_id: str, accepted: bool, reason: str, created_ms: int) -> None:
        self._exec("INSERT INTO decisions VALUES (?,?,?,?)", (signal_id, int(accepted), reason, created_ms))

    def position(self, p: Position) -> None:
        r = p.to_row()
        cols = ",".join(r.keys())
        q = ",".join("?" for _ in r)
        self._exec(f"INSERT OR REPLACE INTO positions ({cols}) VALUES ({q})", tuple(r.values()))

    def equity(self, ts_ms: int, equity_usd: float, open_positions: int) -> None:
        self._exec("INSERT INTO equity VALUES (?,?,?)", (ts_ms, equity_usd, open_positions))

    def alert(self, ts_ms: int, kind: str, title: str, body: str, meta: dict) -> None:
        self._exec("INSERT INTO alerts VALUES (?,?,?,?,?)", (ts_ms, kind, title, body, json.dumps(meta, ensure_ascii=False, default=str)[:20000]))

    def alerts(self, limit: int = 50, since_ms: int | None = None) -> list[dict[str, Any]]:
        if since_ms:
            cur = self.conn.execute("SELECT * FROM alerts WHERE ts_ms>=? ORDER BY ts_ms DESC LIMIT ?", (since_ms, limit))
        else:
            cur = self.conn.execute("SELECT * FROM alerts ORDER BY ts_ms DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            try:
                d["meta"] = json.loads(d.get("meta") or "{}")
            except Exception:  # noqa: BLE001
                pass
            out.append(d)
        return out

    # ------------------------------------------------------------------ reads
    def closed_positions(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM positions WHERE exit_ms IS NOT NULL ORDER BY exit_ms")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def summary(self) -> dict[str, Any]:
        rows = self.closed_positions()
        n = len(rows)
        pnl = sum(r["pnl_usd"] or 0 for r in rows)
        wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
        by_class: dict[str, dict[str, float]] = {}
        for r in rows:
            b = by_class.setdefault(r["event_class"], {"n": 0, "pnl": 0.0, "wins": 0})
            b["n"] += 1
            b["pnl"] += r["pnl_usd"] or 0
            b["wins"] += 1 if (r["pnl_usd"] or 0) > 0 else 0
        news_n = self.conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        sig_n = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        return {"news": news_n, "signals": sig_n, "closed": n, "wins": wins, "pnl_usd": round(pnl, 2), "by_class": by_class}
