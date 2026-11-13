"""单个 mock 站点的 Flask 应用。

每个站点一个 create_app()，runner 在同一进程里用线程监听不同端口。
所有请求都从 truth.json 重新加载真值，保证多端口之间数据始终一致。

模拟的真实站点行为：
- /api/* 全部要求 HMAC 请求签名；
- 除登录外要求 Bearer Token，Token 有 TTL，admin 可一键作废旧 Token；
- 每分钟请求数超阈值返回 429 + Retry-After；
- 列表页与详情页都是 JS 渲染（数据走 XHR），详情页有 v1/v2 两套 class；
- 详情页 XHR 遇到 401 时渲染 #auth-expired 节点，模拟登录过期。
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import ADMIN_KEY, DEMO_PASSWORD, SIGN_WINDOW_SEC
from ..signing import verify_sign
from .locking import process_lock
from .sites import MockSite
from .truth import TruthStore, format_amount_display, format_deadline_display

TOKEN_TTL_SEC = 30 * 60


# ---------------- 前端 JS（签名 + 渲染） ----------------
def _browser_js(secret: str) -> str:
    return """
async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function hmacSha256Hex(secret, text) {
  const key = await crypto.subtle.importKey('raw',
    new TextEncoder().encode(secret), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, '0')).join('');
}
const SITE_SECRET = %r;
async function signedFetch(method, path, body) {
  const ts = String(Math.floor(Date.now() / 1000));
  const nonce = crypto.randomUUID();
  const sign = await hmacSha256Hex(SITE_SECRET, method.toUpperCase() + '\\n' + path + '\\n' + ts + '\\n' + nonce);
  const headers = {'X-Timestamp': ts, 'X-Nonce': nonce, 'X-Sign': sign};
  const token = localStorage.getItem('token');
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (body) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : undefined});
  return res;
}
function showAuthExpired() {
  const el = document.getElementById('auth-expired');
  if (el) el.style.display = 'block';
}
async function apiOrExpire(path) {
  const res = await signedFetch('GET', path);
  if (res.status === 401) { showAuthExpired(); return null; }
  if (!res.ok) { document.getElementById('app').textContent = '请求失败 ' + res.status; return null; }
  return res.json();
}
""" % secret


def _login_html(secret: str) -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>登录</title></head><body>
<div id="auth-expired" style="display:none;color:#c00">登录已过期，请重新登录</div>
<form id="login-form" onsubmit="return doLogin(event)">
  <h3>站点登录</h3>
  <input id="username" name="username" value="demo" />
  <input id="password" name="password" type="password" value="" />
  <button type="submit">登录</button>
  <div id="msg"></div>
</form>
<script>__BROWSER_JS__
async function doLogin(e) {
  e.preventDefault();
  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  const res = await signedFetch('POST', '/api/login', {username, password});
  const data = await res.json();
  if (!res.ok) { document.getElementById('msg').textContent = data.error || '登录失败'; return false; }
  localStorage.setItem('token', data.token);
  localStorage.setItem('expires_at', data.expires_at);
  location.href = '/';
  return false;
}
</script></body></html>""".replace("__BROWSER_JS__", _browser_js(secret))


