# 招投标情报系统

面向十几个省级招投标公告网站的采集与情报平台。本仓库自带一套 **mock 站群**（4 个省）
模拟真实站点的分页、JS 渲染、请求签名、登录过期与限流，可在无外网环境下端到端运行，
并以「爬虫库与 mock 真值完全对账」作为验收标准。

## 功能与需求对照

| 需求 | 实现 |
| --- | --- |
| mock 站群（分页/JS 渲染/签名/登录过期/限流） | `tender_intel/mock/`：Flask 四端口站群 + 真值库 + tick 演变 |
| 全量/增量切换，中断断点续跑 | `crawler/engine.py`：模式切换、按页落 `crawl_checkpoints`、resume |
| 跨站转载去重 | `site_notices (site_id, remote_id)` 锚点 + `canonical_notices.fingerprint` 主记录 |
| 更正/废标挂原公告 | 子公告 `parent_id` 挂接，含跨站顺序补偿（`link_pending_children`） |
| 金额/截止时间/采购单位结构化，失败单独存 | `crawler/parser.py` + `parse_failures` 表 |
| 改版字段大面积为空告警 | 空值比例 ≥50%（至少 3 个详情页）触发 `parse_breakdown` 告警 |
| 抓取时间 + 原始快照 | 每条副本保存列表 JSON 与详情 HTML 两份 `snapshots` |
| 增量跑完与 mock 真值完全对上 | `reconcile.py` 逐副本逐字段对账，`tests/` 全量验收 |
| Web 控制台五个页面 | 站点配置 / 任务调度 / 公告库 / 变更记录 / 告警中心（+总览） |

## 架构

```
tender_intel/
├── mock/          # 真值库 truth.json + 四省 Flask 站点 + 启动器
│   ├── truth.py   # 模拟时钟、seq 计数器、发布/转载/更正/废标/下架事件
│   ├── server.py  # 签名校验、Token/过期、令牌桶限流、v1/v2 JS 页面
│   └── runner.py  # 同进程多线程监听 8301-8304，可选自动 tick
├── crawler/
│   ├── client.py  # HMAC 签名客户端：401 自动重登、429 Retry-After 退避
│   ├── browser.py # Playwright UI 登录 + JS 详情渲染 + HTML 快照（含降级模式）
│   ├── parser.py  # 可配置 CSS 选择器 + 金额/日期结构化
│   ├── mini_dom.py# 标准库实现的轻量 CSS 选择器（零第三方 HTML 依赖）
│   ├── repo.py    # 落库、跨站去重、父子挂接、快照、变更、水位/断点
│   └── engine.py  # 全量/增量引擎、断点续跑、改版告警
├── scheduler/     # DB 驱动调度器，抓虫子进程 + 心跳/僵死回收
├── web/           # Flask 控制台（服务端渲染，无前端构建步骤）
├── db.py          # SQLite schema
├── signing.py     # HMAC-SHA256 请求签名（站点与爬虫共用）
├── reconcile.py   # 真值对账
└── cli.py         # 统一命令行
```

### 关键设计纪律（踩坑沉淀）

1. **增量去重只认持久化 remote_id**。`watermark`（max published_at）只用于提前停止翻页，
   绝不作为跳过依据，避免同秒发布漏数据。
2. **序列号持久化续算**。真值库的 notice/replica seq 落盘，进程重启从文件续算，
   否则 remote_id 撞码互相覆盖。
3. **跨站时间单调**。转载副本的 `published_at` 取目标站当时的模拟时钟；
   更正/废标同步到父公告的全部副本站点，各站 `origin_remote_id` 指向本站父副本。
4. **结构化字段以 API 权威值为准、解析值兜底**，保证去重指纹与真值同口径。

## 快速开始

```powershell
# 1. 安装依赖 + 浏览器内核（只需一次）
py -3.14 -m pip install -r requirements.txt
py -3.14 -m playwright install chromium

# 2. 初始化数据库与站点配置
py -3.14 -m tender_intel.cli init-db

# 3. 启动 mock 站群（--reset 重建真值，--auto-tick 让数据随时间自动演变）
py -3.14 -m tender_intel.cli mock-start --reset --auto-tick 60

# 4. 另开终端：全量抓取 → 对账
py -3.14 -m tender_intel.cli crawl --mode full
py -3.14 -m tender_intel.cli reconcile      # ok=true 即与真值完全一致

# 5. 手动制造变化后增量抓取，再对账
py -3.14 -m tender_intel.cli tick --events 5 --seed 7
py -3.14 -m tender_intel.cli crawl --mode incremental
py -3.14 -m tender_intel.cli reconcile

# 6. Web 控制台 http://127.0.0.1:8080
py -3.14 -m tender_intel.cli web --port 8080

# 7. 调度器（按 schedules 表自动派单，另开终端常驻）
py -3.14 -m tender_intel.cli scheduler
```

无浏览器环境（CI/无内核机器）所有抓取命令加 `--no-browser`，
快照由 API JSON 按当前模板版本合成，行为与真实渲染保持一致。

### 模拟站点改版

```powershell
py -3.14 -m tender_intel.cli switch-template gd 2   # 广东站 class 全换
py -3.14 -m tender_intel.cli crawl --mode full --sites gd
# 告警中心出现 parse_breakdown；控制台「站点配置」一键升级 v2 选择器后恢复
```

## 测试

```powershell
py -3.14 -m pytest tests/ -q
```

- `test_units.py`：签名、金额/日期解析、轻量 DOM、真值库指纹与单调性
- `test_e2e.py`：全量对账、跨站去重、tick 后增量对账、幂等、断点续跑、改版告警、401/429
- `test_browser_render.py`：真实 Playwright 渲染、UI 登录过期重登、降级模式

## 数据落位

所有可变状态在 `TENDER_INTEL_HOME`（默认 `./data`）：

- `truth.json`：mock 站群唯一真值（对账基准）
- `tender.db`：爬虫业务库（SQLite，WAL 模式）
- `_runtime/`、`logs/`：运行期文件
