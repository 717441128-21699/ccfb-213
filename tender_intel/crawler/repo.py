"""数据仓库：把抓到的站点副本落库，并完成跨站去重与更正/废标挂接。

去重锚点：
- site_notices UNIQUE(site_id, remote_id)：同一站点同一公告永不重复；
- canonical_notices.fingerprint = canonical_key(project_code, notice_type, content_hash)：
  不同站点转载的同一公告落到同一条主记录。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..clock import now_iso
from ..mock.truth import canonical_key_of, content_hash_of


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- 站点 ----------------
    def get_site(self, site_code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sites WHERE code=?", (site_code,)
        ).fetchone()

    def list_sites(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sites"
        if enabled_only:
            sql += " WHERE enabled=1"
        return list(self.conn.execute(sql + " ORDER BY id").fetchall())

    # ---------------- 快照 ----------------
    def add_snapshot(self, site_notice_id: int, kind: str, content: str,
                     media_type: str, fetched_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO snapshots(site_notice_id, kind, content, media_type, fetched_at)"
            " VALUES(?,?,?,?,?)",
            (site_notice_id, kind, content, media_type, fetched_at),
        )
        return cur.lastrowid

    # ---------------- 主记录解析 ----------------
    @staticmethod
    def fingerprint_for(item: dict[str, Any]) -> str:
        chash = content_hash_of(
            item["title"], item.get("purchaser") or "",
            float(item.get("budget_amount") or 0.0),
            item.get("deadline_at") or "",
        )
        return canonical_key_of(item.get("project_code") or "",
                                item["notice_type"], chash)

    def _resolve_parent(self, item: dict[str, Any], site_id: int,
                        origin_remote_id: str | None) -> int | None:
        """更正/废标公告挂到原公告主记录。优先 origin_remote_id，兜底项目编号。"""
        if origin_remote_id:
            row = self.conn.execute(
                "SELECT canonical_id FROM site_notices WHERE site_id=? AND remote_id=?",
                (site_id, origin_remote_id),
            ).fetchone()
            if row and row["canonical_id"]:
                return row["canonical_id"]
        # 兜底：同项目编号下最早的招标公告
        row = self.conn.execute(
            """SELECT cn.id FROM canonical_notices cn
               JOIN site_notices sn ON sn.canonical_id = cn.id
               WHERE sn.site_id=? AND cn.project_code=? AND cn.notice_type='tender'
               ORDER BY cn.first_seen_at ASC LIMIT 1""",
            (site_id, item.get("project_code")),
        ).fetchone()
        return row["id"] if row else None

    def _get_or_create_canonical(self, item: dict[str, Any],
                                 parent_id: int | None, ts: str) -> tuple[int, bool]:
        fingerprint = self.fingerprint_for(item)
        row = self.conn.execute(
            "SELECT * FROM canonical_notices WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row:
            return row["id"], False
        cur = self.conn.execute(
            """INSERT INTO canonical_notices(fingerprint, project_code, title,
                  notice_type, status, parent_id, purchaser, budget_amount,
                  deadline_at, first_seen_at, latest_event_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, item.get("project_code"), item["title"],
             item["notice_type"], item.get("status", "active"), parent_id,
             item.get("purchaser"), item.get("budget_amount"),
             item.get("deadline_at"), ts, ts, ts),
        )
        return cur.lastrowid, True

    # ---------------- 核心 upsert ----------------
    def upsert_notice(self, site: sqlite3.Row, list_item: dict[str, Any],
                      detail: dict[str, Any], parsed: Any,
                      list_json: str, html: str, html_media: str,
                      ts: str) -> dict[str, Any]:
        """落库一条公告，返回变化摘要供引擎统计与写 changes/alerts。"""
        site_id = site["id"]
        remote_id = list_item["remote_id"]
        merged = {
            "title": detail.get("title") or list_item["title"],
            "notice_type": detail.get("notice_type") or list_item.get("notice_type", "tender"),
            "project_code": parsed.project_code or detail.get("project_code"),
            # 结构化字段优先用 API 权威值，解析值兜底，保证与真值同口径
            "purchaser": detail.get("purchaser") or parsed.purchaser,
            "budget_amount": detail.get("budget_amount")
            if detail.get("budget_amount") is not None else parsed.budget_amount,
            "deadline_at": detail.get("deadline_at") or parsed.deadline_at,
        }
        origin_remote_id = list_item.get("origin_remote_id")
        parent_id = None
        if merged["notice_type"] in ("correction", "cancellation"):
            parent_id = self._resolve_parent(merged, site_id, origin_remote_id)

        existing = self.conn.execute(
            "SELECT * FROM site_notices WHERE site_id=? AND remote_id=?",
            (site_id, remote_id),
        ).fetchone()

        result: dict[str, Any] = {
            "is_new": existing is None, "field_changes": [],
            "canonical_new": False, "site_notice_id": None,
            "canonical_id": None,
        }

        canonical_id, canonical_new = self._get_or_create_canonical(merged, parent_id, ts)
        result["canonical_id"] = canonical_id
        result["canonical_new"] = canonical_new

        if existing is None:
            cur = self.conn.execute(
                """INSERT INTO site_notices(site_id, remote_id, canonical_id, title, url,
                      notice_type, origin_remote_id, origin_canonical_id, published_at,
                      status, first_seen_at, last_seen_at, fetched_at, raw_list_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (site_id, remote_id, canonical_id, merged["title"],
                 list_item.get("url"), merged["notice_type"], origin_remote_id,
                 parent_id, list_item.get("published_at"), "active",
                 ts, ts, ts, list_json),
            )
            sn_id = cur.lastrowid
            result["site_notice_id"] = sn_id
            self.add_snapshot(sn_id, "list_json", list_json, "application/json", ts)
            self.add_snapshot(sn_id, "detail_html", html, html_media, ts)
            self._record_change(canonical_id, sn_id,
                                "new" if merged["notice_type"] == "tender"
                                else merged["notice_type"],
                                detail=f"{site['code']}:{remote_id} 首次入库", ts=ts)
        else:
            sn_id = existing["id"]
            result["site_notice_id"] = sn_id
            # 之前被下架、现在又出现：恢复
            if existing["status"] == "delisted":
                self._record_change(canonical_id, sn_id, "relist",
                                    detail=f"{site['code']}:{remote_id} 重新上架", ts=ts)
            self.conn.execute(
                """UPDATE site_notices SET canonical_id=?, title=?, url=?, notice_type=?,
                      origin_remote_id=?, origin_canonical_id=?, published_at=?,
                      status='active', last_seen_at=?, fetched_at=?, raw_list_json=?
                   WHERE id=?""",
                (canonical_id, merged["title"], list_item.get("url"),
                 merged["notice_type"], origin_remote_id, parent_id,
                 list_item.get("published_at"), ts, ts, list_json, sn_id),
            )
            self.add_snapshot(sn_id, "list_json", list_json, "application/json", ts)
            self.add_snapshot(sn_id, "detail_html", html, html_media, ts)
            result["field_changes"] = self._diff_and_update_canonical(
                canonical_id, merged, sn_id, ts)

        # 废标公告联动主记录状态
        if merged["notice_type"] == "cancellation":
            self._apply_cancellation(canonical_id, parent_id, sn_id, ts)
        return result

    def _diff_and_update_canonical(self, canonical_id: int, item: dict[str, Any],
                                   sn_id: int, ts: str) -> list[dict[str, str]]:
        row = self.conn.execute(
            "SELECT * FROM canonical_notices WHERE id=?", (canonical_id,)
        ).fetchone()
        changes: list[dict[str, str]] = []
        updates: dict[str, Any] = {}
        for field in ("purchaser", "budget_amount", "deadline_at", "project_code", "title"):
            new_val = item.get(field)
            old_val = row[field]
            if new_val is None:
                continue
            if old_val is None or str(old_val) != str(new_val):
                changes.append({"field": field,
                                "old": "" if old_val is None else str(old_val),
                                "new": str(new_val)})
                updates[field] = new_val
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            self.conn.execute(
                f"UPDATE canonical_notices SET {sets}, updated_at=?, latest_event_at=? WHERE id=?",
                (*updates.values(), ts, ts, canonical_id),
            )
            for ch in changes:
                self._record_change(canonical_id, sn_id, "field_update",
                                    field=ch["field"], old_value=ch["old"],
                                    new_value=ch["new"], ts=ts)
        else:
            self.conn.execute(
                "UPDATE canonical_notices SET updated_at=? WHERE id=?", (ts, canonical_id))
        return changes

    def _apply_cancellation(self, canonical_id: int, parent_id: int | None,
                            sn_id: int, ts: str) -> None:
        self.conn.execute(
            "UPDATE canonical_notices SET status='cancelled', updated_at=?, latest_event_at=?"
            " WHERE id=?", (ts, ts, canonical_id))
        target = parent_id
        if target:
            self.conn.execute(
                "UPDATE canonical_notices SET status='cancelled', updated_at=?, latest_event_at=?"
                " WHERE id=?", (ts, ts, target))
            self._record_change(target, sn_id, "cancellation",
                                detail=f"废标公告 {canonical_id} 已挂接，原公告置为废标", ts=ts)

    def link_pending_children(self, ts: str) -> int:
        """补偿挂接：父公告晚于更正/废标入库时，parent_id 暂时为空，这里重试。

        优先用各站副本的 origin_remote_id 反查父副本的 canonical_id，
        再退回同项目编号的招标公告。
        """
        rows = self.conn.execute(
            """SELECT cn.id AS canonical_id, cn.project_code, sn.site_id,
                      sn.origin_remote_id, sn.id AS site_notice_id
               FROM canonical_notices cn
               JOIN site_notices sn ON sn.canonical_id = cn.id
               WHERE cn.notice_type IN ('correction','cancellation')
                 AND cn.parent_id IS NULL"""
        ).fetchall()
        linked = set()
        for row in rows:
            parent_id = None
            if row["origin_remote_id"]:
                parent = self.conn.execute(
                    "SELECT canonical_id FROM site_notices WHERE site_id=? AND remote_id=?",
                    (row["site_id"], row["origin_remote_id"])).fetchone()
                if parent and parent["canonical_id"]:
                    parent_id = parent["canonical_id"]
            if not parent_id and row["project_code"]:
                parent = self.conn.execute(
                    """SELECT cn.id FROM canonical_notices cn
                       JOIN site_notices sn ON sn.canonical_id = cn.id
                       WHERE sn.site_id=? AND cn.project_code=?
                         AND cn.notice_type='tender'
                       ORDER BY cn.first_seen_at ASC LIMIT 1""",
                    (row["site_id"], row["project_code"])).fetchone()
                if parent:
                    parent_id = parent["id"]
            if parent_id and row["canonical_id"] not in linked:
                self.conn.execute(
                    "UPDATE canonical_notices SET parent_id=?, updated_at=? WHERE id=?",
                    (parent_id, ts, row["canonical_id"]))
                self.conn.execute(
                    "UPDATE site_notices SET origin_canonical_id=? WHERE canonical_id=?",
                    (parent_id, row["canonical_id"]))
                self._record_change(
                    row["canonical_id"], row["site_notice_id"], "link_parent",
                    detail=f"补偿挂接到父公告 canonical#{parent_id}", ts=ts)
                linked.add(row["canonical_id"])
        return len(linked)

    def mark_delisted(self, site_id: int, remote_ids: set[str], ts: str) -> int:
        """全量模式下，本次没再出现的在线副本标记为 delisted。"""
        if not remote_ids:
            return 0
        count = 0
        rows = self.conn.execute(
            "SELECT id, remote_id, canonical_id FROM site_notices"
            " WHERE site_id=? AND status='active'", (site_id,)
        ).fetchall()
        for row in rows:
            if row["remote_id"] not in remote_ids:
                self.conn.execute(
                    "UPDATE site_notices SET status='delisted', last_seen_at=? WHERE id=?",
                    (ts, row["id"]))
                self._record_change(row["canonical_id"], row["id"], "delist",
                                    detail=f"站点侧下架 {row['remote_id']}", ts=ts)
                count += 1
        return count

    # ---------------- 解析失败 / 变更 / 告警 ----------------
    def add_parse_failures(self, site_id: int, remote_id: str,
                           failures: list[dict[str, Any]], run_id: int,
                           ts: str) -> None:
        for fail in failures:
            self.conn.execute(
                """INSERT INTO parse_failures(site_id, remote_id, field, raw_value,
                      reason, run_id, created_at) VALUES(?,?,?,?,?,?,?)""",
                (site_id, remote_id, fail["field"], fail.get("raw_value"),
                 fail.get("reason"), run_id, ts),
            )

    def _record_change(self, canonical_id: int, site_notice_id: int | None,
                       change_type: str, field: str | None = None,
                       old_value: str | None = None, new_value: str | None = None,
                       detail: str | None = None, ts: str = now_iso()) -> None:
        self.conn.execute(
            """INSERT INTO changes(canonical_id, site_notice_id, change_type, field,
                  old_value, new_value, detail, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (canonical_id, site_notice_id, change_type, field,
             old_value, new_value, detail, ts),
        )

    def add_alert(self, site_id: int | None, run_id: int | None, alert_type: str,
                  severity: str, message: str, payload: dict[str, Any] | None = None,
                  ts: str = now_iso()) -> int:
        cur = self.conn.execute(
            """INSERT INTO alerts(site_id, run_id, alert_type, severity, message,
                  payload_json, resolved, created_at) VALUES(?,?,?,?,?,?,0,?)""",
            (site_id, run_id, alert_type, severity, message,
             json.dumps(payload, ensure_ascii=False) if payload else None, ts),
        )
        return cur.lastrowid

    # ---------------- 水位 / 断点 ----------------
    def get_watermark(self, site_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT v FROM kv WHERE k=?", (f"watermark:{site_id}",)
        ).fetchone()
        return row["v"] if row else None

    def set_watermark(self, site_id: int, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (f"watermark:{site_id}", value),
        )

    def save_checkpoint(self, run_id: int, site_id: int, stage: str, page: int,
                        last_remote_id: str | None, seen_ids: set[str], ts: str) -> None:
        self.conn.execute(
            """INSERT INTO crawl_checkpoints(run_id, site_id, stage, page, last_remote_id,
                  seen_remote_ids_json, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(run_id, site_id) DO UPDATE SET
                 stage=excluded.stage, page=excluded.page,
                 last_remote_id=excluded.last_remote_id,
                 seen_remote_ids_json=excluded.seen_remote_ids_json,
                 updated_at=excluded.updated_at""",
            (run_id, site_id, stage, page, last_remote_id,
             json.dumps(sorted(seen_ids)), ts),
        )

    def get_checkpoint(self, run_id: int, site_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM crawl_checkpoints WHERE run_id=? AND site_id=?",
            (run_id, site_id),
        ).fetchone()
        if not row:
            return None
        return {
            "stage": row["stage"], "page": row["page"],
            "last_remote_id": row["last_remote_id"],
            "seen_remote_ids": set(json.loads(row["seen_remote_ids_json"])),
        }

    def clear_checkpoint(self, run_id: int, site_id: int) -> None:
        self.conn.execute(
            "DELETE FROM crawl_checkpoints WHERE run_id=? AND site_id=?",
            (run_id, site_id),
        )