def _list_html(secret: str) -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>公告列表</title></head><body>
<div id="auth-expired" style="display:none;color:#c00">登录已过期，请重新登录</div>
<a href="/login">登录</a>
<div id="app">加载中...</div>
<script>__BROWSER_JS__
(async function () {
  const data = await apiOrExpire('/api/notices?page=1&size=20');
  if (!data) return;
  const tpl = data.template_version;
  const app = document.getElementById('app');
  app.innerHTML = '';
  for (const item of data.items) {
    const card = document.createElement(tpl === 2 ? 'article' : 'div');
    card.className = tpl === 2 ? 'bid-card' : 'notice-card';
    card.dataset.remoteId = item.remote_id;
    const a = document.createElement('a');
    a.href = '/notice/' + item.remote_id;
    a.textContent = item.title;
    card.appendChild(a);
    app.appendChild(card);
  }
})();
</script></body></html>""".replace("__BROWSER_JS__", _browser_js(secret))


def _detail_html(secret: str) -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>公告详情</title></head><body>
<div id="auth-expired" style="display:none;color:#c00">登录已过期，请重新登录</div>
<a href="/login">登录</a>
<div id="app">加载中...</div>
<script>__BROWSER_JS__
(async function () {
  const remoteId = location.pathname.split('/').pop();
  const data = await apiOrExpire('/api/notice/' + remoteId);
  if (!data) return;
  const tpl = data.template_version;
  const app = document.getElementById('app');
  if (tpl === 2) {
    app.innerHTML =
      '<section class="bid-detail">' +
      '<h2 class="bid-title">' + data.title + '</h2>' +
      '<span class="prj-no">项目编号：' + data.project_code + '</span><br>' +
      '<span class="buyer-name">采购单位：' + data.purchaser + '</span><br>' +
      '<span class="money-amount">预算金额：' + data.budget_display + '</span><br>' +
      '<time class="close-time">截止时间：' + data.deadline_display + '</time>' +
      '</section>';
  } else {
    app.innerHTML =
      '<div class="notice-detail">' +
      '<h1 class="detail-title">' + data.title + '</h1>' +
      '<div class="project-code">项目编号：' + data.project_code + '</div>' +
      '<div class="purchaser">采购单位：' + data.purchaser + '</div>' +
      '<div class="budget">预算金额：' + data.budget_display + '</div>' +
      '<div class="deadline">截止时间：' + data.deadline_display + '</div>' +
      '</div>';
  }
})();
</script></body></html>""".replace("__BROWSER_JS__", _browser_js(secret))


