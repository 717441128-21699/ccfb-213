"""四个模拟省份站点的静态定义。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MockSite:
    code: str
    name: str
    port: int
    secret: str
    rate_limit_per_min: int


# 速率限制刻意设小，方便测试里打满 429
MOCK_SITES: list[MockSite] = [
    MockSite("js", "江苏省模拟招投标公共服务平台", 8301, "secret-js-2026", 30),
    MockSite("zj", "浙江省模拟政府采购网", 8302, "secret-zj-2026", 24),
    MockSite("gd", "广东省模拟公共资源交易中心", 8303, "secret-gd-2026", 20),
    MockSite("sc", "四川省模拟采购公告平台", 8304, "secret-sc-2026", 26),
]

SITE_BY_CODE = {s.code: s for s in MOCK_SITES}
SITE_CODES = [s.code for s in MOCK_SITES]

# v1 页面 CSS 选择器（数据库 sites.selectors_json 初始值，与 mock 模板 v1 对齐）
SELECTORS_V1 = {
    "list_item": ".notice-card",
    "detail_title": ".detail-title",
    "detail_purchaser": ".purchaser",
    "detail_budget": ".budget",
    "detail_deadline": ".deadline",
    "detail_code": ".project-code",
}

# 改版后 v2 的选择器（字段 class 全部改名），控制台升级站点配置时使用
SELECTORS_V2 = {
    "list_item": "article.bid-card",
    "detail_title": "h2.bid-title",
    "detail_purchaser": "span.buyer-name",
    "detail_budget": "span.money-amount",
    "detail_deadline": "time.close-time",
    "detail_code": "span.prj-no",
}
