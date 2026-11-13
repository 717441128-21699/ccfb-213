"""站点初始化：把 mock 四站配置写入 sites 表（已存在则更新连接信息）。"""
from __future__ import annotations

import json

from .clock import now_iso
from .config import DEMO_PASSWORD
from .db import connect, init_db
from .mock.sites import MOCK_SITES, SELECTORS_V1


def bootstrap_sites(db_file: str | None = None,
                    base_urls: dict[str, str] | None = None) -> None:
    init_db(db_file)
    base_urls = base_urls or {}
    conn = connect(db_file)
    ts = now_iso()
    try:
        for site in MOCK_SITES:
            base_url = base_urls.get(site.code) or f"http://127.0.0.1:{site.port}"
            existing = conn.execute(
                "SELECT id FROM sites WHERE code=?", (site.code,)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE sites SET name=?, base_url=?, secret=?, username=?,
                          password=?, rate_limit_per_min=?, updated_at=? WHERE code=?""",
                    (site.name, base_url, site.secret, "demo", DEMO_PASSWORD,
                     site.rate_limit_per_min, ts, site.code))
            else:
                conn.execute(
                    """INSERT INTO sites(code, name, base_url, secret, username, password,
                          rate_limit_per_min, enabled, selectors_json, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
                    (site.code, site.name, base_url, site.secret, "demo",
                     DEMO_PASSWORD, site.rate_limit_per_min,
                     json.dumps(SELECTORS_V1, ensure_ascii=False), ts, ts))
        conn.commit()
    finally:
        conn.close()
