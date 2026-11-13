"""公告库：跨站去重后的主记录 + 各站副本 + 原始快照 + 更正/废标挂树。"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from ..db import connect

bp = Blueprint("notices", __name__, url_prefix="/notices")
PAGE_SIZE = 15


@bp.get("/")
def index():
    q = request.args.get("q", "").strip()
    site_code = request.args.get("site", "").strip()
    notice_type = request.args.get("type", "").strip()
    status = request.args.get("status", "").strip()
    page = max(int(request.args.get("page", 1)), 1)

    where, params = [], []
    if q:
        where.append("(cn.title LIKE ? OR cn.project_code LIKE ? OR cn.purchaser LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if notice_type:
        where.append("cn.notice_type=?")
        params.append(notice_type)
    if status:
        where.append("cn.status=?")
        params.append(status)
    if site_code:
        where.append("EXISTS(SELECT 1 FROM site_notices sn0 "
                     "WHERE sn0.canonical_id=cn.id AND sn0.site_id="
                     "(SELECT id FROM sites WHERE code=?))")
        params.append(site_code)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM canonical_notices cn {where_sql}", params).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT cn.*,
                  (SELECT COUNT(*) FROM site_notices sn WHERE sn.canonical_id=cn.id
                     AND sn.status='active') AS replica_count,
                  (SELECT GROUP_CONCAT(s.code) FROM site_notices sn
                     JOIN sites s ON s.id=sn.site_id
                     WHERE sn.canonical_id=cn.id AND sn.status='active') AS site_codes
                FROM canonical_notices cn {where_sql}
                ORDER BY cn.latest_event_at DESC, cn.id DESC
                LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        sites = conn.execute("SELECT code, name FROM sites ORDER BY id").fetchall()
    finally:
        conn.close()

    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)

    def page_link(target: int) -> str:
        args = request.args.to_dict()
        args["page"] = str(target)
        from urllib.parse import urlencode
        return "?" + urlencode({k: v for k, v in args.items() if v != ""})

    return render_template("notices/index.html", notices=rows, sites=sites,
                           filters={"q": q, "site": site_code,
                                    "type": notice_type, "status": status},
                           page=page, total_pages=total_pages, total=total,
                           page_link=page_link)


@bp.get("/<int:canonical_id>")
def detail(canonical_id: int):
    conn = connect()
    try:
        notice = conn.execute(
            "SELECT * FROM canonical_notices WHERE id=?",
            (canonical_id,)).fetchone()
        if not notice:
            return "公告不存在", 404
        replicas = conn.execute(
            """SELECT sn.*, s.code AS site_code, s.name AS site_name
               FROM site_notices sn JOIN sites s ON s.id=sn.site_id
               WHERE sn.canonical_id=? ORDER BY sn.published_at, sn.id""",
            (canonical_id,)).fetchall()
        children = conn.execute(
            "SELECT * FROM canonical_notices WHERE parent_id=? ORDER BY first_seen_at",
            (canonical_id,)).fetchall()
        parent = None
        if notice["parent_id"]:
            parent = conn.execute(
                "SELECT * FROM canonical_notices WHERE id=?",
                (notice["parent_id"],)).fetchone()
        change_rows = conn.execute(
            """SELECT ch.*, s.code AS site_code FROM changes ch
               LEFT JOIN site_notices sn ON sn.id=ch.site_notice_id
               LEFT JOIN sites s ON s.id=sn.site_id
               WHERE ch.canonical_id=? ORDER BY ch.id DESC LIMIT 100""",
            (canonical_id,)).fetchall()
        # 每个副本最新的两份快照
        snapshot_map: dict[int, list] = {}
        for rep in replicas:
            snapshot_map[rep["id"]] = conn.execute(
                "SELECT * FROM snapshots WHERE site_notice_id=?"
                " ORDER BY id DESC LIMIT 2", (rep["id"],)).fetchall()
        failures = conn.execute(
            """SELECT pf.*, s.code FROM parse_failures pf
               JOIN sites s ON s.id=pf.site_id
               WHERE pf.remote_id IN
                 (SELECT remote_id FROM site_notices WHERE canonical_id=?)
               ORDER BY pf.id DESC LIMIT 30""",
            (canonical_id,)).fetchall()
    finally:
        conn.close()
    return render_template("notices/detail.html", notice=notice, replicas=replicas,
                           children=children, parent=parent, changes=change_rows,
                           snapshot_map=snapshot_map, failures=failures)


@bp.get("/replicas/<int:site_notice_id>/snapshots")
def snapshots(site_notice_id: int):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE site_notice_id=? ORDER BY id DESC",
            (site_notice_id,)).fetchall()
        rep = conn.execute(
            """SELECT sn.*, s.code FROM site_notices sn
               JOIN sites s ON s.id=sn.site_id WHERE sn.id=?""",
            (site_notice_id,)).fetchone()
    finally:
        conn.close()
    return render_template("notices/snapshots.html", snapshots=rows, rep=rep)
