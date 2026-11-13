"""进程内读写锁 + 跨进程文件锁，保护 truth.json 的并发读写。

mock 站群四站点同进程多线程：用 RLock 串行化；
tick 等外部进程改文件时：用文件锁避免读到半写状态。
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_rlock = threading.RLock()


@contextmanager
def process_lock():
    _rlock.acquire()
    try:
        yield
    finally:
        _rlock.release()


class FileLock:
    """简易阻塞文件锁（锁文件 + O_EXCL 原子创建），跨平台。"""

    def __init__(self, target_path: str | Path, timeout: float = 10.0):
        self.lock_path = Path(str(target_path) + ".lock")
        self.timeout = timeout
        self._acquired = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                if time.time() > deadline:
                    # 兜底：陈旧锁（持有者崩溃）直接接管
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                time.sleep(0.05)

    def release(self) -> None:
        if self._acquired:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
