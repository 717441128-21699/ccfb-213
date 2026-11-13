"""mock 站群启动器：同一进程内多线程监听四个端口，共享 truth.json。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import requests
from werkzeug.serving import make_server

from ..config import ADMIN_KEY, ensure_dirs, runtime_dir, truth_path
from .server import create_app
from .sites import MOCK_SITES
from .truth import seed_store


class _ServerThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        self.server = make_server("127.0.0.1", port, app, threaded=True)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


class MockCluster:
    def __init__(self, truth_file: str | Path | None = None,
                 ports: dict[str, int] | None = None,
                 auto_tick_sec: int = 0, tick_events: int = 2):
        ensure_dirs()
        self.truth_file = Path(truth_file) if truth_file else truth_path()
        self.ports = ports or {s.code: s.port for s in MOCK_SITES}
        self.auto_tick_sec = auto_tick_sec
        self.tick_events = tick_events
        self._threads: list[_ServerThread] = []
        self._stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._lock_file = runtime_dir() / "cluster.lock"

    def base_url(self, code: str) -> str:
        return f"http://127.0.0.1:{self.ports[code]}"

    def start(self, seed_if_empty: bool = True) -> None:
        if seed_if_empty and not self.truth_file.exists():
            seed_store(self.truth_file)
        for site in MOCK_SITES:
            app = create_app(site, self.truth_file)
            thread = _ServerThread(app, self.ports[site.code])
            thread.start()
            self._threads.append(thread)
        self._wait_ready()
        if self.auto_tick_sec > 0:
            self._tick_thread = threading.Thread(target=self._auto_tick_loop, daemon=True)
            self._tick_thread.start()

    def _wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                if all(
                    requests.get(f"{self.base_url(s.code)}/admin/health", timeout=2).ok
                    for s in MOCK_SITES
                ):
                    return
            except requests.RequestException as exc:
                last_err = exc
            time.sleep(0.25)
        raise RuntimeError(f"mock cluster not ready: {last_err}")

    def _auto_tick_loop(self) -> None:
        while not self._stop.wait(self.auto_tick_sec):
            try:
                self.trigger_tick(events=self.tick_events)
            except Exception as exc:  # 演示环境里 tick 失败不应打挂站点
                print(f"[auto-tick] {exc}")

    def trigger_tick(self, events: int = 2, seed: int | None = None) -> dict:
        """对任意一个站点调 admin/tick 即可（真值共享）。"""
        url = f"{self.base_url(MOCK_SITES[0].code)}/admin/tick"
        body = {"events": events, "allow_delist": True}
        if seed is not None:
            body["seed"] = seed
        resp = requests.post(url, json=body,
                             headers={"X-Admin-Key": ADMIN_KEY}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def set_template(self, site_code: str, version: int) -> None:
        url = f"{self.base_url(site_code)}/admin/template"
        resp = requests.post(url, json={"version": version},
                             headers={"X-Admin-Key": ADMIN_KEY}, timeout=10)
        resp.raise_for_status()

    def expire_tokens(self, site_code: str) -> None:
        url = f"{self.base_url(site_code)}/admin/expire-tokens"
        resp = requests.post(url, json={},
                             headers={"X-Admin-Key": ADMIN_KEY}, timeout=10)
        resp.raise_for_status()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.shutdown()
        for t in self._threads:
            t.join(timeout=5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="启动四省 mock 站群")
    parser.add_argument("--auto-tick", type=int, default=0,
                        help="自动 tick 间隔秒数，0 表示关闭")
    parser.add_argument("--tick-events", type=int, default=2)
    parser.add_argument("--reset", action="store_true", help="重置真值库")
    args = parser.parse_args()

    ensure_dirs()
    if args.reset and truth_path().exists():
        truth_path().unlink()
    cluster = MockCluster(auto_tick_sec=args.auto_tick, tick_events=args.tick_events)
    cluster.start()
    for s in MOCK_SITES:
        print(f"mock site {s.code} -> {cluster.base_url(s.code)} ({s.name})")
    print("按 Ctrl+C 停止")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cluster.stop()


if __name__ == "__main__":
    main()
