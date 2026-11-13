"""单元测试：签名、金额/日期解析、轻量 DOM、真值库指纹与单调性。"""
from __future__ import annotations

import time

from tender_intel.clock import parse_amount, parse_deadline
from tender_intel.crawler.mini_dom import text_of
from tender_intel.mock.truth import (
    TruthStore, canonical_key_of, content_hash_of, format_amount_display,
)
from tender_intel.signing import build_sign, verify_sign


def test_sign_roundtrip_and_tamper():
    secret = "s3cr3t"
    ts = str(int(time.time()))
    sig = build_sign(secret, "GET", "/api/notices", ts, "n1")
    ok, _ = verify_sign(secret, "GET", "/api/notices", ts, "n1", sig)
    assert ok
    # 路径被篡改 → 验签失败
    ok2, reason = verify_sign(secret, "GET", "/api/admin", ts, "n1", sig)
    assert not ok2 and reason == "bad signature"
    # 过期时间戳
    old = str(int(time.time()) - 10_000)
    sig2 = build_sign(secret, "GET", "/x", old, "n2")
    ok3, reason3 = verify_sign(secret, "GET", "/x", old, "n2", sig2)
    assert not ok3 and "expired" in reason3


def test_amount_parser():
    assert parse_amount("预算金额：1,234.56万元")[0] == 12_345_600.0
    assert parse_amount("2.50亿元")[0] == 250_000_000.0
    assert parse_amount("￥88,000.00元")[0] == 88_000.0
    val, err = parse_amount("面议")
    assert val is None and err
    assert parse_amount("")[0] is None


def test_deadline_parser():
    iso, err = parse_deadline("截止时间：2026年12月05日 09:30")
    assert err is None and iso.startswith("2026-12-05T09:30")
    iso2, err2 = parse_deadline("2026-11-20T10:00:00Z")
    assert err2 is None and iso2 == "2026-11-20T10:00:00Z"
    val, err3 = parse_deadline("待定")
    assert val is None and err3


def test_mini_dom_v1_and_v2():
    html_v1 = """
    <html><body><div class="notice-detail">
      <h1 class="detail-title">某项目招标公告</h1>
      <div class="project-code">项目编号：JS2026001</div>
      <div class="purchaser">采购单位：市第一人民医院</div>
      <div class="budget">预算金额：320.00万元</div>
      <div class="deadline">截止时间：2026年12月01日 14:00</div>
    </div></body></html>"""
    assert text_of(html_v1, ".detail-title") == "某项目招标公告"
    assert text_of(html_v1, "div.purchaser") == "采购单位：市第一人民医院"

    html_v2 = """
    <section class="bid-detail">
      <h2 class="bid-title">另一个项目</h2>
      <span class="buyer-name">采购单位：县水利局</span>
      <span class="money-amount">预算金额：1.20亿元</span>
      <time class="close-time">截止时间：2026年11月28日 09:00</time>
    </section>"""
    assert text_of(html_v2, "section.bid-detail h2.bid-title") == "另一个项目"
    assert text_of(html_v2, ".money-amount") == "预算金额：1.20亿元"
    assert text_of(html_v2, ".not-exist") == ""


def test_amount_display_roundtrip():
    assert format_amount_display(3_200_000.0) == "320.00万元"
    assert format_amount_display(120_000_000.0) == "1.20亿元"
    amount, _ = parse_amount(format_amount_display(3_200_000.0))
    assert amount == 3_200_000.0


def test_truth_store_repost_monotonic_and_canonical(tmp_path):
    store = TruthStore(tmp_path / "truth.json")
    import random
    rng = random.Random(7)
    store.seed_initial(notice_count=6, seed=7)
    before = store.sim_now
    logs = store.tick(events=4, rng=rng)
    assert logs and store.sim_now > before

    # seq 持久化：重新 load 后继续递增不撞号
    remote_ids_before = {
        rep["remote_id"] for n in store.notices.values()
        for rep in n["replicas"].values()
    }
    reloaded = TruthStore.load(tmp_path / "truth.json")
    reloaded.tick(events=3, rng=random.Random(8))
    remote_ids_after = {
        rep["remote_id"] for n in reloaded.notices.values()
        for rep in n["replicas"].values()
    }
    assert remote_ids_before.issubset(remote_ids_after)
    # 同站 remote_id 唯一
    all_ids = [f"{code}:{rep['remote_id']}"
               for n in reloaded.notices.values()
               for code, rep in n["replicas"].items()]
    assert len(all_ids) == len(set(all_ids))

    # 转载公告跨站 canonical_key 相同
    for notice in reloaded.notices.values():
        if len(notice["replicas"]) >= 2:
            assert notice["canonical_key"] == canonical_key_of(
                notice["project_code"], notice["notice_type"],
                notice["content_hash"])
            assert content_hash_of(notice["title"], notice["purchaser"],
                                   notice["budget_amount"],
                                   notice["deadline_at"]) == notice["content_hash"]
            break
    else:
        raise AssertionError("种子数据里应存在跨站转载公告")
