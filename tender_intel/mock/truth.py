"""真值库（Ground Truth）：mock 站群唯一的数据真相源。

所有 mock 站点的接口都从这里读数据；tick 让数据随模拟时间演变。
对账时直接读取本文件，验证爬虫库与真值是否完全一致。

关键纪律（踩坑经验）：
1. seq 计数器持久化在文件里，每次进程启动/tick 前必须续算，否则撞码覆盖；
2. 跨站转载的 published_at 取目标站当时时钟（sim_now），保持各站时间单调；
3. 更正/废标公告同步到父公告的全部副本站点，各站 origin_remote_id 指向本站父副本。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..clock import to_iso
from ..config import truth_path
from .locking import FileLock, process_lock
from .sites import SITE_CODES

SIM_START = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

_PURCHASERS = [
    "市第一人民医院", "省交通工程建设局", "区教育装备中心", "市政务服务管理局",
    "县水利局", "省大数据中心", "市轨道交通集团", "区城市管理局",
    "省图书馆", "市公安局交通警察支队", "县农业农村局", "高新区管委会",
]
_GOODS = [
    "智慧校园信息化建设项目", "办公自动化设备采购", "市政道路养护工程",
    "云平台扩容服务项目", "医疗设备更新采购", "食堂食材配送服务",
    "数据中心机房改造工程", "安防监控系统集成项目", "物业管理服务采购",
    "检验检测仪器采购", "城市绿化提升工程", "档案数字化加工服务",
]


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def content_hash_of(title: str, purchaser: str, amount: float, deadline: str) -> str:
    raw = f"{title}|{purchaser}|{amount:.2f}|{deadline}"
    return _sha1(raw)[:16]


def canonical_key_of(project_code: str, notice_type: str, content_hash: str) -> str:
    return _sha1(f"{project_code}|{notice_type}|{content_hash}")[:16]


class TruthStore:
    """真值库的内存表示 + JSON 持久化（原子写）。

    同进程内通过 _shared 缓存共享单例：每次 load() 检查文件 mtime，
    外部/其他线程写入后自动重载，避免多站点线程各自读到陈旧副本。
    """

    _shared: dict[str, "TruthStore"] = {}
    _shared_mtime: dict[str, float] = {}

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else truth_path()
        self.sim_now: datetime = SIM_START
        self.seq: dict[str, int] = {"notice": 0, "replica": 0}
        # uid -> notice dict
        self.notices: dict[str, dict] = {}
        # site_code -> {"template_version": int, "token_epoch": int}
        self.site_admin: dict[str, dict] = {
            code: {"template_version": 1, "token_epoch": 1} for code in SITE_CODES
        }

    # ---------- 持久化 ----------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "TruthStore":
        p = Path(path) if path else truth_path()
        key = str(p.resolve())
        if not p.exists():
            return cls(p)
        mtime = p.stat().st_mtime
        cached = cls._shared.get(key)
        if cached is not None and cls._shared_mtime.get(key) == mtime:
            return cached
        store = cls(p)
        with FileLock(p), process_lock():
            for attempt in range(3):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    break
                except json.JSONDecodeError:
                    if attempt == 2:
                        raise
                    import time as _time
                    _time.sleep(0.05)
        store.sim_now = datetime.fromisoformat(data["sim_now"])
        store.seq = data["seq"]
        store.notices = data["notices"]
        for code in SITE_CODES:
            store.site_admin.setdefault(code, {"template_version": 1, "token_epoch": 1})
            store.site_admin[code].setdefault("template_version", 1)
            store.site_admin[code].setdefault("token_epoch", 1)
        cls._shared[key] = store
        cls._shared_mtime[key] = mtime
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sim_now": to_iso(self.sim_now),
            "seq": self.seq,
            "notices": self.notices,
            "site_admin": self.site_admin,
        }
        key = str(self.path.resolve())
        with FileLock(self.path), process_lock():
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            # 刷新本进程缓存的 mtime，后续 load() 直接命中
            TruthStore._shared[key] = self
            TruthStore._shared_mtime[key] = self.path.stat().st_mtime

    # ---------- 基础工具 ----------
    def _next(self, kind: str) -> int:
        self.seq[kind] += 1
        return self.seq[kind]

    def _new_remote_id(self, site_code: str) -> str:
        n = self._next("replica")
        return f"{site_code}-{n:08d}"

    def _new_notice_uid(self) -> str:
        return f"uid-{self._next('notice'):08d}"

    def active_replicas(self, site_code: str) -> list[dict]:
        """某站点当前在线的全部副本（按发布时间倒序）。"""
        out = []
        for notice in self.notices.values():
            rep = notice["replicas"].get(site_code)
            if rep and rep["status"] == "active":
                out.append({"notice": notice, "replica": rep})
        out.sort(key=lambda x: x["replica"]["published_at"], reverse=True)
        return out

    def list_page(self, site_code: str, page: int, size: int) -> dict:
        items = self.active_replicas(site_code)
        total = len(items)
        start = (page - 1) * size
        rows = items[start:start + size]
        return {
            "page": page, "size": size, "total": total,
            "items": [
                {
                    "remote_id": r["replica"]["remote_id"],
                    "title": r["notice"]["title"],
                    "notice_type": r["notice"]["notice_type"],
                    "published_at": r["replica"]["published_at"],
                    "url": f"/notice/{r['replica']['remote_id']}",
                }
                for r in rows
            ],
        }

    def get_replica(self, site_code: str, remote_id: str) -> dict | None:
        for notice in self.notices.values():
            rep = notice["replicas"].get(site_code)
            if rep and rep["remote_id"] == remote_id:
                return {"notice": notice, "replica": rep}
        return None

    # ---------- 数据生成 ----------
    def _make_tender(self, rng: random.Random, primary: str) -> dict:
        goods = rng.choice(_GOODS)
        purchaser = rng.choice(_PURCHASERS)
        project_code = (
            f"{primary.upper()}{self.sim_now.year}{rng.randint(1000, 9999)}-"
            f"{rng.randint(100, 999)}"
        )
        title = f"{purchaser}{goods}招标公告"
        # 金额按万元/亿元的两位小数生成，保证展示口径与真值可无损互转
        if rng.random() < 0.3:
            budget_yuan = round(rng.uniform(0.5, 8.0), 2) * 100_000_000
        else:
            budget_yuan = round(rng.uniform(20.0, 4800.0), 2) * 10_000
        budget_yuan = round(budget_yuan, 2)
        deadline = self.sim_now + timedelta(days=rng.randint(7, 45),
                                            hours=rng.randint(0, 8))
        deadline_iso = to_iso(deadline)
        chash = content_hash_of(title, purchaser, budget_yuan, deadline_iso)
        uid = self._new_notice_uid()
        notice = {
            "uid": uid,
            "canonical_key": canonical_key_of(project_code, "tender", chash),
            "project_code": project_code,
            "title": title,
            "notice_type": "tender",
            "parent_uid": None,
            "purchaser": purchaser,
            "budget_amount": budget_yuan,
            "deadline_at": deadline_iso,
            "content_hash": chash,
            "replicas": {},
        }
        self._attach_replica(notice, primary)
        self.notices[uid] = notice
        return notice

    def _attach_replica(self, notice: dict, site_code: str,
                        origin_remote_id: str | None = None) -> dict:
        """在目标站挂副本；发布时间取目标站当前时钟（sim_now 全局单调）。"""
        remote_id = self._new_remote_id(site_code)
        rep = {
            "remote_id": remote_id,
            "site_code": site_code,
            "published_at": to_iso(self.sim_now),
            "first_seen_at": to_iso(self.sim_now),
            "status": "active",
            "origin_remote_id": origin_remote_id,
        }
        notice["replicas"][site_code] = rep
        return rep

    def _tender_notices(self) -> list[dict]:
        return [n for n in self.notices.values() if n["notice_type"] == "tender"]

    # ---------- 演变事件 ----------
    def event_publish(self, rng: random.Random) -> str:
        primary = rng.choice(SITE_CODES)
        notice = self._make_tender(rng, primary)
        return f"publish: {notice['replicas'][primary]['remote_id']} {notice['title']}"

    def event_repost(self, rng: random.Random) -> str | None:
        candidates = [n for n in self._tender_notices()
                      if len(n["replicas"]) < len(SITE_CODES)]
        if not candidates:
            return None
        notice = rng.choice(candidates)
        target = rng.choice([c for c in SITE_CODES if c not in notice["replicas"]])
        rep = self._attach_replica(notice, target)
        return f"repost: {notice['uid']} -> {target} ({rep['remote_id']})"

    def _make_child(self, rng: random.Random, parent: dict, kind: str) -> dict:
        suffix = "更正公告" if kind == "correction" else "废标公告"
        title = parent["title"].replace("招标公告", suffix)
        # 更正公告微调金额/截止时间，产生字段变更
        budget = parent["budget_amount"]
        deadline = parent["deadline_at"]
        if kind == "correction":
            # 按万元取整，保证「万元两位小数」展示口径可无损还原
            budget = round(round(parent["budget_amount"] * rng.uniform(0.95, 1.08) / 10_000), 2) * 10_000
            deadline = to_iso(
                datetime.fromisoformat(parent["deadline_at"]) + timedelta(days=rng.randint(1, 10))
            )
        chash = content_hash_of(title, parent["purchaser"], budget, deadline)
        uid = self._new_notice_uid()
        child = {
            "uid": uid,
            "canonical_key": canonical_key_of(parent["project_code"], kind, chash),
            "project_code": parent["project_code"],
            "title": title,
            "notice_type": kind,
            "parent_uid": parent["uid"],
            "purchaser": parent["purchaser"],
            "budget_amount": budget,
            "deadline_at": deadline,
            "content_hash": chash,
            "replicas": {},
        }
        # 同步到父公告的全部在线副本站点，origin_remote_id 指向本站父副本
        for site_code, parent_rep in list(parent["replicas"].items()):
            if parent_rep["status"] != "active":
                continue
            self._attach_replica(child, site_code,
                                 origin_remote_id=parent_rep["remote_id"])
        self.notices[uid] = child
        return child

    def event_correction(self, rng: random.Random) -> str | None:
        parents = self._tender_notices()
        if not parents:
            return None
        parent = rng.choice(parents)
        child = self._make_child(rng, parent, "correction")
        sites = ",".join(child["replicas"].keys())
        return f"correction: {parent['uid']} -> {child['uid']} @ {sites}"

    def event_cancellation(self, rng: random.Random) -> str | None:
        # 只挑还没有废标公告的
        cancelled_parents = {
            n["parent_uid"] for n in self.notices.values()
            if n["notice_type"] == "cancellation"
        }
        parents = [n for n in self._tender_notices() if n["uid"] not in cancelled_parents]
        if not parents:
            return None
        parent = rng.choice(parents)
        child = self._make_child(rng, parent, "cancellation")
        parent["status"] = "cancelled"
        sites = ",".join(child["replicas"].keys())
        return f"cancellation: {parent['uid']} -> {child['uid']} @ {sites}"

    def event_delist(self, rng: random.Random) -> str | None:
        """撤下某站的一个副本（站点侧下架，真值仍保留）。"""
        options = []
        for notice in self.notices.values():
            for code, rep in notice["replicas"].items():
                if rep["status"] == "active":
                    options.append((notice, code, rep))
        if not options:
            return None
        notice, code, rep = rng.choice(options)
        rep["status"] = "delisted"
        rep["delisted_at"] = to_iso(self.sim_now)
        return f"delist: {rep['remote_id']} @ {code}"

    def tick(self, events: int = 3, rng: random.Random | None = None,
             allow_delist: bool = True) -> list[str]:
        """推进模拟时钟并随机产生 events 个演变事件。"""
        with process_lock():
            rng = rng or random.Random()
            logs: list[str] = []
            self.sim_now += timedelta(hours=rng.randint(2, 8))
            weights = [
                ("publish", 0.40), ("repost", 0.25),
                ("correction", 0.18), ("cancellation", 0.10), ("delist", 0.07),
            ]
            for _ in range(events):
                kind = rng.choices([w[0] for w in weights],
                                   weights=[w[1] for w in weights])[0]
                if kind == "delist" and not allow_delist:
                    kind = "publish"
                handler = {
                    "publish": self.event_publish,
                    "repost": self.event_repost,
                    "correction": self.event_correction,
                    "cancellation": self.event_cancellation,
                    "delist": self.event_delist,
                }[kind]
                msg = handler(rng)
                if msg:
                    logs.append(msg)
            self.save()
            return logs

    # ---------- 初始种子 ----------
    def seed_initial(self, notice_count: int = 24, seed: int = 42) -> list[str]:
        with process_lock():
            rng = random.Random(seed)
            logs: list[str] = []
            for i in range(notice_count):
                self.sim_now += timedelta(hours=rng.randint(1, 10))
                notice = self._make_tender(rng, rng.choice(SITE_CODES))
                logs.append(f"publish: {notice['uid']}")
                # 约一半公告会被 1~2 个站转载
                if rng.random() < 0.55:
                    for target in rng.sample(
                        [c for c in SITE_CODES if c not in notice["replicas"]],
                        k=rng.randint(1, 2),
                    ):
                        self.sim_now += timedelta(hours=rng.randint(1, 4))
                        rep = self._attach_replica(notice, target)
                        logs.append(f"repost: {notice['uid']} -> {target} ({rep['remote_id']})")
            # 预置几条更正/废标
            for kind in ("correction", "correction", "cancellation"):
                self.sim_now += timedelta(hours=rng.randint(2, 6))
                if kind == "correction":
                    logs.append(self.event_correction(rng) or "")
                else:
                    logs.append(self.event_cancellation(rng) or "")
            self.save()
            return [x for x in logs if x]

    # ---------- 对账视图 ----------
    def truth_view(self) -> dict:
        """给对账器用的全量真值视图。"""
        replicas: dict[str, dict] = {}
        canonical: dict[str, dict] = {}
        for notice in self.notices.values():
            canonical[notice["canonical_key"]] = {
                "project_code": notice["project_code"],
                "title": notice["title"],
                "notice_type": notice["notice_type"],
                "parent_key": (
                    self.notices[notice["parent_uid"]]["canonical_key"]
                    if notice.get("parent_uid") else None
                ),
                "purchaser": notice["purchaser"],
                "budget_amount": notice["budget_amount"],
                "deadline_at": notice["deadline_at"],
                "status": notice.get("status", "active"),
                "content_hash": notice["content_hash"],
            }
            for site_code, rep in notice["replicas"].items():
                if rep["status"] != "active":
                    continue
                replicas[f"{site_code}:{rep['remote_id']}"] = {
                    "site_code": site_code,
                    "remote_id": rep["remote_id"],
                    "canonical_key": notice["canonical_key"],
                    "notice_type": notice["notice_type"],
                    "origin_remote_id": rep.get("origin_remote_id"),
                    "published_at": rep["published_at"],
                    "title": notice["title"],
                    "purchaser": notice["purchaser"],
                    "budget_amount": notice["budget_amount"],
                    "deadline_at": notice["deadline_at"],
                    "project_code": notice["project_code"],
                }
        return {"sim_now": to_iso(self.sim_now), "canonical": canonical,
                "replicas": replicas}


# 便捷函数
def seed_store(path: str | Path | None = None, notice_count: int = 24,
               seed: int = 42) -> TruthStore:
    p = Path(path) if path else truth_path()
    if p.exists():
        p.unlink()
    store = TruthStore(p)
    store.seed_initial(notice_count=notice_count, seed=seed)
    return store


def format_amount_display(amount_yuan: float) -> str:
    """mock 详情页展示金额的口径：过亿用亿元，过万用万元，其余用元。"""
    if amount_yuan >= 100_000_000:
        return f"{amount_yuan / 100_000_000:.2f}亿元"
    if amount_yuan >= 10_000:
        return f"{amount_yuan / 10_000:.2f}万元"
    return f"{amount_yuan:.2f}元"


def format_deadline_display(deadline_iso: str) -> str:
    dt = datetime.fromisoformat(deadline_iso)
    return dt.strftime("%Y年%m月%d日 %H:%M")
