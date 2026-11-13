"""告警中心：改版、限流、登录失败、抓取错误。"""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..db import connect

bp = Blueprint("alerts", __name__, url_prefix="/alerts")
PAGE_SIZE = 25


@bp.get("/")
def index():
    only_open = request.args.get("open") == "1"
    page = max(int(request.args.get("page", 1)), 1)
    where = "WHERE a.resolved=0" if only_open else ""
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM alerts a {where}").fetchone()["c"]
        rows = conn.execute(
            f"""SELECT a.*, s.code AS site_code FROM alerts a
                LEFT JOIN sites s ON s.id=a.site_id {where}
                ORDER BY a.id DESC LIMIT ? OFFSET ?""",
            (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        open_count = conn.execute(
            "SELECT COUNT(*) c FROM alerts WHERE resolved=0").fetchone()["c"]
    finally:
        conn.close()
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    return render_template("alerts/index.html", alerts=rows, only_open=only_open,
                           page=page, total_pages=total_pages, total=total,
                           open_count=open_count)


@bp.post("/<int:alert_id>/resolve")
def resolve(alert_id: int):
    conn = connect()
    try:
        conn.execute("UPDATE alerts SET resolved=1 WHERE id=?", (alert_id,))
        conn.commit()
    finally:
        conn.close()
    flash(f"告警 #{alert_id} 已标记处理", "ok")
    return redirect(url_for("alerts.index", open=request.args.get("open", "")))


@bp.post("/resolve-all")
def resolve_all():
    conn = connect()
    try:
        conn.execute("UPDATE alerts SET resolved=1 WHERE resolved=0")
        conn.commit()
    finally:
        conn.close()
    flash("全部未处理告警已标记处理", "ok")
    return redirect(url_for("alerts.index"))
