# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

(以下为中文指引;完整设计文档见 `docs/design.md`。)

## 项目概述

OCS(网课助手)的本地 AI 题库服务:LLM 答题 + SQLite 题目哈希缓存 + Web 管理面板。技术栈 Python 3.13 · FastAPI · aiosqlite · litellm · Alpine.js(面板零构建)。

## 常用命令

```bash
# 环境(macOS)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # 开发依赖(含 pytest);仅运行装 requirements.txt

# 启动服务(默认 127.0.0.1:8000;HOST/PORT/DATA_DIR 环境变量可覆盖)
python run.py
TIKU_FAKE_LLM=1 python run.py             # FAKE 模式:EchoLLM 回显,不加载 litellm,无需配 provider

# 测试(pytest-asyncio auto 模式;默认排除 live 标记)
pytest                                     # 全量
pytest tests/test_parse.py                 # 单文件
pytest tests/test_parse.py -k fence        # 按关键字选用例(测试名为英文 snake_case)
pytest --cov=app                           # 覆盖率
pytest -m live                             # 真实网络冒烟(默认排除)
```

依赖固定精确版本:改动依赖后安装,再 `pip freeze` 回填 requirements*.txt。

## 架构

### 查询主路径(核心数据流)

`GET /api/query` → normalize(选项切分/规范化/MD5 哈希)→ SQLite 查缓存:

- **命中**:touch(last_hit_at/hits)→ 内容→字母映射 → `code=1`(~1ms、零 token)
- **未命中**:in-flight 同题去重(共享 Task)→ litellm.Router 优先级 failover → 解析校验 → 写缓存 → `code=1`

### 关键不变量(改动前必读)

1. **缓存存答案内容,不存字母**:`questions.answer` 存选项原文(多选 `#` 拼接)/判断题 `正确|错误`/填空文本,响应时按**当前请求的选项顺序**映射字母——直接缓存字母会在选项乱序重放时答错。命中与 LLM 新答共用同一条 `content_to_response` 映射路径(app/answer/parse.py)。
2. **解析失败绝不入缓存**:LLM 失败/解析失败只计统计与 call_log,返回 `code=0`,防止错误答案固化。
3. **先答后写**:缓存写失败仅记日志,不影响已生成的答案返回。
4. **in-flight 任务用 `asyncio.shield`**:等待方被取消(OCS 60s 超时放弃)不影响任务完成并写缓存——下次同题秒回。
5. **哈希键规则**(app/normalize.py):选项规范化后**排序**参与哈希(乱序重放仍命中);judgement/completion **不把选项纳入键**(判断题选项标签不稳定,纳入会大量 miss)。

### 分层与依赖方向

`api/`(路由 + deps 依赖注入)→ `answer/`(AnswerService 主流程 + prompt + parse 纯函数)→ `llm/`(仅 RouterManager 触碰 litellm)→ `repository/`(纯 SQL)→ `db.py`(aiosqlite 封装)。

- `LLMProtocol`(app/llm/types.py)是测试接缝:AnswerService 只依赖「有 `ask()` 方法的对象」,测试注入 FakeLLM,无需联网或加载 litellm。
- litellm 延迟导入:RouterManager 只在非 FAKE 模式创建(main.py `_build_router_manager`)。
- providers/settings 写操作成功后触发 `rebuild_router` 热重建 litellm Router。

### 配置体系(三层)

1. **环境变量**(app/config.py,全部惰性读取、返回新值,便于测试 monkeypatch):`HOST`/`PORT`/`DATA_DIR`/`TIKU_FAKE_LLM`。
2. **app_config KV 表**(repository/settings.py,读时与代码内 DEFAULTS 合并,新增键免迁移):`ttl_days`、`llm_timeout`、`api_token` 等;cleanup 循环每轮重读,改设置热生效。
3. **providers 表**:base_url/api_key/model/priority/rpm/max_parallel → 映射为 litellm deployment(`order`=priority,越小越优先)。

### 数据库

- 单个 `tiku.db`:aiosqlite 单连接(内部串行队列)+ WAL + busy_timeout;时间戳全部 epoch 秒(TTL 为纯整数差,时区无关),stats_daily 按本地时区分桶。
- 迁移:db.py 的 `MIGRATIONS` dict 按 `PRAGMA user_version` 递增执行,**新增版本只追加条目**。
- TTL 清理(cleanup.py 后台任务):分批 DELETE 用 IN 子查询写法(不依赖 `DELETE ... LIMIT`),批间 sleep 让出避免锁库;不做自动 VACUUM。

### 测试体系

- conftest.py fixtures:`data_dir`(每测试独立 DATA_DIR)、`db`、`client`(TIKU_FAKE_LLM=1 + EchoLLM)、`plain_client`(真实 RouterManager 路径,无 provider 时 ask 报未配置)。
- httpx `ASGITransport` 直连 app,不起端口。
- 重点覆盖:normalize/parse 纯函数、in-flight 去重(同题并发 LLM 只调一次)、查询闭环(未命中→入库→再查命中)、失败不缓存、TTL 分批清理。

## 约定

- 代码注释、docstring、commit message 均用中文(commit 遵循 `<type>: <描述>` 格式)。
- 数据面端点(/api/query、/api/health、/api/import)始终返回 HTTP 200,业务失败用 body 内 `code=0` 表达(OCS handler 约定);管理端点挂可选 `api_token` 鉴权(X-Token 头或 ?token=,默认关闭)。
- Web 面板(app/static/)零构建:Alpine.js + 手写 CSS,不引入构建工具。
- Docker 部署文件入库,但本开发机不做构建验证。
