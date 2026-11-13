"""抓取引擎：全量/增量可切换，按页落断点，中断后从断点继续。

增量纪律：
- 去重只认持久化的 (site_id, remote_id)，水位 max(published_at) 仅用于
  提前停止翻页，绝不能跳过 remote_id 去重判断；
- 翻页遇到整页都早于水位时才停止，防止同秒发布导致漏数据。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

from ..clock import now_iso
from ..config import PARSE_BREAKDOWN_MIN_PAGES, PARSE_BREAKDOWN_RATIO
from ..db import connect, init_db
from .browser import BrowserRenderError, BrowserSession
from .client import CrawlError, SignedClient
from .parser import parse_detail_html
from .repo import Repository

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    sites: int = 0
    pages: int = 0
    list_items: int = 0
    new_notices: int = 0
    updated_notices: int = 0
    canonical_new: int = 0
    parse_failures: int = 0
    delisted: int = 0
    rate_hits: int = 0
    relogins: int = 0
    alerts: int = 0
    per_site: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class CrawlEngine:
    def __init__(self, no_browser: bool = False, page_size: int = 10,
                 cancel_event: threading.Event | None = None,
                 db_file: str | None = None):
        self.no_browser = no_browser
        self.page_size = page_size
        self.cancel_event = cancel_event or threading.Event()
        self.db_file = db_file
        self.stats = RunStats()

    # ---------------- 运行入口 ----------------
    def run(self, mode: str, site_codes: list[str] | None = None,
            resume_run_id: int | None = None) -> int:
        if mode not in ("full", "incremental"):
            raise ValueError(f"unknown mode {mode}")
        init_db(self.db_file)
        conn = connect(self.db_file)
        run_id = self._start_run(conn, mode, site_codes, resume_run_id)
        repo = Repository(conn)
        try:
            sites = repo.list_sites(enabled_only=True)
            if site_codes:
                wanted = set(site_codes)
                sites = [s for s in sites if s["code"] in wanted]
            self.stats.sites = len(sites)

            for site in sites:
                if self.cancel_event.is_set():
                    self._finish(conn, run_id, "cancelled")
                    return run_id
                try:
                    self._crawl_site(conn, repo, site, mode, run_id)
                except CrawlError as exc:
                    logger.exception("站点 %s 抓取失败: %s", site["code"], exc)
                    repo.add_alert(
                        site["id"], run_id, exc.type, "critical",
                        f"站点 {site['code']} 抓取中断: {exc}",
                        {"status": exc.status})
                    self.stats.alerts += 1
                    conn.commit()
            # 全部站点跑完再兜底一次，处理跨站顺序导致的漏挂
            linked = repo.link_pending_children(now_iso())
            if linked:
                conn.commit()
            self._finish(conn, run_id, "succeeded")
            return run_id
        except Exception as exc:  # 断点已保存，下次可 resume
            logger.exception("运行 %s 异常退出: %s", run_id, exc)
            self._finish(conn, run_id, "failed", error=str(exc))
            raise
        finally:
            conn.close()

    # ---------------- run 状态 ----------------
    def _start_run(self, conn, mode, site_codes, resume_run_id) -> int:
        ts = now_iso()
        if resume_run_id:
            row = conn.execute(
                "SELECT * FROM crawl_runs WHERE id=? AND status IN ('running','failed','cancelled')",
                (resume_run_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"run {resume_run_id} 不可恢复")
            conn.execute(
                "UPDATE crawl_runs SET status='running', started_at=?, error=NULL WHERE id=?",
                (ts, resume_run_id))
            return resume_run_id
        cur = conn.execute(
            "INSERT INTO crawl_runs(mode, status, site_codes, started_at, stats_json)"
            " VALUES(?,?,?,?,?)",
            (mode, "running", json.dumps(site_codes or [], ensure_ascii=False),
             ts, "{}"),
        )
        conn.commit()
        return cur.lastrowid

    def _finish(self, conn, run_id: int, status: str, error: str | None = None) -> None:
        conn.execute(
            "UPDATE crawl_runs SET status=?, finished_at=?, stats_json=?, error=? WHERE id=?",
            (status, now_iso(),
             json.dumps(self.stats.as_dict(), ensure_ascii=False), error, run_id),
        )
        conn.commit()

    # ---------------- 单站抓取 ----------------
    def _crawl_site(self, conn, repo: Repository, site, mode: str, run_id: int) -> None:
        site_id = site["id"]
        selectors = json.loads(site["selectors_json"] or "{}")
        client = SignedClient(site["base_url"], site["secret"],
                              site["username"], site["password"])
        browser = BrowserSession(site["base_url"], site["username"],
                                 site["password"], no_browser=self.no_browser)
        site_stat = {"pages": 0, "items": 0, "new": 0, "updated": 0,
                     "parse_failures": 0, "empty_core_pages": 0, "detail_pages": 0}
        self.stats.per_site[site["code"]] = site_stat
        try:
            client.login()
            browser.start()

            checkpoint = repo.get_checkpoint(run_id, site_id)
            page = checkpoint["page"] if checkpoint else 1
            seen_remote_ids = checkpoint["seen_remote_ids"] if checkpoint else set()
            watermark = None if mode == "full" else repo.get_watermark(site_id)
            max_published = watermark
            empty_stop = False

            while True:
                if self.cancel_event.is_set():
                    repo.save_checkpoint(run_id, site_id, "list", page,
                                         None, seen_remote_ids, now_iso())
                    conn.commit()
                    return

                data = client.list_notices(page, self.page_size)
                items = data.get("items", [])
                site_stat["pages"] += 1
                self.stats.pages += 1
                if not items:
                    break

                # 增量：整页都早于/等于水位 → 后面没有新东西了
                if mode == "incremental" and watermark:
                    older = all(
                        (it.get("published_at") or "") <= watermark for it in items
                    )
                    if older:
                        empty_stop = True
                        break

                for item in items:
                    remote_id = item["remote_id"]
                    seen_remote_ids.add(remote_id)
                    site_stat["items"] += 1
                    self.stats.list_items += 1
                    published = item.get("published_at")
                    if published and (max_published is None or published > max_published):
                        max_published = published

                    # 增量去重锚点：remote_id 已见过就只刷新心跳，不重复渲染详情
                    existing = conn.execute(
                        "SELECT id FROM site_notices WHERE site_id=? AND remote_id=?",
                        (site_id, remote_id),
                    ).fetchone()
                    if mode == "incremental" and existing:
                        conn.execute(
                            "UPDATE site_notices SET last_seen_at=?, fetched_at=? WHERE id=?",
                            (now_iso(), now_iso(), existing["id"]))
                        site_stat["updated"] += 1
                        continue

                    self._crawl_detail(conn, repo, site, selectors, client,
                                       browser, item, run_id, site_stat)

                repo.save_checkpoint(run_id, site_id, "list", page,
                                     items[-1]["remote_id"], seen_remote_ids, now_iso())
                conn.commit()

                total = data.get("total", 0)
                if page * self.page_size >= total:
                    break
                page += 1

            # 全量模式：真值有、本次没抓到的在线副本 → 下架
            if mode == "full":
                delisted = repo.mark_delisted(site_id, seen_remote_ids, now_iso())
                site_stat["delisted"] = delisted
                self.stats.delisted += delisted

            if max_published:
                repo.set_watermark(site_id, max_published)
            repo.link_pending_children(now_iso())
            repo.clear_checkpoint(run_id, site_id)
            conn.commit()

            self._maybe_parse_breakdown_alert(repo, site, run_id, site_stat,
                                              empty_stop, watermark)
            self.stats.rate_hits += client.rate_hits
            self.stats.relogins += client.relogins + browser.ui_relogins
            if client.rate_hits:
                repo.add_alert(site_id, run_id, "rate_limited", "warning",
                               f"站点 {site['code']} 运行中触发 {client.rate_hits} 次限流",
                               {"rate_hits": client.rate_hits})
                self.stats.alerts += 1
                conn.commit()
        finally:
            client.close()
            browser.close()

    def _crawl_detail(self, conn, repo: Repository, site, selectors: dict,
                      client: SignedClient, browser: BrowserSession,
                      list_item: dict, run_id: int, site_stat: dict) -> None:
        remote_id = list_item["remote_id"]
        ts = now_iso()
        detail = client.notice_detail(remote_id)
        try:
            rendered = browser.render_detail(remote_id, api_json=detail)
            html, media_type = rendered.html, rendered.media_type
        except BrowserRenderError as exc:
            # 浏览器渲染失败不致命：降级合成 HTML，并记告警
            repo.add_alert(site["id"], run_id, "crawl_error", "warning",
                           f"{site['code']}:{remote_id} 浏览器渲染失败，已降级: {exc}")
            self.stats.alerts += 1
            html = BrowserSession._synthetic_html(detail)
            media_type = "text/synthetic-html"

        parsed = parse_detail_html(html, selectors)
        site_stat["detail_pages"] += 1
        if parsed.empty_core:
            site_stat["empty_core_pages"] += 1
        if parsed.failures:
            repo.add_parse_failures(site["id"], remote_id, parsed.failures, run_id, ts)
            site_stat["parse_failures"] += len(parsed.failures)
            self.stats.parse_failures += len(parsed.failures)

        summary = repo.upsert_notice(
            site, list_item, detail, parsed,
            list_json=json.dumps(list_item, ensure_ascii=False),
            html=html, html_media=media_type, ts=ts,
        )
        if summary["is_new"]:
            site_stat["new"] += 1
            self.stats.new_notices += 1
        else:
            site_stat["updated"] += 1
            self.stats.updated_notices += 1
        if summary["canonical_new"]:
            self.stats.canonical_new += 1

    def _maybe_parse_breakdown_alert(self, repo: Repository, site, run_id: int,
                                     site_stat: dict, empty_stop: bool,
                                     watermark: str | None) -> None:
        """字段大面积为空 → 改版告警。增量空跑（没有新详情页）不算。"""
        detail_pages = site_stat["detail_pages"]
        if detail_pages < PARSE_BREAKDOWN_MIN_PAGES:
            return
        ratio = site_stat["empty_core_pages"] / detail_pages
        if ratio >= PARSE_BREAKDOWN_RATIO:
            repo.add_alert(
                site["id"], run_id, "parse_breakdown", "critical",
                f"站点 {site['code']} {detail_pages} 个详情页中 "
                f"{site_stat['empty_core_pages']} 个核心字段为空（{ratio:.0%}），疑似改版",
                {"ratio": round(ratio, 3), "detail_pages": detail_pages,
                 "empty_core_pages": site_stat["empty_core_pages"]})
            self.stats.alerts += 1


def run_crawl(mode: str, site_codes: list[str] | None = None,
              no_browser: bool = False, resume_run_id: int | None = None,
              cancel_event: threading.Event | None = None,
              db_file: str | None = None) -> int:
    engine = CrawlEngine(no_browser=no_browser, cancel_event=cancel_event,
                         db_file=db_file)
    return engine.run(mode, site_codes=site_codes, resume_run_id=resume_run_id)
