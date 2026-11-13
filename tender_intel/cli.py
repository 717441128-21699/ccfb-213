"""统一命令行入口。

常用命令：
  python -m tender_intel.cli mock-start [--auto-tick 60] [--reset]
  python -m tender_intel.cli init-db
  python -m tender_intel.cli crawl --mode full|incremental [--sites js,zj] [--no-browser]
  python -m tender_intel.cli resume <run_id> [--no-browser]
  python -m tender_intel.cli reconcile
  python -m tender_intel.cli tick [--events 3] [--seed 7]
  python -m tender_intel.cli web --port 8080
  python -m tender_intel.cli scheduler
  python -m tender_intel.cli crawl-run --run-id N --mode full   # 子进程内部入口
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading

from .bootstrap import bootstrap_sites
from .config import ensure_dirs
from .db import connect, init_db
from .reconcile import reconcile


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_init_db(args) -> None:
    ensure_dirs()
    init_db()
    bootstrap_sites()
    print("数据库已初始化，4 个 mock 站点配置已写入 sites 表")


def cmd_mock_start(args) -> None:
    from .mock.runner import MockCluster
    cluster = MockCluster(auto_tick_sec=args.auto_tick,
                          tick_events=args.tick_events)
    if args.reset:
        from .config import truth_path
        if truth_path().exists():
            truth_path().unlink()
    cluster.start()
    from .mock.sites import MOCK_SITES
    for s in MOCK_SITES:
        print(f"mock site {s.code} -> {cluster.base_url(s.code)} ({s.name})")
    print("按 Ctrl+C 停止")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        cluster.stop()


def cmd_tick(args) -> None:
    from .mock.runner import MockCluster
    cluster = MockCluster()
    result = cluster.trigger_tick(events=args.events, seed=args.seed)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_expire_tokens(args) -> None:
    from .mock.runner import MockCluster
    MockCluster().expire_tokens(args.site)
    print(f"站点 {args.site} 全部 Token 已作废")


def cmd_switch_template(args) -> None:
    from .mock.runner import MockCluster
    MockCluster().set_template(args.site, args.version)
    print(f"站点 {args.site} 模板已切换为 v{args.version}")


def cmd_crawl(args) -> None:
    from .crawler.engine import run_crawl
    sites = args.sites.split(",") if args.sites else None
    run_id = run_crawl(args.mode, site_codes=sites, no_browser=args.no_browser)
    _print_run_result(run_id)


def cmd_crawl_run(args) -> None:
    """子进程入口：复用已创建的 run_id，并启动心跳线程。"""
    from .crawler.engine import CrawlEngine
    from .scheduler import heartbeat_loop
    stop_hb = threading.Event()
    hb = threading.Thread(target=heartbeat_loop,
                          args=(args.run_id, stop_hb), daemon=True)
    hb.start()
    try:
        engine = CrawlEngine(no_browser=args.no_browser)
        run_id = engine.run(args.mode,
                            site_codes=args.sites.split(",") if args.sites else None,
                            resume_run_id=args.run_id)
        _print_run_result(run_id)
    finally:
        stop_hb.set()


def cmd_resume(args) -> None:
    from .crawler.engine import run_crawl
    run_id = run_crawl(args.mode if args.mode else "incremental",
                       resume_run_id=args.run_id, no_browser=args.no_browser)
    _print_run_result(run_id)


def _print_run_result(run_id: int) -> None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
        print(f"run_id={run_id} status={row['status']} mode={row['mode']}")
        stats = json.loads(row["stats_json"] or "{}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if row["error"]:
            print(f"error: {row['error']}")
    finally:
        conn.close()


def cmd_reconcile(args) -> None:
    report = reconcile()
    data = report.as_dict()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if not report.ok:
        sys.exit(1)


def cmd_web(args) -> None:
    from .web import create_web_app
    init_db()
    bootstrap_sites()
    app = create_web_app()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


def cmd_scheduler(args) -> None:
    from .scheduler import Scheduler
    Scheduler.ensure_default_schedule()
    sched = Scheduler(poll_sec=args.poll, once=args.once)
    sched.run_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tender_intel")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-db")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("mock-start")
    p.add_argument("--auto-tick", type=int, default=0)
    p.add_argument("--tick-events", type=int, default=2)
    p.add_argument("--reset", action="store_true")
    p.set_defaults(func=cmd_mock_start)

    p = sub.add_parser("tick")
    p.add_argument("--events", type=int, default=3)
    p.add_argument("--seed", type=int, default=None)
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("expire-tokens")
    p.add_argument("site")
    p.set_defaults(func=cmd_expire_tokens)

    p = sub.add_parser("switch-template")
    p.add_argument("site")
    p.add_argument("version", type=int, choices=(1, 2))
    p.set_defaults(func=cmd_switch_template)

    p = sub.add_parser("crawl")
    p.add_argument("--mode", choices=("full", "incremental"), required=True)
    p.add_argument("--sites", default=None, help="逗号分隔站点 code，缺省全部")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("crawl-run")
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--mode", choices=("full", "incremental"), required=True)
    p.add_argument("--sites", default=None)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_crawl_run)

    p = sub.add_parser("resume")
    p.add_argument("run_id", type=int)
    p.add_argument("--mode", choices=("full", "incremental"), default=None)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("reconcile")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("web")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("scheduler")
    p.add_argument("--poll", type=int, default=10)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_scheduler)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    args.func(args)


if __name__ == "__main__":
    main()
