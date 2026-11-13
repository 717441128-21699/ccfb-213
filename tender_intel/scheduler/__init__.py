"""任务调度：数据库驱动的间隔调度器，抓取任务以独立子进程运行。

- schedules 表存调度计划，Web 控制台可直接改；
- 子进程方式运行爬虫，崩溃不影响控制台；PID 落库支持取消；
- crawl_runs 带心跳，超过阈值没心跳的 running 任务判定为僵死并标记 failed，
  断点仍在 crawl_checkpoints 里，可一键 resume。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..clock import now_iso
from ..db import connect, init_db

logger = logging.getLogger(__name__)

HEARTBEAT_SEC = 10
STALE_RUN_SEC = 90
POLL_SEC = 10


class Scheduler:
    def __init__(self, db_file: str | None = None, poll_sec: int = POLL_SEC,
                 once: bool = False):
        self.db_file = db_file
        self.poll_sec = poll_sec
        self.once = once
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ---------------- 计划管理 ----------------
    @staticmethod
    def ensure_default_schedule(db_file: str | None = None) -> None:
        init_db(db_file)
        conn = connect(db_file)
        try:
            existing = conn.execute("SELECT COUNT(*) AS c FROM schedules").fetchone()
            if existing["c"] == 0:
                ts = now_iso()
                conn.execute(
                    """INSERT INTO schedules(site_code, mode, interval_minutes, enabled,
                          next_run_at, created_at) VALUES(NULL, 'incremental', 60, 1, ?, ?)""",
                    (ts, ts),
                )
                conn.commit()
        finally:
            conn.close()

    @staticmethod
    def spawn_crawl(mode: str, site_codes: list[str] | None = None,
                    no_browser: bool = False, resume_run_id: int | None = None,
                    db_file: str | None = None) -> int:
        """启动抓虫子进程，返回 run_id。

        resume_run_id 给定时复用该 run 记录（断点在 crawl_checkpoints 里）；
        否则先在库里占一条新的 running 记录。
        """
        init_db(db_file)
        conn = connect(db_file)
        try:
            if resume_run_id is not None:
                row = conn.execute(
                    "SELECT id FROM crawl_runs WHERE id=? "
                    "AND status IN ('running','failed','cancelled')",
                    (resume_run_id,)).fetchone()
                if not row:
                    raise ValueError(f"run {resume_run_id} 不可恢复")
                run_id = resume_run_id
                conn.execute(
                    "UPDATE crawl_runs SET status='running', started_at=?, error=NULL WHERE id=?",
                    (now_iso(), run_id))
            else:
                cur = conn.execute(
                    "INSERT INTO crawl_runs(mode, status, site_codes, started_at, stats_json)"
                    " VALUES(?,?,?,?,?)",
                    (mode, "running", json.dumps(site_codes or [], ensure_ascii=False),
                     now_iso(), "{}"),
                )
                run_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        # -I 隔离模式不把 cwd 加入 sys.path，这里把项目根显式传进去
        # （仍保留 -I 对 PYTHONPATH/用户 site 的隔离，防目录里的恶意模块注入）
        project_root = str(Path(__file__).resolve().parents[1].parent)
        cmd = [sys.executable, "-I", "-c",
               f"import sys; sys.path.insert(0, r'{project_root}');"
               "from tender_intel.cli import main; main()",
               "crawl-run", "--run-id", str(run_id), "--mode", mode]
        if no_browser:
            cmd.append("--no-browser")
        if site_codes:
            cmd += ["--sites", ",".join(site_codes)]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        proc = subprocess.Popen(cmd, creationflags=creationflags)

        conn = connect(db_file)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)",
                (f"run_pid:{run_id}", str(proc.pid)),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("已启动抓虫子进程 run_id=%s pid=%s mode=%s", run_id, proc.pid, mode)
        return run_id

    @staticmethod
    def cancel_run(run_id: int, db_file: str | None = None) -> bool:
        conn = connect(db_file)
        try:
            row = conn.execute(
                "SELECT v FROM kv WHERE k=?", (f"run_pid:{run_id}",)).fetchone()
            if not row:
                return False
            pid = int(row["v"])
        finally:
            conn.close()
        import signal
        try:
            if sys.platform == "win32":
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
                ctypes.windll.kernel32.CloseHandle(handle)
            else:
                import os
                os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError) as exc:
            logger.warning("取消 run %s 失败: %s", run_id, exc)
            return False
        conn = connect(db_file)
        try:
            conn.execute("DELETE FROM kv WHERE k=?", (f"run_pid:{run_id}",))
            conn.commit()
        finally:
            conn.close()
        return True

    @staticmethod
    def reap_stale_runs(db_file: str | None = None) -> list[int]:
        """把没心跳的 running 任务标记 failed（断点保留，可 resume）。"""
        conn = connect(db_file)
        stale = []
        try:
            rows = conn.execute(
                "SELECT id, started_at FROM crawl_runs WHERE status='running'").fetchall()
            now = time.time()
            for row in rows:
                hb = conn.execute(
                    "SELECT v FROM kv WHERE k=?", (f"heartbeat:{row['id']}",)).fetchone()
                if not hb:
                    continue
                if now - float(hb["v"]) > STALE_RUN_SEC:
                    conn.execute(
                        "UPDATE crawl_runs SET status='failed', finished_at=?,"
                        " error='心跳超时，进程疑似被杀（可断点续跑）' WHERE id=?",
                        (now_iso(), row["id"]))
                    conn.execute("DELETE FROM kv WHERE k=?", (f"run_pid:{row['id']}",))
                    stale.append(row["id"])
            conn.commit()
        finally:
            conn.close()
        return stale

    # ---------------- 主循环 ----------------
    def run_forever(self) -> None:
        logger.info("调度器启动，轮询间隔 %ss", self.poll_sec)
        while not self._stop.is_set():
            try:
                self.reap_stale_runs(self.db_file)
                self._tick_due_schedules()
            except Exception:
                logger.exception("调度轮询异常")
            if self.once:
                return
            self._stop.wait(self.poll_sec)

    def _tick_due_schedules(self) -> None:
        conn = connect(self.db_file)
        try:
            # 已有 running 任务时不叠加新任务
            running = conn.execute(
                "SELECT COUNT(*) AS c FROM crawl_runs WHERE status='running'").fetchone()
            if running["c"] > 0:
                return
            now = now_iso()
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?",
                (now,)).fetchall()
            due = []
            for row in rows:
                site_codes = [row["site_code"]] if row["site_code"] else None
                due.append((row, site_codes))
            conn.commit()
        finally:
            conn.close()

        for row, site_codes in due:
            run_id = self.spawn_crawl(
                row["mode"], site_codes=site_codes, db_file=self.db_file)
            conn = connect(self.db_file)
            try:
                from datetime import datetime, timedelta, timezone
                nxt = (datetime.now(timezone.utc)
                       + timedelta(minutes=row["interval_minutes"]))
                conn.execute(
                    "UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=?",
                    (now, nxt.strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]))
                conn.execute(
                    "INSERT INTO kv(k, v) VALUES(?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (f"schedule_run:{row['id']}", str(run_id)))
                conn.commit()
            finally:
                conn.close()


def heartbeat_loop(run_id: int, stop_event: threading.Event,
                   db_file: str | None = None) -> None:
    """抓虫子进程内部调用：定期写心跳。"""
    while not stop_event.wait(HEARTBEAT_SEC):
        conn = connect(db_file)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)",
                (f"heartbeat:{run_id}", str(time.time())),
            )
            conn.commit()
        finally:
            conn.close()