class _RuntimeState:
    """单进程内的 Token、限流状态（四站点各自独立）。"""

    def __init__(self, rate_limit_per_min: int):
        self.lock = threading.Lock()
        self.tokens: dict[str, dict] = {}   # token -> {exp, epoch}
        self.buckets: dict[str, deque] = {}  # identity -> 时间戳队列
        self.rate_limit = rate_limit_per_min

    def issue_token(self, epoch: int) -> dict:
        token = secrets.token_hex(16)
        exp = time.time() + TOKEN_TTL_SEC
        with self.lock:
            self.tokens[token] = {"exp": exp, "epoch": epoch}
        return {"token": token, "expires_at": int(exp), "ttl": TOKEN_TTL_SEC}

    def check_token(self, token: str | None, epoch: int) -> tuple[bool, str]:
        if not token:
            return False, "missing token"
        with self.lock:
            meta = self.tokens.get(token)
            if not meta:
                return False, "invalid token"
            if meta["epoch"] != epoch:
                return False, "token revoked"
            if meta["exp"] < time.time():
                del self.tokens[token]
                return False, "token expired"
        return True, "ok"

    def check_rate(self, identity: str) -> tuple[bool, int]:
        """返回 (是否放行, 建议等待秒数)。"""
        now = time.time()
        with self.lock:
            q = self.buckets.setdefault(identity, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.rate_limit:
                retry = int(60 - (now - q[0])) + 1
                return False, max(retry, 1)
            q.append(now)
            return True, 0


def create_app(site: MockSite, truth_file: str | Path) -> Flask:
    app = Flask(f"mock_{site.code}")
    state = _RuntimeState(site.rate_limit_per_min)

    def load_truth() -> TruthStore:
        return TruthStore.load(truth_file)

    def require_sign() -> tuple[bool, str]:
        path = request.path
        ok, reason = verify_sign(
            site.secret, request.method, path,
            request.headers.get("X-Timestamp", ""),
            request.headers.get("X-Nonce", ""),
            request.headers.get("X-Sign", ""),
            window=SIGN_WINDOW_SEC,
        )
        return ok, reason

    def bearer_token() -> str | None:
        auth = request.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else None

    # ---------------- HTML 页面 ----------------
    @app.get("/login")
    def login_page():
        return Response(_login_html(site.secret), content_type="text/html")

    @app.get("/")
    def list_page():
        return Response(_list_html(site.secret), content_type="text/html")

    @app.get("/notice/<remote_id>")
    def detail_page(remote_id: str):
        return Response(_detail_html(site.secret), content_type="text/html")

    # ---------------- API ----------------
    @app.post("/api/login")
    def api_login():
        ok, reason = require_sign()
        if not ok:
            return jsonify(error=reason), 403
        body = request.get_json(silent=True) or {}
        if body.get("username") != "demo" or body.get("password") != DEMO_PASSWORD:
            return jsonify(error="bad credentials"), 401
        epoch = load_truth().site_admin[site.code]["token_epoch"]
        return jsonify(state.issue_token(epoch))

    @app.get("/api/notices")
    def api_notices():
        ok, reason = require_sign()
        if not ok:
            return jsonify(error=reason), 403
        store = load_truth()
        token_ok, token_reason = state.check_token(
            bearer_token(), store.site_admin[site.code]["token_epoch"])
        if not token_ok:
            return jsonify(error=token_reason), 401
        identity = bearer_token() or request.remote_addr or "anon"
        allowed, retry = state.check_rate(identity)
        if not allowed:
            resp = jsonify(error="rate limit exceeded")
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry)
            return resp
        page = max(int(request.args.get("page", 1)), 1)
        size = min(max(int(request.args.get("size", 10)), 1), 100)
        data = store.list_page(site.code, page, size)
        data["template_version"] = store.site_admin[site.code]["template_version"]
        return jsonify(data)

    @app.get("/api/notice/<remote_id>")
    def api_notice_detail(remote_id: str):
        ok, reason = require_sign()
        if not ok:
            return jsonify(error=reason), 403
        store = load_truth()
        token_ok, token_reason = state.check_token(
            bearer_token(), store.site_admin[site.code]["token_epoch"])
        if not token_ok:
            return jsonify(error=token_reason), 401
        identity = bearer_token() or request.remote_addr or "anon"
        allowed, retry = state.check_rate(identity)
        if not allowed:
            resp = jsonify(error="rate limit exceeded")
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry)
            return resp
        found = store.get_replica(site.code, remote_id)
        if not found or found["replica"]["status"] != "active":
            return jsonify(error="not found"), 404
        notice, _rep = found["notice"], found["replica"]
        return jsonify({
            "remote_id": remote_id,
            "title": notice["title"],
            "project_code": notice["project_code"],
            "notice_type": notice["notice_type"],
            "purchaser": notice["purchaser"],
            "budget_display": format_amount_display(notice["budget_amount"]),
            "deadline_display": format_deadline_display(notice["deadline_at"]),
            "budget_amount": notice["budget_amount"],
            "deadline_at": notice["deadline_at"],
            "template_version": store.site_admin[site.code]["template_version"],
        })

    # ---------------- Admin（仅本机演示用） ----------------
    @app.get("/admin/health")
    def admin_health():
        store = load_truth()
        return jsonify(
            site=site.code, ok=True,
            sim_now=to_iso_safe(store.sim_now),
            notices=len(store.notices),
            template_version=store.site_admin[site.code]["template_version"],
            active_replicas=len(store.active_replicas(site.code)),
        )

    def require_admin() -> bool:
        return request.headers.get("X-Admin-Key") == ADMIN_KEY

    @app.post("/admin/tick")
    def admin_tick():
        if not require_admin():
            return jsonify(error="forbidden"), 403
        body = request.get_json(silent=True) or {}
        store = load_truth()
        import random as _random
        rng = _random.Random(body["seed"]) if "seed" in body else _random.Random()
        logs = store.tick(events=int(body.get("events", 3)),
                          rng=rng, allow_delist=bool(body.get("allow_delist", True)))
        return jsonify(ok=True, sim_now=to_iso_safe(store.sim_now), logs=logs)

    @app.post("/admin/template")
    def admin_template():
        if not require_admin():
            return jsonify(error="forbidden"), 403
        version = int((request.get_json(silent=True) or {}).get("version", 1))
        with process_lock():
            store = load_truth()
            store.site_admin[site.code]["template_version"] = version
            store.save()
        return jsonify(ok=True, template_version=version)

    @app.post("/admin/expire-tokens")
    def admin_expire():
        if not require_admin():
            return jsonify(error="forbidden"), 403
        with process_lock():
            store = load_truth()
            store.site_admin[site.code]["token_epoch"] += 1
            store.save()
        with state.lock:
            state.tokens.clear()
        return jsonify(ok=True)

    return app


def to_iso_safe(dt) -> str:
        from ..clock import to_iso
        return to_iso(dt)
