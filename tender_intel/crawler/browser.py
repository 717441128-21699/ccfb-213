"""Playwright 渲染层：模拟真实用户 UI 登录，渲染 JS 详情页并保存原始 HTML 快照。

设计：
- 浏览器和 SignedClient 是两套独立登录态，浏览器里的 Token 过期时页面会渲染
  #auth-expired，此时重新走一次 UI 登录即可（真实站点的过期行为）；
- no_browser=True 时降级为用 API JSON 合成 HTML，保证无图形/无内核环境可跑，
  但快照会标注 media_type=text/synthetic-html。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BrowserRenderError(RuntimeError):
    pass


class RenderedPage:
    def __init__(self, html: str, media_type: str, re_logged_in: bool):
        self.html = html
        self.media_type = media_type
        self.re_logged_in = re_logged_in


class BrowserSession:
    def __init__(self, base_url: str, username: str, password: str,
                 no_browser: bool = False):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.no_browser = no_browser
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.ui_relogins = 0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self.no_browser:
            logger.info("已启用 --no-browser 降级模式，不启动 Playwright")
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserRenderError("未安装 playwright") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._ui_login()

    def _ui_login(self) -> None:
        page = self._page
        page.goto(self.base_url + "/login", wait_until="networkidle", timeout=20000)
        page.fill("#username", self.username)
        page.fill("#password", self.password)
        page.click("#login-form button[type=submit]")
        page.wait_for_url(self.base_url + "/", timeout=20000)
        page.wait_for_selector(".notice-card, .bid-card, #app", timeout=20000)
        self.ui_relogins += 1
        logger.info("%s 浏览器 UI 登录成功（第 %d 次）", self.base_url, self.ui_relogins)

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        finally:
            self._playwright = self._browser = self._context = self._page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- 渲染 ----------
    def render_detail(self, remote_id: str,
                      api_json: dict[str, Any] | None = None) -> RenderedPage:
        if self.no_browser:
            return RenderedPage(self._synthetic_html(api_json),
                                "text/synthetic-html", False)
        url = f"{self.base_url}/notice/{remote_id}"
        page = self._page
        page.goto(url, wait_until="networkidle", timeout=20000)

        re_logged_in = False
        expired = page.locator("#auth-expired")
        if expired.count() and expired.first.is_visible():
            logger.info("%s 浏览器登录态过期，重新 UI 登录", self.base_url)
            self._ui_login()
            re_logged_in = True
            page.goto(url, wait_until="networkidle", timeout=20000)

        # 等真正的详情容器出现（v1 / v2 两套 class）
        page.wait_for_selector(
            ".notice-detail, .bid-detail", timeout=20000,
        )
        html = page.content()
        return RenderedPage(html, "text/html", re_logged_in)

    @staticmethod
    def _synthetic_html(api_json: dict[str, Any] | None) -> str:
        """降级模式：按站点当前模板版本（v1/v2）合成 HTML。

        必须跟随 template_version，否则站点改版后选择器失效的行为无法在
        --no-browser 模式下复现。
        """
        if not api_json:
            return "<html><body><div class='notice-detail'></div></body></html>"
        if api_json.get("template_version") == 2:
            return f"""<html><body><section class="bid-detail">
<h2 class="bid-title">{api_json.get('title', '')}</h2>
<span class="prj-no">项目编号：{api_json.get('project_code', '')}</span><br>
<span class="buyer-name">采购单位：{api_json.get('purchaser', '')}</span><br>
<span class="money-amount">预算金额：{api_json.get('budget_display', '')}</span><br>
<time class="close-time">截止时间：{api_json.get('deadline_display', '')}</time>
</section></body></html>"""
        return f"""<html><body><div class="notice-detail">
<h1 class="detail-title">{api_json.get('title', '')}</h1>
<div class="project-code">项目编号：{api_json.get('project_code', '')}</div>
<div class="purchaser">采购单位：{api_json.get('purchaser', '')}</div>
<div class="budget">预算金额：{api_json.get('budget_display', '')}</div>
<div class="deadline">截止时间：{api_json.get('deadline_display', '')}</div>
</div></body></html>"""
