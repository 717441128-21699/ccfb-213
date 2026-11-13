"""对账：把爬虫 SQLite 库与 mock 真值库逐副本、逐字段比对。

通过条件（需求：增量跑完公告库要和 mock 站真值完全对上）：
1. 在线副本集合一致：真值 active 副本 (site, remote_id) 与库中 status=active 完全相等；
2. 每条副本的结构化字段（金额/截止时间/采购单位/项目编号/标题）一致；
3. 跨站去重正确：同一 canonical_key 的副本在库里指向同一条 canonical_notices；
4. 更正/废标挂接正确：parent 指向原招标公告主记录。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .db import connect
from .mock.truth import TruthStore


@dataclass
class ReconcileReport:
    ok: bool = True
    truth_replicas: int = 0
    db_replicas: int = 0
    missing: list[str] = field(default_factory=list)      # 真值有、库里没有
    extra: list[str] = field(default_factory=list)        # 库里有、真值没有
    field_mismatches: list[dict] = field(default_factory=list)
    dedup_errors: list[str] = field(default_factory=list)
    parent_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "truth_replicas": self.truth_replicas,
            "db_replicas": self.db_replicas,
            "missing": self.missing,
            "extra": self.extra,
            "field_mismatches": self.field_mismatches,
            "dedup_errors": self.dedup_errors,
            "parent_errors": self.parent_errors,
        }


def _close(a, b, tol: float = 0.011) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a) == str(b)


def reconcile(db_file: str | None = None,
              truth_file: str | None = None) -> ReconcileReport:
    store = TruthStore.load(truth_file)
    truth = store.truth_view()
    report = ReconcileReport()
    conn: sqlite3.Connection = connect(db_file)
    try:
        site_rows = {r["code"]: r for r in conn.execute("SELECT * FROM sites")}
        db_rows = conn.execute(
            """SELECT sn.*, s.code AS site_code, cn.fingerprint, cn.parent_id,
                      cn.notice_type AS cn_type, cn.project_code AS cn_project_code
               FROM site_notices sn
               JOIN sites s ON s.id = sn.site_id
               LEFT JOIN canonical_notices cn ON cn.id = sn.canonical_id
               WHERE sn.status='active'"""
        ).fetchall()
        db_index = {f"{r['site_code']}:{r['remote_id']}": r for r in db_rows}
        truth_index = truth["replicas"]

        report.truth_replicas = len(truth_index)
        report.db_replicas = len(db_index)
        report.missing = sorted(set(truth_index) - set(db_index))
        report.extra = sorted(set(db_index) - set(truth_index))

        # 字段一致性
        for key, t in sorted(truth_index.items()):
            row = db_index.get(key)
            if row is None:
                continue
            mism = {}
            if row["fingerprint"] != t["canonical_key"]:
                mism["canonical_key"] = {"db": row["fingerprint"],
                                         "truth": t["canonical_key"]}
            cn = conn.execute(
                "SELECT * FROM canonical_notices WHERE id=?",
                (row["canonical_id"],)).fetchone()
            if cn:
                if not _close(cn["budget_amount"], t["budget_amount"]):
                    mism["budget_amount"] = {"db": cn["budget_amount"],
                                             "truth": t["budget_amount"]}
                if (cn["deadline_at"] or "") != (t["deadline_at"] or ""):
                    mism["deadline_at"] = {"db": cn["deadline_at"],
                                           "truth": t["deadline_at"]}
                if (cn["purchaser"] or "") != (t["purchaser"] or ""):
                    mism["purchaser"] = {"db": cn["purchaser"],
                                         "truth": t["purchaser"]}
                if (cn["project_code"] or "") != (t["project_code"] or ""):
                    mism["project_code"] = {"db": cn["project_code"],
                                            "truth": t["project_code"]}
                if (cn["title"] or "") != (t["title"] or ""):
                    mism["title"] = {"db": cn["title"], "truth": t["title"]}
                if cn["notice_type"] != t["notice_type"]:
                    mism["notice_type"] = {"db": cn["notice_type"],
                                           "truth": t["notice_type"]}
            if mism:
                report.field_mismatches.append({"key": key, "fields": mism})

        # 跨站去重：同 canonical_key 的所有副本必须指向同一 canonical_id
        groups: dict[str, set] = {}
        for key, t in truth_index.items():
            row = db_index.get(key)
            if row:
                groups.setdefault(t["canonical_key"], set()).add(row["canonical_id"])
        for ckey, ids in groups.items():
            if len(ids) > 1:
                report.dedup_errors.append(
                    f"{ckey} 被映射到多个 canonical_notices: {sorted(ids)}")

        # 更正/废标挂接
        for ckey, tc in truth["canonical"].items():
            if tc["notice_type"] not in ("correction", "cancellation"):
                continue
            row = conn.execute(
                "SELECT id, parent_id FROM canonical_notices WHERE fingerprint=?",
                (ckey,)).fetchone()
            if row is None:
                continue
            expected_parent = conn.execute(
                "SELECT id FROM canonical_notices WHERE fingerprint=?",
                (tc["parent_key"],)).fetchone()
            if expected_parent is None:
                report.parent_errors.append(f"{ckey}: 真值父公告 {tc['parent_key']} 不在库中")
            elif row["parent_id"] != expected_parent["id"]:
                report.parent_errors.append(
                    f"{ckey}: parent_id={row['parent_id']}，应为 {expected_parent['id']}")

        # 站点维度汇总（方便看哪个省出问题）
        per_site: dict[str, dict] = {}
        for key in truth_index:
            code = key.split(":", 1)[0]
            per_site.setdefault(code, {"truth": 0, "db": 0})["truth"] += 1
        for key in db_index:
            code = key.split(":", 1)[0]
            per_site.setdefault(code, {"truth": 0, "db": 0})["db"] += 1

        report.ok = not (report.missing or report.extra or report.field_mismatches
                         or report.dedup_errors or report.parent_errors)
        report.per_site = per_site  # type: ignore[attr-defined]
        return report
    finally:
        conn.close()
