"""SQLite schema 与连接管理。

设计要点（对应需求）：
- site_notices 以 (site_id, remote_id) 唯一，是增量去重的锚点；
- canonical_notices 是跨站去重后的主记录，site_notices.canonical_id 挂接；
- 更正/废标公告以 parent_id 挂到原公告；
- snapshots 存每条数据的原始快照（列表 JSON + 详情 HTML）；
- changes 记录结构化字段变更与事件；alerts 记录告警。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import db_path, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,          -- js / zj / gd / sc
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    secret TEXT NOT NULL,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    rate_limit_per_min INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    selectors_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_notices (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,   -- 跨站去重指纹
    project_code TEXT,
    title TEXT NOT NULL,
    notice_type TEXT NOT NULL DEFAULT 'tender',  -- tender/correction/cancellation
    status TEXT NOT NULL DEFAULT 'active',       -- active/cancelled
    parent_id INTEGER REFERENCES canonical_notices(id),
    purchaser TEXT,
    budget_amount REAL,
    deadline_at TEXT,
    first_seen_at TEXT NOT NULL,
    latest_event_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_notices (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    remote_id TEXT NOT NULL,
    canonical_id INTEGER REFERENCES canonical_notices(id),
    title TEXT NOT NULL,
    url TEXT,
    notice_type TEXT NOT NULL DEFAULT 'tender',
    origin_remote_id TEXT,              -- 更正/废标指向的原公告 remote_id
    origin_canonical_id INTEGER,
    published_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active / delisted
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    raw_list_json TEXT,
    UNIQUE(site_id, remote_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    site_notice_id INTEGER NOT NULL REFERENCES site_notices(id),
    kind TEXT NOT NULL,                 -- list_json / detail_html
    content TEXT NOT NULL,
    media_type TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parse_failures (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    remote_id TEXT NOT NULL,
    field TEXT NOT NULL,               -- amount / deadline / purchaser
    raw_value TEXT,
    reason TEXT,
    run_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES canonical_notices(id),
    site_notice_id INTEGER REFERENCES site_notices(id),
    change_type TEXT NOT NULL,          -- new/correction/cancellation/field_update/delist
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    site_id INTEGER REFERENCES sites(id),
    run_id INTEGER,
    alert_type TEXT NOT NULL,           -- parse_breakdown / rate_limited / auth_failed / crawl_error
    severity TEXT NOT NULL DEFAULT 'warning',  -- info/warning/critical
    message TEXT NOT NULL,
    payload_json TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,                 -- full / incremental
    status TEXT NOT NULL,               -- running/succeeded/failed/cancelled
    site_codes TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS crawl_checkpoints (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    site_id INTEGER NOT NULL,
    stage TEXT NOT NULL,                -- list / detail
    page INTEGER NOT NULL DEFAULT 1,
    last_remote_id TEXT,
    seen_remote_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, site_id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    site_code TEXT,                     -- NULL 表示全部站点
    mode TEXT NOT NULL DEFAULT 'incremental',
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sn_canonical ON site_notices(canonical_id);
CREATE INDEX IF NOT EXISTS idx_sn_parent ON canonical_notices(parent_id);
CREATE INDEX IF NOT EXISTS idx_changes_canonical ON changes(canonical_id);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_sn_published ON site_notices(published_at);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    p = Path(path) if path else db_path()
    conn = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str | Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["v"])


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )
