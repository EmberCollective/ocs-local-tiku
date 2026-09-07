# ocs-local-tiku 设计文档

> 版本：v1.0（2026-09-07）
> 状态：设计定稿，实现进行中（M1 → M4）

## 1. 背景与目标

[OCSJS](https://github.com/ocsjs/ocsjs)（网课助手脚本）的自动答题功能依赖在线题库，而在线题库经常失效、收费或需要审核。本项目自建**本地题库服务**：

**LLM 答题 + SQLite 题目哈希缓存 + Web 管理面板**

核心价值：

- 不依赖任何在线题库的可用性——只要有任一 LLM API（OpenAI 兼容格式）即可答题
- 重复题目毫秒级返回（缓存命中，零 LLM 成本），缓存随使用自然沉淀
- 多 API 配置 + 自动 failover，单点故障不影响使用
- 个人单用户场景，本地部署，数据完全自持

前期调研结论（为什么值得做）：

| 参考项目 | 架构 | 差距 |
|---|---|---|
| [Miaozeqiu/ai-ocs-question_bank](https://github.com/Miaozeqiu/ai-ocs-question_bank) | LLM + SQLite | 与本想法最接近，但单文件原型、简陋 |
| [LynnGuo666/ocsjs-ai-answer-service](https://github.com/LynnGuo666/ocsjs-ai-answer-service) | LLM + 内存缓存 | 缓存重启即丢，无持久化 |
| [DokiDoki1103/tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter) | 哈希收录 + 多题库聚合 | 缓存/收录架构最成熟，但依赖第三方题库而非 LLM |

「成熟收录架构 + LLM 作为答案源」这个组合没有现成实现，本项目填补该空白。

## 2. 需求与选型

| 项 | 决定 |
|---|---|
| 技术栈 | Python 3.13 + FastAPI + aiosqlite + litellm（Router） |
| 运行方式 | 本地直跑（默认 `127.0.0.1:8000`，`HOST`/`PORT`/`DATA_DIR` 环境变量可覆盖）；提供 Dockerfile/compose（有 Docker 的环境可用） |
| LLM 调度 | litellm.Router：`order` 优先级 failover、`allowed_fails` + `cooldown_time` 限流自动冷却；后期分流只改 `routing_strategy` |
| 缓存 | 题目规范化 → MD5 hex 主键 → SQLite 命中即回 + 更新 `last_hit_at` |
| 缓存淘汰 | 默认 60 天（可配置）无命中自动删除，分批次清理（500 条/批） |
| Web 面板 | 服务内置单页面（Alpine.js + 手写 CSS，零构建） |
| 批量导入 | 预留 `POST /api/import`（JSON，≤5000 条/批，同一哈希去重），UI 二期 |
| 安全 | 绑定回环地址；可选 api_token（默认关闭） |

## 3. 哈希选型调研（实测数据，Python 3.13.8）

**速度不是区分因素：**

| 层面 | 实测 |
|---|---|
| 哈希计算（126B 题目文本 ×10 万次） | blake2b 0.38μs / blake2s 0.43μs / sha256 0.55μs / sha1 0.56μs / md5 0.61μs —— 全亚微秒，10 万次总计 < 60ms |
| SQLite 10 万行表主键点查 ×10 万次 | BLOB(16B) 0.91μs / TEXT hex(32) 1.50μs / INTEGER(64bit) 1.61μs / TEXT 原文(126B) 1.66μs —— 极差 0.75μs |

单次搜题的耗时主导者是网络往返（毫秒级）与 LLM（秒级），哈希层任何选择占比 < 0.01%。注意 SHA-256 在现代 CPU（SHA-NI 指令）上快于 MD5，「MD5 更快」在本场景不成立、也不重要。

**碰撞安全性：** 个人题库规模上限估 10 万条，MD5 全长 128bit 生日碰撞概率 ≈ 1.5×10⁻²⁹，可忽略；MD5 的「已破解」指构造性前缀碰撞，需要恶意对抗者——个人单用户题库无此威胁模型。

**决定性因素是生态兼容：** tikuAdapter（题库生态最成熟的项目）使用 MD5 hex（题目 + 排序选项 + 题型）。保持相同格式意味着未来可直接互导题库；hex 字符串也便于面板展示与调试。

**结论：MD5 → 32 字符 hex TEXT 作唯一键**，同时保留规范化原文做写入时兜底比对。真正决定去重质量的是**规范化函数**（见 §4），而非哈希算法。

不采用：xxhash/blake3（第三方依赖，短文本无优势）、BLOB（互操作差）、64bit 截断（无收益且降低碰撞边界）。

## 4. OCS 协议对接（源码核实结论）

以下结论来自阅读 `ocsjs/ocsjs` 仓库 `packages/core/src/core/answer-wrapper/` 源码：

- **请求**：OCS 将题库配置 `data` 中的占位符替换后发送——GET 拼到 URL query（自动 URL 编码），POST 发 JSON body。`options` 由平台脚本以 `options.map(o => o.innerText).join('\n')` 构造，**主流为换行符 `\n` 拼接**，不同平台可能用 `##`、`|` 等。
- **响应解析**：OCS 执行用户配置的 handler 字符串，约定返回 `[题目, 答案]` 或 `undefined`（未搜到）。
- **超时**：OCS 侧 60s 竞速超时 → 服务端全链路预算 ≤ 55s（默认 `llm_timeout=20s`、`num_retries=1`）。
- **答案消费**：OCS 拿到答案后自行按 `===`/`#`/`---`/`###`/`|`/`;`/`；` 拆多选，再做「去选项前缀 → 规范化 → string-similarity ≥ 0.6 匹配 / 纯字母 A/B/C/D 回退」。**返回 `A#B#C` 纯字母或选项文本两者均兼容**。
- **跨域**：`type: "GM_xmlhttpRequest"` 模式不受 CORS 限制；`type: "fetch"` 模式受限 → 服务端全局 `CORSMiddleware(allow_origins=["*"])` 兜底。

### 用户 OCS 端配置（随 README 提供）

```json
{
  "url": "http://127.0.0.1:8000/api/query",
  "name": "本地题库",
  "method": "get",
  "data": { "title": "${title}", "type": "${type}", "options": "${options}" },
  "handler": "return (res) => res.code === 1 ? [res.question, res.answer] : undefined"
}
```

### options 规范化（normalize.py，缓存命中率的根基）

1. **切分**：按分隔符优先级（`###` > `##` > `\n` > `|` > `;` > `；` > `\t`），取切出非空片段数最多的结果；全空视为无选项。
2. **清洗**：每个选项剥离字母前缀（正则覆盖 `A.` `A、` `(A)` `A）` `A:` 等），去首尾空白。
3. **规范化**（哈希与匹配共用）：全角转半角（NFKC）→ 删除空白与零宽字符 → casefold → 去尾部标点。

**哈希键**：`md5(norm(title) + "|" + type + "|" + "\x1f".join(sorted(norm(opt) for opt in options)))`

- 选项排序参与哈希 → 「同题选项乱序重放」命中同一缓存
- `judgement`/`completion` **不把选项纳入键**——判断题选项标签（对/错/√/×）不稳定，纳入会导致大量 miss

**关键设计——缓存存「答案内容」而非字母**：`questions.answer` 列存内容（多选选项原文按 `#` 拼接 / 判断题 `正确`|`错误` / 填空文本），响应时按**当前请求的选项顺序**映射字母。直接缓存字母会在选项乱序重放时返回错误字母。此设计让「缓存命中」与「LLM 新答」共用同一条 内容→字母 映射路径。

## 5. 架构与目录结构

```
ocs-local-tiku/
├── run.py                      # uvicorn 入口（HOST/PORT/DATA_DIR 环境变量，默认 127.0.0.1:8000）
├── Dockerfile                  # python:3.13-slim，非 root，uvicorn
├── docker-compose.yml          # 127.0.0.1:8000:8000、named volume、TZ、healthcheck
├── .dockerignore / .gitignore
├── requirements.txt            # 固定精确版本（安装后 pip freeze 回填）
├── pyproject.toml              # pytest 配置（asyncio_mode=auto）
├── README.md
├── docs/design.md              # 本文档
├── app/
│   ├── main.py                 # create_app + lifespan（建库/建 Router/起清理任务）+ CORS + 静态挂载
│   ├── config.py               # DATA_DIR（env，默认 ./data）、DB_PATH、HOST/PORT
│   ├── db.py                   # aiosqlite 单连接、PRAGMA（WAL/busy_timeout）、user_version 版本化迁移
│   ├── models.py               # Pydantic 模型
│   ├── normalize.py            # 切分/清洗/规范化/哈希（纯函数，重点测试对象）
│   ├── cleanup.py              # TTL 批次清理后台任务
│   ├── answer/
│   │   ├── service.py          # AnswerService：查询主流程 + in-flight 同题去重
│   │   ├── prompt.py           # system/user prompt 构造
│   │   └── parse.py            # LLM 输出解析 + 内容→字母映射 + 判断题映射（纯函数）
│   ├── llm/
│   │   ├── router_manager.py   # litellm.Router 构建/热重建/acompletion/单 provider 测试
│   │   ├── types.py            # AskResult dataclass（测试替身注入点）
│   │   └── usage_tracker.py    # litellm CustomLogger：按 api_base 归属 provider 写统计
│   ├── repository/
│   │   ├── questions.py        # questions 读写/touch/分页搜索/批量删/导入 upsert
│   │   ├── providers.py        # providers CRUD
│   │   ├── settings.py         # app_config KV（与代码内 DEFAULTS 合并）
│   │   └── stats.py            # stats_daily / provider_stats_daily / call_log
│   ├── api/
│   │   ├── deps.py             # AppState 依赖注入、可选 token 校验
│   │   ├── query.py            # GET/POST /api/query（OCS 对接）
│   │   ├── import_api.py       # POST /api/import（预留）
│   │   ├── health.py           # GET /api/health（免鉴权）
│   │   ├── admin_providers.py  # providers CRUD + reorder + test + status
│   │   ├── admin_cache.py      # /api/cache 分页/搜索/删除 + /api/stats
│   │   └── admin_settings.py   # GET/PUT /api/settings、GET /api/log
│   └── static/                 # index.html + app.js + style.css（Alpine.js 面板）
├── data/                       # 运行时生成（.gitignore 忽略）
└── tests/
    ├── conftest.py             # tmp 数据库 fixture、FakeLLM、httpx ASGI client
    ├── test_normalize.py
    ├── test_parse.py
    ├── test_query_flow.py
    ├── test_cleanup.py
    ├── test_providers_api.py
    └── test_import.py
```

数据流（查询主路径）：

```
OCS ──GET /api/query──▶ api/query.py ──▶ AnswerService
    normalize（切分/规范化/哈希）
    ├─ 缓存命中 ──▶ touch(last_hit_at, hits) ──▶ 内容→字母映射 ──▶ 返回 code=1
    └─ 未命中 ──▶ in-flight 去重（同题共享 Task）
              ──▶ RouterManager.ask（litellm.Router，优先级 failover）
              ──▶ parse（解析/映射/校验）
              ├─ 成功 ──▶ 写入 questions ──▶ 返回 code=1
              └─ 失败 ──▶ call_log(llm-fail / parse-fail) ──▶ 返回 code=0（不缓存）
```

## 6. 数据模型（SQLite，时间戳全部 Unix epoch 整型秒）

epoch 秒使 TTL 成为纯整数差值比较，**容器/宿主机时区变化零影响**；`stats_daily.day` 由应用层按本地时区分桶；面板显示由前端 `new Date(ts*1000).toLocaleString()` 转换。

```sql
CREATE TABLE questions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  cache_key    TEXT NOT NULL UNIQUE,      -- md5 hex（唯一约束 = 天然去重）
  question     TEXT NOT NULL,             -- 首见原始题目
  qtype        TEXT NOT NULL CHECK(qtype IN ('single','multiple','judgement','completion')),
  options_json TEXT NOT NULL DEFAULT '[]',-- 首见切分后的选项原文数组
  answer       TEXT NOT NULL,             -- 答案内容（多选 # 拼接；判断题 正确/错误）
  source       TEXT NOT NULL DEFAULT 'llm' CHECK(source IN ('llm','import')),
  created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  last_hit_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  hits         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_questions_last_hit ON questions(last_hit_at);
CREATE INDEX idx_questions_created  ON questions(created_at);

CREATE TABLE providers (
  id           TEXT PRIMARY KEY,          -- uuid4().hex
  name         TEXT NOT NULL,
  base_url     TEXT NOT NULL,             -- OpenAI 兼容，含 /v1
  api_key      TEXT NOT NULL DEFAULT '',
  model        TEXT NOT NULL,             -- 裸模型名，如 gpt-4o-mini / deepseek-chat
  priority     INTEGER NOT NULL DEFAULT 1,-- 1 最优先，映射 litellm order
  rpm          INTEGER,                   -- 可空；映射 litellm rpm
  max_parallel INTEGER,                   -- 可空；映射 max_parallel_requests
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  updated_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE app_config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                     -- JSON 序列化
);

CREATE TABLE stats_daily (
  day               TEXT PRIMARY KEY,     -- YYYY-MM-DD（本地时区分桶）
  cache_hits        INTEGER NOT NULL DEFAULT 0,
  cache_misses      INTEGER NOT NULL DEFAULT 0,
  llm_calls         INTEGER NOT NULL DEFAULT 0,
  llm_failures      INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE provider_stats_daily (
  day               TEXT NOT NULL,
  provider_id       TEXT NOT NULL,
  calls             INTEGER NOT NULL DEFAULT 0,
  failures          INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider_id)
);

CREATE TABLE call_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  kind              TEXT NOT NULL,        -- hit | miss | llm-fail | parse-fail | import
  cache_key         TEXT,
  question          TEXT,
  provider_id       TEXT,
  model             TEXT,
  latency_ms        INTEGER,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  error             TEXT
);
CREATE INDEX idx_call_log_ts ON call_log(ts);
```

**写入策略**：aiosqlite 单连接（内部串行队列）+ `journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL`。WAL 保证容器/进程重启安全（崩溃自动恢复，读写不互斥）。统计表用 `INSERT ... ON CONFLICT DO UPDATE SET x = x + ?` 原子自增。面板搜索用 `question LIKE '%kw%'`（个人量级足够；FTS5 留作未来增强）。

**配置持久化选 SQLite 而非 JSON 文件**：原子事务 vs 文件写坏风险；单一事实源（备份 = 复制 `tiku.db` 一个文件）；providers 的 id/排序/启停语义由表结构直接支撑。`app_config` KV 读时与代码内 `DEFAULTS` 合并，新增键自动有默认值，免迁移。

## 7. API 设计

`/api/query` 与 `/api/health` 始终返回 HTTP 200（OCS handler 约定业务失败用 code=0 表达）；管理 API 开启 api_token 时校验 `X-Token` 头或 `?token=`（默认关闭，本地零配置）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/query` | 参数 `title`（必填）、`type`（single/multiple/judgement/completion，默认 single）、`options`（字符串，可空）→ `{"code":1,"question":"...","answer":"A#B#C"}` 或 `{"code":0,"msg":"原因"}` |
| POST | `/api/import` | `{"items":[{question,type,options,answer}]}`，≤5000 条/批 → `{"code":1,"imported":N,"updated":M,"skipped":K}` |
| GET | `/api/health` | 免鉴权，`{"status":"ok"}`（healthcheck 用） |
| GET/POST/PUT/DELETE | `/api/providers`（及 `/{id}`） | CRUD；api_key 打码返回（`sk-***abc`），编辑留空 = 不修改 |
| POST | `/api/providers/reorder` | `{ids:[...]}` 按序重排 priority（列表顺序 = 优先级） |
| POST | `/api/providers/{id}/test` | 直连测试（不走 Router，10s 超时）→ `{ok, latency_ms, reply, error?}` |
| GET | `/api/providers/status` | 近 24h 各 provider 调用/失败/token、成功率、平均延迟、冷却标记（best-effort） |
| GET | `/api/cache?page=&page_size=&q=&type=` | 分页浏览 + 搜索 |
| DELETE | `/api/cache/{id}`、POST `/api/cache/delete-batch` | 单条/批量删除（≤1000/批） |
| GET | `/api/cache/export` | 导出全部缓存 JSON（备份） |
| GET | `/api/stats` | 总条数/命中率/LLM 调用与 token（今日 + 累计 + 近 14 天序列） |
| GET/PUT | `/api/settings` | 读写设置；涉及 Router 的键保存后热重建 |
| GET | `/api/log?limit=&kind=` | 近期调用日志 |

providers/settings 写操作均触发 `RouterManager.rebuild()`。

## 8. LLM 调度层（litellm.Router 集成）

```python
from litellm import Router

MODEL_GROUP = "answering"

model_list = [
    {
        "model_name": MODEL_GROUP,
        "litellm_params": {
            "model": f"openai/{p.model}",      # OpenAI 兼容端点统一 openai/ 前缀
            "api_base": p.base_url,
            "api_key": p.api_key,
            "order": p.priority,               # 越小越优先 = failover 顺序（无需 fallbacks 列表）
            **({"rpm": p.rpm} if p.rpm else {}),
            **({"max_parallel_requests": p.max_parallel} if p.max_parallel else {}),
        },
    }
    for p in sorted(enabled_providers, key=lambda x: x.priority)
]

router = Router(
    model_list=model_list,
    routing_strategy=settings.routing_strategy,  # 默认 simple-shuffle；分流改 usage-based-routing-v2 / least-busy
    num_retries=1,
    allowed_fails=3,          # 分钟内失败超限 → 冷却该 deployment
    cooldown_time=60,
    default_litellm_params={"temperature": 0, "timeout": 20},
)
resp = await router.acompletion(model=MODEL_GROUP, messages=messages)
```

要点（对照 [litellm 官方 Router 文档](https://docs.litellm.ai/docs/routing)确认）：

- **优先级 failover = `order`**：order 小者优先；order=1 失败（连接错误/404/429）自动尝试 order=2，每级有自己的重试。无需再配 fallbacks 列表。
- **限流冷却 = `allowed_fails` + `cooldown_time`**：429 或失败超限 → 该 deployment 移出可用池，到期自动恢复；粒度为单个 deployment。
- **并发控制 = `rpm` + `max_parallel_requests`**（per-deployment）。OCS 几十题并发时：in-flight 去重先合并同题，剩余由各 provider 并发上限排队。
- **分流扩展点**：只改 `routing_strategy` 字符串（`usage-based-routing-v2` 按 RPM/TPM 余量分流、`least-busy` 最少在途）。面板设置页做成下拉框，保存即热重建，无需改架构。
- **统计**：`acompletion` 响应带 `usage.prompt_tokens/completion_tokens`；per-provider 归属用 litellm `CustomLogger`（实现 `async_log_success_event`/`async_log_failure_event`，从 `kwargs["litellm_params"]["api_base"]` 反查 provider_id）。
- **单 provider 测试**：不经 Router，直连 `litellm.acompletion(..., timeout=10)` 发 ping。
- `LITELLM_TELEMETRY=False` 关遥测；只装 SDK 不装 proxy；requirements.txt 固定精确版本。
- **超时预算**：OCS 60s 硬超时；默认 `llm_timeout=20s × num_retries=1`，最坏链路 ≈ `Σ 级 timeout×(retries+1)`（全部 provider 皆挂的病态场景会超，此时 OCS 丢弃可接受——服务端仍完成并写缓存，下次同题秒回）。

**测试替身**：`AnswerService` 依赖「有 `ask(messages) -> AskResult` 方法的对象」最小协议；测试注入 `FakeLLM`，完全不需要联网或实例化 litellm。litellm 只在 `RouterManager` 内部出现。

## 9. 缓存生命周期

### 写入与命中

- 命中：`UPDATE questions SET last_hit_at=?, hits=hits+1 WHERE cache_key=?`（轻量 touch）
- 写入：LLM 答案解析成功后 `INSERT`（`cache_key` 唯一约束天然去重；写入遇哈希已存在则比对规范化原文，不一致记录日志——碰撞兜底）

### TTL 批次清理（cleanup.py，lifespan 启动，关闭时 cancel）

```python
async def cleanup_loop(db, settings):
    await asyncio.sleep(60)                     # 启动后 1 分钟首轮
    while True:
        ttl_days = await settings.get("ttl_days")           # 每轮重读，热生效（默认 60）
        batch    = await settings.get("cleanup_batch_size") # 默认 500
        cutoff   = int(time.time()) - ttl_days * 86400      # epoch 差值，时区无关
        while True:
            n = await db.execute("""
                DELETE FROM questions WHERE cache_key IN (
                    SELECT cache_key FROM questions
                    WHERE last_hit_at < ? ORDER BY last_hit_at LIMIT ?
                )""", (cutoff, batch))
            if n < batch:
                break
            await asyncio.sleep(2)               # 批间让出，避免持续锁库
        await db.execute("DELETE FROM call_log WHERE ts < ?", (int(time.time()) - 7 * 86400,))
        interval_h = await settings.get("cleanup_interval_hours")   # 默认 6
        await asyncio.sleep(interval_h * 3600)
```

- 子查询走 `idx_questions_last_hit` 索引，单批 DELETE 毫秒级；WAL 下读写不互斥
- `DELETE ... LIMIT` 直写不被 Python 自带 SQLite 构建保证支持，故用 IN 子查询写法
- DB 文件不随删除收缩属正常，不做自动 VACUUM（避免长事务），README 说明可手动 VACUUM

## 10. Web 面板

- **服务方式**：`app.mount("/static", StaticFiles(...))`，`GET /` 返回 `index.html`。同源无 CORS 问题；全局 CORS 兜底兼容 OCS `type:"fetch"` 模式。
- **技术**：Alpine.js 3（CDN，可下载本地引用以完全离线）+ 手写 `style.css`。不用 Tailwind CDN（play 版控制台告警且体积大）；四个 tab 手写百行 CSS 足够。
- **四个 tab**：
  1. **仪表盘**：卡片（总缓存/今日命中率/今日 LLM 调用/今日 token）+ 近 14 天 CSS 柱状趋势 + 最近调用日志
  2. **Provider 管理**：表格（优先级↑↓、启停、测试、删除）+ 新增/编辑弹层（api_key 密码框，留空不改）+ 路由策略下拉（failover ↔ 分流，保存热重建）
  3. **缓存管理**：搜索 + 题型筛选 + 分页表格（题目/题型/答案/来源/命中数/最后命中）+ 单删/勾选批删/导出
  4. **设置**：TTL、批大小、间隔、llm_timeout、num_retries、allowed_fails、cooldown_time、api_token
- 全部 `fetch('/api/...')` + Alpine 状态渲染；不做 WebSocket（仪表盘手动/定时轮询即可）
- 面板内嵌「OCS 配置指引」说明块，附可复制 JSON 片段

## 11. 部署

### 本地直跑（当前环境主方式）

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
python run.py                                    # 默认 127.0.0.1:8000，data/ ./data
```

`config.py`：`DATA_DIR = env("DATA_DIR", "./data")`、`HOST = env("HOST", "127.0.0.1")`、`PORT = env("PORT", 8000)`。

### Docker（文件入库；在无 Docker 的开发机上不做构建验证）

```dockerfile
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LITELLM_TELEMETRY=False TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.py .
RUN useradd --create-home --uid 1000 appuser && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

compose 要点：

- `ports: "127.0.0.1:8000:8000"` —— 限宿主机回环（单用户安全默认；需要局域网访问改为 `"8000:8000"`）
- **named volume** `tiku-data:/data`（Windows 下比 bind mount 权限语义稳；首次挂载自动复制镜像内 /data 属主）
- `TZ=Asia/Shanghai`（仅影响 stats_daily 按天分桶；epoch 存储使 TTL 不受影响）
- healthcheck 用 Python 标准库 urllib 探活（slim 镜像无 curl）：`python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"`
- `restart: unless-stopped`；`.dockerignore` 排除 data/tests/__pycache__/.git
- 备份：`docker run --rm -v tiku-data:/data -v %cd%:/backup alpine cp /data/tiku.db /backup/`

## 12. 错误处理

| 场景 | 处理 |
|---|---|
| OCS 60s 超时放弃 | 服务端任务自然完成、**结果仍写缓存**（下次同题秒回——特性而非浪费） |
| title 缺失/为空 | `{"code":0,"msg":"缺少题目"}`，HTTP 200 |
| type 非法 | 回退 single，call_log 记 warning |
| LLM 单点失败/429 | Router 内部：429 即时冷却该 deployment，order 降级到下一 provider |
| LLM 全部失败 | `{"code":0,"msg":"LLM 全部不可用：<摘要>"}`；计 llm_failures + call_log；**不缓存** |
| LLM 输出解析失败（空/拒答/格式不符） | `{"code":0,"msg":"答案解析失败"}`；call_log(parse-fail)；**绝不缓存**（防错误答案固化） |
| 多选部分选项匹配不上 | 已匹配映射字母、未匹配保留原文 `#` 拼接返回（OCS 文本相似度兜底），code=1 且缓存内容 |
| SQLITE_BUSY | WAL + busy_timeout=5000 + aiosqlite 单连接串行；OperationalError 重试 1 次（100ms 退避）；缓存写失败不影响答案返回（先答后写） |
| in-flight 任务异常 | 共享该 Task 的并发请求收到同一错误（同题同命）；done_callback 确保 dict 清理不泄漏 |
| 未预期异常 | `/api/query` 最外层 try/except → code=0，服务不崩，logging 记堆栈 |

## 13. 测试策略

框架：pytest + pytest-asyncio + httpx（`ASGITransport` 直连 app，不起端口）。所有测试设 `DATA_DIR=tmp_path` 隔离。`FakeLLM`（预设 reply/delay/error，计数 calls）注入 `AnswerService`；litellm 不做单测（另留 `@pytest.mark.live` 冒烟，默认 skip）。

重点用例：

1. **normalize/parse 纯函数全覆盖**（质量核心）：`\n`/`##` 切分、字母前缀剥离、全角半角、大小写；选项乱序同哈希；判断/填空键不含选项；相似题不同哈希。选项文本→字母映射、纯字母透传、多选分隔归一、判断题标准词、markdown 围栏与「答案：」前缀剥离、空/拒答 → ParseError、未匹配片段保留原文
2. **in-flight 去重**：同题 `asyncio.gather` 10 并发，FakeLLM.calls == 1
3. **查询闭环**：未命中→LLM→入库→再查命中（LLM 仍只调 1 次）；全失败 code=0；解析失败不入库
4. **TTL 清理**：501 过期 + 5 新鲜，batch=500 → 分两批、只删过期；call_log 裁剪
5. **providers/settings API**：CRUD、reorder 触发 rebuild（spy）、api_key 打码、`/api/health` 200
6. **导入**：同键 upsert 计数、坏行 skipped、超 5000 拒绝

## 14. 实现里程碑

| 里程碑 | 内容 | 验证 |
|---|---|---|
| **M1** | 骨架 + 缓存层 + FakeLLM 查询闭环（无 litellm） | pytest 全绿；curl 同题两次第二次秒回且 FakeLLM 只调一次；配进 OCS 实测协议联通 |
| **M2** | litellm 集成 + provider 管理 API + 统计 | curl 配两个真实 provider，主力 key 改错 → 自动落次级；`/api/providers/status` 有 token 统计 |
| **M3** | Web 面板 | 浏览器完成 provider 增删改/测连/排序/启停、缓存管理、设置热切换 |
| **M4** | TTL 清理 + 批量导入 + README 收尾 | 改旧行触发清理；导入 JSON 后可命中；Docker 构建验证延后至有 Docker 的环境 |

每里程碑独立提交推送。

## 15. 参考资料

- [ocsjs/ocsjs](https://github.com/ocsjs/ocsjs) 与 [官方文档（AnswererWrapper）](https://docs.ocsjs.com/docs/other/api)
- [litellm Router 官方文档](https://docs.litellm.ai/docs/routing)（order/cooldown/rpm/max_parallel_requests/CustomLogger）
- [DokiDoki1103/tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter)（哈希收录架构参考）
- [Miaozeqiu/ai-ocs-question_bank](https://github.com/Miaozeqiu/ai-ocs-question_bank)、[LynnGuo666/ocsjs-ai-answer-service](https://github.com/LynnGuo666/ocsjs-ai-answer-service)（同类原型参考）
