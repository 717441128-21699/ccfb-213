"""Web 控制台：站点配置 / 任务调度 / 公告库 / 变更记录 / 告警。"""
from __future__ import annotations

import os

from flask import Flask

from ..bootstrap import bootstrap_sites
from ..db import init_db


def create_web_app() -> Flask:
    init_db()
    bootstrap_sites()
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    # 演示用固定密钥；生产环境应从环境变量注入
    app.secret_key = os.environ.get("TENDER_INTEL_SECRET", "tender-intel-dev-secret")

    from .routes_dashboard import bp as dash_bp
    from .routes_sites import bp as sites_bp
    from .routes_tasks import bp as tasks_bp
    from .routes_notices import bp as notices_bp
    from .routes_changes import bp as changes_bp
    from .routes_alerts import bp as alerts_bp

    app.register_blueprint(dash_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(notices_bp)
    app.register_blueprint(changes_bp)
    app.register_blueprint(alerts_bp)

    from .helpers import register_helpers
    register_helpers(app)
    return app
