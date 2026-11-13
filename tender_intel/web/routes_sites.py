"""站点配置页：连接信息、限流、启用开关、CSS 选择器（v1/v2 模板适配）。"""
from __future__ import annotations

import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..clock import now_iso
from ..db import connect
from ..mock.sites import SELECTORS_V1, SELECTORS_V2

bp = Blueprint("sites", __name__, url_prefix="/sites")


@bp.get("/")
def index():
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT s.*,
                 (SELECT COUNT(*) FROM site_notices sn
                   WHERE sn.site_id=s.id AND sn.status='active') AS replica_count
               FROM sites s ORDER BY s.id""").fetchall()
        sites = []
        for r in rows:
            d = dict(r)
            d["selector_version"] = 2 if "bid-card" in (r["selectors_json"] or "") else 1
            sites.append(d)
    finally:
        conn.close()
    return render_template("sites/index.html", sites=sites)


@bp.route("/<int:site_id>/edit", methods=["GET", "POST"])
def edit(site_id: int):
    conn = connect()
    try:
        site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        if not site:
            flash("站点不存在", "err")
            return redirect(url_for("sites.index"))
        if request.method == "POST":
            selectors_json = request.form.get("selectors_json", "{}")
            try:
                json.loads(selectors_json)
            except ValueError:
                flash("选择器 JSON 格式不合法", "err")
                return render_template("sites/edit.html", site=site)
            conn.execute(
                """UPDATE sites SET name=?, base_url=?, secret=?, username=?,
                      password=?, rate_limit_per_min=?, enabled=?, selectors_json=?,
                      updated_at=? WHERE id=?""",
                (request.form["name"], request.form["base_url"],
                 request.form["secret"], request.form["username"],
                 request.form["password"], int(request.form["rate_limit_per_min"]),
                 1 if request.form.get("enabled") == "on" else 0,
                 selectors_json, now_iso(), site_id))
            conn.commit()
            flash("站点配置已保存", "ok")
            return redirect(url_for("sites.index"))
    finally:
        conn.close()
    return render_template("sites/edit.html", site=site)


@bp.post("/<int:site_id>/selectors/v2")
def upgrade_selectors(site_id: int):
    """站点改版后，把选择器整体切换到 v2（与 mock 模板 v2 class 对齐）。"""
    conn = connect()
    try:
        conn.execute("UPDATE sites SET selectors_json=?, updated_at=? WHERE id=?",
                     (json.dumps(SELECTORS_V2, ensure_ascii=False), now_iso(), site_id))
        conn.commit()
    finally:
        conn.close()
    flash("选择器已升级为 v2，下次抓取即可恢复解析", "ok")
    return redirect(url_for("sites.index"))


@bp.post("/<int:site_id>/selectors/v1")
def downgrade_selectors(site_id: int):
    conn = connect()
    try:
        conn.execute("UPDATE sites SET selectors_json=?, updated_at=? WHERE id=?",
                     (json.dumps(SELECTORS_V1, ensure_ascii=False), now_iso(), site_id))
        conn.commit()
    finally:
        conn.close()
    flash("选择器已回退为 v1", "ok")
    return redirect(url_for("sites.index"))


@bp.post("/<int:site_id>/test")
def test_connection(site_id: int):
    """发一次登录 + 列表请求，验证签名/账号/连通性。"""
    from ..crawler.client import CrawlError, SignedClient
    conn = connect()
    try:
        site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    finally:
        conn.close()
    try:
        client = SignedClient(site["base_url"], site["secret"],
                              site["username"], site["password"])
        client.login()
        data = client.list_notices(1, 5)
        client.close()
        flash(f"连通正常：首页返回 {data.get('total', 0)} 条公告", "ok")
    except CrawlError as exc:
        flash(f"连通失败（{exc.type}）：{exc}", "err")
    except Exception as exc:  # 网络层等
        flash(f"连通失败：{exc}", "err")
    return redirect(url_for("sites.index"))
