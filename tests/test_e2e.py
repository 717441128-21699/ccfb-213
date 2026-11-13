"""端到端：mock 站群 → 抓取 → 落库 → 与真值完全对账。

默认走 --no-browser（API 合成 HTML），真实 Playwright 渲染在 test_browser_render 单独验证。
"""
from __future__ import annotations

import json
import threading

import pytest

from tender_intel.crawler.engine import CrawlEngine
from tender_intel.db import connect
from tender_intel.mock.sites import MOCK_SITES, SELECTORS_V2
from tender_intel.mock.truth import TruthStore
from tender_intel.reconcile import reconcile


def _crawl(env, mode, sites=None, resume=None, cancel=None):
    engine = CrawlEngine(no_browser=True, db_file=env["db_file"],
                         cancel_event=cancel or threading.Event())
    return engine.run(mode, site_codes=sites, resume_run_id=resume)


def test_full_crawl_matches_truth(env):
    run_id = _crawl(env, "full")
    conn = connect(env["db_file"])
    try:
        run = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "succeeded"
        # 每个启用站点都抓到了副本
        per_site = json.loads(run["stats_json"])["per_site"]
        assert set(per_site) == {s.code for s in MOCK_SITES}
        assert all(v["detail_pages"] > 0 for v in per_site.values())
        # 每条公告都有两份快照
        snap_count = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        rep_count = conn.execute(
            "SELECT COUNT(*) c FROM site_notices WHERE status='active'").fetchone()["c"]
        assert snap_count == rep_count * 2
        # 解析失败表应为空（mock v1 模板字段完整）
        assert conn.execute("SELECT COUNT(*) c FROM parse_failures").fetchone()["c"] == 0
    finally:
        conn.close()

    report = reconcile(env["db_file"], env["truth_file"])
    assert report.ok, report.as_dict()
    assert report.missing == [] and report.extra == []
    assert report.field_mismatches == []
    assert report.dedup_errors == [] and report.parent_errors == []


def test_cross_site_dedup_and_correction_linking(env):
    """跨站转载必须合并为同一主记录；更正/废标挂到原公告下。"""
    _crawl(env, "full")
    store = TruthStore.load(env["truth_file"])
    view = store.truth_view()
    conn = connect(env["db_file"])
    try:
        # 找一个被多站转载的招标公告
        multi = [n for n in store.notices.values()
                 if n["notice_type"] == "tender" and len(n["replicas"]) >= 2]
        assert multi, "真值中应存在跨站转载"
        notice = multi[0]
        rows = conn.execute(
            "SELECT DISTINCT canonical_id FROM site_notices sn JOIN sites s ON s.id=sn.site_id"
            " WHERE s.code IN ({}) AND sn.remote_id IN ({})".format(
                ",".join("?" * len(notice["replicas"])),
                ",".join("?" * len(notice["replicas"]))),
            ([*notice["replicas"].keys()]
             + [r["remote_id"] for r in notice["replicas"].values()]),
        ).fetchall()
        assert len(rows) == 1, "同一公告的跨站副本必须指向同一 canonical_id"

        # 更正/废标挂接
        children = [n for n in store.notices.values()
                    if n["notice_type"] in ("correction", "cancellation")]
        assert children
        for child in children[:3]:
            row = conn.execute(
                "SELECT id, parent_id FROM canonical_notices WHERE fingerprint=?",
                (child["canonical_key"],)).fetchone()
            assert row is not None
            parent = conn.execute(
                "SELECT id FROM canonical_notices WHERE fingerprint=?",
                (store.notices[child["parent_uid"]]["canonical_key"],)).fetchone()
            assert row["parent_id"] == parent["id"]
    finally:
        conn.close()


def test_incremental_after_tick_still_matches_truth(env):
    """核心验收：tick 让真值变化后，增量跑完公告库与真值完全对上。"""
    _crawl(env, "full")
    # tick 几次：新增、转载、更正、废标、下架都可能发生
    for seed in (101, 202, 303):
        env["cluster"].trigger_tick(events=4, seed=seed)
    run_id = _crawl(env, "incremental")
    conn = connect(env["db_file"])
    try:
        run = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "succeeded"
    finally:
        conn.close()

    report = reconcile(env["db_file"], env["truth_file"])
    assert report.ok, report.as_dict()


def test_incremental_is_idempotent(env):
    """连续两次增量，第二次不应产生重复数据。"""
    _crawl(env, "full")
    env["cluster"].trigger_tick(events=3, seed=555)
    _crawl(env, "incremental")
    conn = connect(env["db_file"])
    try:
        count_first = conn.execute("SELECT COUNT(*) c FROM site_notices").fetchone()["c"]
    finally:
        conn.close()
    _crawl(env, "incremental")
    conn = connect(env["db_file"])
    try:
        count_second = conn.execute("SELECT COUNT(*) c FROM site_notices").fetchone()["c"]
        assert count_first == count_second
        # 无重复 (site_id, remote_id) 由唯一约束兜底，这里再验主记录不重复
        dup = conn.execute(
            "SELECT fingerprint, COUNT(*) c FROM canonical_notices"
            " GROUP BY fingerprint HAVING c > 1").fetchall()
        assert not dup
    finally:
        conn.close()
    assert reconcile(env["db_file"], env["truth_file"]).ok


