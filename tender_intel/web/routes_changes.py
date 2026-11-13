"""变更记录页：新增、更正、废标、字段变更、下架的全局流水。"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from ..db import connect

bp = Blueprint("changes", __name__, url_prefix="/changes")
PAGE_SIZE = 25


@bp.get("/")
def index():
    change_type = request.args.get("type", "").strip()
    page = max(int(request.args.get("page", 1)), 1)
    where, params = [], []
    if change_type:
        where.append("ch.change_type=?")
        params.append(change_type)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM changes ch {where_sql}", params).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT ch.*, cn.title AS canonical_title, s.code AS site_code,
                       sn.remote_id AS remote_id
                FROM changes ch
                JOIN canonical_notices cn ON cn.id=ch.canonical_id
                LEFT JOIN site_notices sn ON sn.id=ch.site_notice_id
                LEFT JOIN sites s ON s.id=sn.site_id
                {where_sql}
                ORDER BY ch.id DESC LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    finally:
        conn.close()
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    return render_template("changes/index.html", changes=rows,
                           selected_type=change_type, page=page,
                           total_pages=total_pages, total=total)
