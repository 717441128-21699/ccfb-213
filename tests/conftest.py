"""pytest 公共夹具：临时 HOME + 临时端口的 mock 站群 + 已初始化的爬虫库。"""
from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

import pytest

from tender_intel.bootstrap import bootstrap_sites
from tender_intel.db import connect, init_db
from tender_intel.mock.runner import MockCluster
from tender_intel.mock.sites import MOCK_SITES
from tender_intel.mock.truth import seed_store


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    home = tmp_path_factory.mktemp("tender_home")
    os.environ["TENDER_INTEL_HOME"] = str(home)
    truth_file = home / "truth.json"
    db_file = home / "tender.db"

    ports = {s.code: _free_port() for s in MOCK_SITES}
    seed_store(truth_file, notice_count=20, seed=42)
    cluster = MockCluster(truth_file=truth_file, ports=ports)
    cluster.start(seed_if_empty=False)
    base_urls = {code: f"http://127.0.0.1:{port}" for code, port in ports.items()}

    init_db(db_file)
    bootstrap_sites(db_file, base_urls=base_urls)

    yield {
        "home": home, "truth_file": truth_file, "db_file": db_file,
        "cluster": cluster, "ports": ports, "base_urls": base_urls,
    }

    cluster.stop()


@pytest.fixture()
def db_conn(env):
    conn = connect(env["db_file"])
    yield conn
    conn.close()


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# 让子进程（如有）继承临时 HOME
if "TENDER_INTEL_HOME" not in os.environ:
    pass