def test_resume_from_checkpoint(env):
    """抓取中途取消 → 断点保留 → resume 跑完后对账一致。"""
    cancel = threading.Event()
    engine = CrawlEngine(no_browser=True, db_file=env["db_file"],
                         cancel_event=cancel)

    # 在第一个站点抓 1~2 页后取消：用补丁拦截 list_notices 触发 cancel
    orig = engine.run
    called = {"n": 0}

    conn = connect(env["db_file"])
    try:
        total_before = conn.execute(
            "SELECT COUNT(*) c FROM crawl_runs WHERE status='succeeded'").fetchone()["c"]
    finally:
        conn.close()

    # 直接用底层方法跑一个站，手动在循环中取消比较难，改为：启动后立刻取消
    # （首条断点会在第一页落库后写入）
    from tender_intel.crawler import engine as engine_mod
    real_list = engine_mod.SignedClient.list_notices

    def patched_list(self, page, size=10):
        result = real_list(self, page, size)
        called["n"] += 1
        if called["n"] >= 1:
            cancel.set()
        return result

    engine_mod.SignedClient.list_notices = patched_list
    try:
        run_id = engine.run("full")
    finally:
        engine_mod.SignedClient.list_notices = real_list

    conn = connect(env["db_file"])
    try:
        run = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "cancelled"
        cp = conn.execute(
            "SELECT COUNT(*) c FROM crawl_checkpoints WHERE run_id=?",
            (run_id,)).fetchone()["c"]
        assert cp >= 1
    finally:
        conn.close()

    # 断点续跑
    _crawl(env, "full", resume=run_id)
    report = reconcile(env["db_file"], env["truth_file"])
    assert report.ok, report.as_dict()


def test_parse_breakdown_alert_on_redesign(env):
    """站点改版（class 全换）→ 核心字段大面积为空 → 产生 parse_breakdown 告警。"""
    target = "gd"
    env["cluster"].set_template(target, 2)  # mock 输出 v2 class，但库里选择器还是 v1
    _crawl(env, "full", sites=[target])
    conn = connect(env["db_file"])
    try:
        alert = conn.execute(
            "SELECT * FROM alerts WHERE alert_type='parse_breakdown' ORDER BY id DESC"
        ).fetchall()
        assert alert, "改版后必须产生 parse_breakdown 告警"
        assert any(target in (a["message"] or "") for a in alert)
        # 解析失败表有对应记录
        failures = conn.execute(
            "SELECT COUNT(*) c FROM parse_failures pf JOIN sites s ON s.id=pf.site_id"
            " WHERE s.code=?", (target,)).fetchone()["c"]
        assert failures > 0
    finally:
        conn.close()

    # 控制台升级选择器到 v2 后，再抓一次，不再有新的改版告警，且对账恢复一致
    conn = connect(env["db_file"])
    try:
        conn.execute("UPDATE sites SET selectors_json=? WHERE code=?",
                     (json.dumps(SELECTORS_V2, ensure_ascii=False), target))
        conn.commit()
    finally:
        conn.close()
    _crawl(env, "full", sites=[target])
    assert reconcile(env["db_file"], env["truth_file"]).ok
    env["cluster"].set_template(target, 1)  # 还原


def test_token_expiry_relogin(env):
    """Token 被站点作废后，客户端自动重登并重试成功。"""
    from tender_intel.crawler.client import SignedClient
    site_row = None
    conn = connect(env["db_file"])
    try:
        site_row = conn.execute("SELECT * FROM sites WHERE code='js'").fetchone()
    finally:
        conn.close()
    client = SignedClient(site_row["base_url"], site_row["secret"],
                          site_row["username"], site_row["password"])
    client.login()
    data = client.list_notices(1, 5)
    assert data["items"]
    env["cluster"].expire_tokens("js")
    # 旧 Token 请求 → 401 → 自动重登重试，调用方无感
    data2 = client.list_notices(1, 5)
    assert data2["items"]
    assert client.relogins >= 2
    client.close()


def test_rate_limit_returns_429_then_retry(env):
    """超过每分钟阈值 → 429 + Retry-After，客户端退避后重试成功。"""
    import requests
    from tender_intel.signing import sign_headers
    import uuid
    site = MOCK_SITES[2]  # gd 限流 20/分钟
    base = env["base_urls"][site.code]
    # 登录拿 token
    login = requests.post(
        base + "/api/login",
        headers=sign_headers(site.secret, "POST", "/api/login", uuid.uuid4().hex),
        json={"username": "demo", "password": "demo123"}, timeout=15)
    token = login.json()["token"]
    headers = sign_headers(site.secret, "GET", "/api/notices", uuid.uuid4().hex)
    headers["Authorization"] = f"Bearer {token}"
    statuses = []
    for _ in range(site.rate_limit_per_min + 3):
        resp = requests.get(base + "/api/notices",
                            params={"page": 1, "size": 1}, headers=headers, timeout=15)
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            assert resp.headers.get("Retry-After")
            break
    assert 429 in statuses
