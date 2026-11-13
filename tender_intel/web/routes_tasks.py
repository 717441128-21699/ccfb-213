"""任务调度页：抓取计划、手动触发、取消、断点续跑、运行详情。"""
from __future__ import annotations

import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..clock import now_iso
from ..db import connect
from ..scheduler import Scheduler

bp = Blueprint("tasks", __name__, url_prefix="/tasks")


def _no_browser_flag() -> bool:
    return request.form.get("no_browser") == "on"


@bp.get("/")
def index():
    conn = connect()
    try:
        schedules = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        runs = conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 25").fetchall()
        sites = conn.execute(
            "SELECT code, name FROM sites WHERE enabled=1 ORDER BY id").fetchall()
    finally:
        conn.close()
    return render_template("tasks/index.html", schedules=schedules,
                           runs=runs, sites=sites)


@bp.post("/run")
def trigger_run():
    mode = request.form.get("mode", "incremental")
    sites_param = request.form.getlist("site_codes")
    site_codes = sites_param or None
    try:
        run_id = Scheduler.spawn_crawl(
            mode, site_codes=site_codes,
            no_browser=_no_browser_flag())
        flash(f"已提交抓取任务 run #{run_id}（{mode}）", "ok")
    except Exception as exc:
        flash(f"任务启动失败: {exc}", "err")
    return redirect(url_for("tasks.index"))


@bp.post("/runs/<int:run_id>/cancel")
def cancel(run_id: int):
    ok = Scheduler.cancel_run(run_id)
    if ok:
        conn = connect()
        try:
            conn.execute(
                "UPDATE crawl_runs SET status='cancelled', finished_at=? WHERE id=? AND status='running'",
                (now_iso(), run_id))
            conn.commit()
        finally:
            conn.close()
        flash(f"run #{run_id} 已取消，断点保留，可随时续跑", "ok")
    else:
        flash("进程已结束或 PID 不存在，无法取消", "err")
    return redirect(url_for("tasks.index"))


@bp.post("/runs/<int:run_id>/resume")
def resume(run_id: int):
    conn = connect()
    try:
        row = conn.execute("SELECT mode FROM crawl_runs WHERE id=?",
                           (run_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        flash("运行不存在", "err")
        return redirect(url_for("tasks.index"))
    try:
        Scheduler.spawn_crawl(
            row["mode"], resume_run_id=run_id,
            no_browser=_no_browser_flag())
        flash(f"run #{run_id} 已从断点续跑", "ok")
    except Exception as exc:
        flash(f"续跑失败: {exc}", "err")
    return redirect(url_for("tasks.index"))


@bp.get("/runs/<int:run_id>")
def detail(run_id: int):
    conn = connect()
    try:
        run = conn.execute("SELECT * FROM crawl_runs WHERE id=?",
                           (run_id,)).fetchone()
        if not run:
            flash("运行不存在", "err")
            return redirect(url_for("tasks.index"))
        checkpoints = conn.execute(
            """SELECT cp.*, s.code AS site_code FROM crawl_checkpoints cp
               JOIN sites s ON s.id=cp.site_id
               WHERE cp.run_id=? ORDER BY s.id""", (run_id,)).fetchall()
        run_alerts = conn.execute(
            """SELECT a.*, s.code AS site_code FROM alerts a
               LEFT JOIN sites s ON s.id=a.site_id
               WHERE a.run_id=? ORDER BY a.id DESC""", (run_id,)).fetchall()
        stats = json.loads(run["stats_json"] or "{}")
        failures = conn.execute(
            """SELECT pf.*, s.code FROM parse_failures pf
               JOIN sites s ON s.id=pf.site_id
               WHERE pf.run_id=? ORDER BY pf.id DESC LIMIT 50""",
            (run_id,)).fetchall()
    finally:
        conn.close()
    return render_template("tasks/detail.html", run=run, checkpoints=checkpoints,
                           run_alerts=run_alerts, stats=stats, failures=failures)


# ---------------- 调度计划 ----------------
@bp.post("/schedules")
def create_schedule():
    mode = request.form.get("mode", "incremental")
    interval = max(int(request.form.get("interval_minutes", 60)), 1)
    site_code = request.form.get("site_code") or None
    ts = now_iso()
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO schedules(site_code, mode, interval_minutes, enabled,
                  next_run_at, created_at) VALUES(?,?,?,1,?,?)""",
            (site_code, mode, interval, ts, ts))
        conn.commit()
    finally:
        conn.close()
    flash("调度计划已创建", "ok")
    return redirect(url_for("tasks.index"))


@bp.post("/schedules/<int:sched_id>/toggle")
def toggle_schedule(sched_id: int):
    conn = connect()
    try:
        conn.execute(
            "UPDATE schedules SET enabled = 1 - enabled WHERE id=?", (sched_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("tasks.index"))


@bp.post("/schedules/<int:sched_id>/delete")
def delete_schedule(sched_id: int):
    conn = connect()
    try:
        conn.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
        conn.commit()
    finally:
        conn.close()
    flash("调度计划已删除", "ok")
    return redirect(url_for("tasks.index"))
