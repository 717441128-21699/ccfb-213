"""带签名的 HTTP 客户端：自动登录、401 重登、429 退避重试。"""
from __future__ import annotations

import logging
import uuid

import requests

from ..signing import sign_headers

logger = logging.getLogger(__name__)


class CrawlError(RuntimeError):
    """可归类的爬虫错误。type 供告警页分组。"""

    def __init__(self, type_: str, message: str, status: int | None = None):
        super().__init__(message)
        self.type = type_
        self.status = status


class SignedClient:
    def __init__(self, base_url: str, secret: str, username: str, password: str,
                 max_rate_retries: int = 6, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.username = username
        self.password = password
        self.max_rate_retries = max_rate_retries
        self.timeout = timeout
        self.session = requests.Session()
        self._token: str | None = None
        # 计数器，供调度层统计限流/重登次数
        self.rate_hits = 0
        self.relogins = 0

    # ---------- 底层 ----------
    def _headers(self, method: str, path: str) -> dict[str, str]:
        headers = sign_headers(self.secret, method, path, uuid.uuid4().hex)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def login(self) -> None:
        path = "/api/login"
        resp = self.session.post(
            self.base_url + path,
            headers=sign_headers(self.secret, "POST", path, uuid.uuid4().hex),
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if resp.status_code == 403:
            raise CrawlError("auth_failed", f"登录签名被拒: {resp.text}", 403)
        if resp.status_code != 200:
            raise CrawlError("auth_failed",
                             f"登录失败 HTTP {resp.status_code}: {resp.text}",
                             resp.status_code)
        self._token = resp.json()["token"]
        self.relogins += 1
        logger.info("%s 登录成功（第 %d 次）", self.base_url, self.relogins)

    def request(self, method: str, path: str, params: dict | None = None,
                _retry_auth: bool = True, _rate_retries: int = 0) -> requests.Response:
        url = self.base_url + path
        try:
            resp = self.session.request(
                method, url, headers=self._headers(method, path),
                params=params, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CrawlError("network", f"网络错误 {method} {path}: {exc}") from exc

        if resp.status_code == 401:
            # Token 过期/被作废：重新登录后重试一次
            if not _retry_auth:
                raise CrawlError("auth_failed",
                                 f"重登后仍 401: {resp.text}", 401)
            logger.info("%s 登录过期，重新登录后重试", self.base_url)
            self.login()
            return self.request(method, path, params,
                                _retry_auth=False, _rate_retries=_rate_retries)

        if resp.status_code == 429:
            self.rate_hits += 1
            if _rate_retries >= self.max_rate_retries:
                raise CrawlError("rate_limited",
                                 f"限流重试超过上限 ({self.max_rate_retries})", 429)
            retry_after = int(resp.headers.get("Retry-After", "3"))
            # 单测里限流窗口很小，封顶等待避免拖死
            import time
            wait = min(retry_after, 8)
            logger.warning("%s 触发限流，等待 %ss 后重试（第 %d 次）",
                           self.base_url, wait, _rate_retries + 1)
            time.sleep(wait)
            return self.request(method, path, params,
                                _retry_auth=_retry_auth,
                                _rate_retries=_rate_retries + 1)

        if resp.status_code == 403:
            raise CrawlError("auth_failed", f"签名校验失败: {resp.text}", 403)
        if resp.status_code >= 500:
            raise CrawlError("server_error",
                             f"站点错误 HTTP {resp.status_code}", resp.status_code)
        return resp

    def get_json(self, path: str, params: dict | None = None) -> dict:
        resp = self.request("GET", path, params=params)
        if resp.status_code == 404:
            raise CrawlError("not_found", f"{path} 不存在", 404)
        if resp.status_code != 200:
            raise CrawlError("http_error",
                             f"HTTP {resp.status_code}: {resp.text[:200]}",
                             resp.status_code)
        return resp.json()

    # ---------- 业务接口 ----------
    def list_notices(self, page: int, size: int = 10) -> dict:
        return self.get_json("/api/notices", {"page": page, "size": size})

    def notice_detail(self, remote_id: str) -> dict:
        return self.get_json(f"/api/notice/{remote_id}")

    def close(self) -> None:
        self.session.close()
