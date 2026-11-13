"""总览页。"""
from __future__ import annotations

import json

from flask import Blueprint, render_template

from ..db import connect

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    conn = connect()
    try:
        counts = {
            "sites": conn.execute(
                "SELECT COUNT(*) c FROM sites WHERE enabled=1").fetchone()["c"],
            "canonical": conn.execute(
                "SELECT COUNT(*) c FROM canonical_notices").fetchone()["c"],
            "replicas": conn.execute(
                "SELECT COUNT(*) c FROM site_notices WHERE status='active'").fetchone()["c"],
            "children": conn.execute(
                "SELECT COUNT(*) c FROM canonical_notices"
                " WHERE notice_type IN ('correction','cancellation')").fetchone()["c"],
            "alerts": conn.execute(
                "SELECT COUNT(*) c FROM alerts WHERE resolved=0").fetchone()["c"],
            "failures": conn.execute("SELECT COUNT(*) c FROM parse_failures").fetchone()["c"],
        }
        recent_runs = conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 8").fetchall()
        site_rows = conn.execute(
            """SELECT s.*,
                 (SELECT COUNT(*) FROM site_notices sn
                   WHERE sn.site_id=s.id AND sn.status='active') AS replica_count
               FROM sites s ORDER BY s.id""").fetchall()
        selector_versions = {}
        for s in site_rows:
            selectors = json.loads(s["selectors_json"] or "{}")
            selector_versions[s["id"]] = 2 if "bid-card" in str(selectors) else 1
    finally:
        conn.close()

    return render_template("dashboard.html", counts=counts,
                           recent_runs=recent_runs, site_rows=site_rows,
                           selector_versions=selector_versions)
