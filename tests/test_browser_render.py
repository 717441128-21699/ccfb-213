"""真实 Playwright 渲染测试：UI 登录、JS 渲染详情页、登录过期后重登。

单独成文件，方便用 -k browser 选择性执行（需要 chromium 内核）。
"""
from __future__ import annotations

from tender_intel.crawler.browser import BrowserSession, BrowserRenderError
from tender_intel.crawler.client import SignedClient
from tender_intel.crawler.parser import parse_detail_html
from tender_intel.db import connect
from tender_intel.mock.sites import SELECTORS_V1


def test_browser_renders_js_detail(env):
    conn = connect(env["db_file"])
    try:
        site = conn.execute("SELECT * FROM sites WHERE code='js'").fetchone()
    finally:
        conn.close()

    client = SignedClient(site["base_url"], site["secret"],
                          site["username"], site["password"])
    client.login()
    item = client.list_notices(1, 5)["items"][0]
    detail = client.notice_detail(item["remote_id"])

    with BrowserSession(site["base_url"], site["username"], site["password"]) as browser:
        page = browser.render_detail(item["remote_id"], api_json=detail)

    assert page.media_type == "text/html"
    assert "detail-title" in page.html  # 真实渲染出的 v1 DOM
    parsed = parse_detail_html(page.html, SELECTORS_V1)
    assert parsed.title and parsed.purchaser and parsed.budget_amount and parsed.deadline_at
    assert parsed.ok, parsed.failures
    client.close()


def test_browser_relogin_after_token_expired(env):
    conn = connect(env["db_file"])
    try:
        site = conn.execute("SELECT * FROM sites WHERE code='zj'").fetchone()
    finally:
        conn.close()

    browser = BrowserSession(site["base_url"], site["username"], site["password"])
    browser.start()
    try:
        client = SignedClient(site["base_url"], site["secret"],
                              site["username"], site["password"])
        client.login()
        item = client.list_notices(1, 3)["items"][0]
        # 先正常渲染一次
        page1 = browser.render_detail(item["remote_id"])
        assert "detail-title" in page1.html

        # 作废全部 Token：浏览器 localStorage 里的旧 Token 失效
        env["cluster"].expire_tokens("zj")
        detail = client.notice_detail(item["remote_id"])  # 客户端会自动重登
        page2 = browser.render_detail(item["remote_id"], api_json=detail)
        assert page2.re_logged_in is True
        assert "detail-title" in page2.html
        assert browser.ui_relogins >= 2
        client.close()
    finally:
        browser.close()


def test_no_browser_fallback_synthetic(env):
    conn = connect(env["db_file"])
    try:
        site = conn.execute("SELECT * FROM sites WHERE code='sc'").fetchone()
    finally:
        conn.close()
    client = SignedClient(site["base_url"], site["secret"],
                          site["username"], site["password"])
    client.login()
    item = client.list_notices(1, 3)["items"][0]
    detail = client.notice_detail(item["remote_id"])
    with BrowserSession(site["base_url"], site["username"], site["password"],
                        no_browser=True) as browser:
        page = browser.render_detail(item["remote_id"], api_json=detail)
    assert page.media_type == "text/synthetic-html"
    parsed = parse_detail_html(page.html, SELECTORS_V1)
    assert parsed.ok
    client.close()
