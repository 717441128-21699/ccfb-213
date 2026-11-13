"""全局路径与运行参数。所有可变状态都落在 TENDER_INTEL_HOME（默认 ./data）下。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "tender_intel"

# mock 管理接口固定密钥（演示用）
ADMIN_KEY = os.environ.get("MOCK_ADMIN_KEY", "mock-admin-key")
# mock 站统一演示密码
DEMO_PASSWORD = "demo123"
# 签名时间戳容忍窗口（秒）
SIGN_WINDOW_SEC = 300
# 解析失败率告警阈值：一次运行中核心字段为空的详情页占比
PARSE_BREAKDOWN_RATIO = 0.5
PARSE_BREAKDOWN_MIN_PAGES = 3


def home_dir() -> Path:
    p = os.environ.get("TENDER_INTEL_HOME")
    return Path(p) if p else Path.cwd() / "data"


def db_path() -> Path:
    return home_dir() / "tender.db"


def truth_path() -> Path:
    return home_dir() / "truth.json"


def runtime_dir() -> Path:
    return home_dir() / "_runtime"


def log_dir() -> Path:
    return home_dir() / "logs"


def ensure_dirs() -> None:
    for d in (home_dir(), runtime_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
