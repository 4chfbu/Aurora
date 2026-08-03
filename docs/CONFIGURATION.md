# Aurora 配置文档

本文档对应当前仓库中的 Aurora v2 MVP（Python FastAPI 后端、Kali Worker 和 React/Vite 前端）。配置实现位于 `aurora/config.py`。

## 1. 配置加载规则

1. 启动 API 时，Aurora 从当前工作目录的 `.env` 读取配置。
2. 已存在的进程环境变量优先于 `.env`，因为 `.env` 使用 `setdefault` 写入环境。
3. 配置在第一次调用 `get_settings()` 时缓存。修改 `.env` 后必须重启 API 进程。
4. `.env` 解析器支持空行、`#` 注释和 `KEY=VALUE`；值两端的单引号或双引号会被去掉，不支持变量展开。
5. 相对路径以启动进程的当前工作目录为基准，而不是以配置文件所在目录为基准。

建议从示例开始：

```bash
cp .env.example .env
```

不要把真实 API Key、Cookie 或密码提交到仓库。`.gitignore` 已忽略 `.env`、数据库、Artifact 和前端构建产物。

## 2. 最小配置

### 默认真实 Runtime

生产和本地开发默认都使用 Codex Harness。先配置 LLM，再启动私有 CC Switch 服务：

```dotenv
AURORA_DB_URL=sqlite:///./aurora.db
AURORA_ARTIFACT_DIR=./artifacts
AURORA_WORKER_RUNTIME=codex
AURORA_CONTAINER_NETWORK=aurora-runtime
AURORA_LLM_BASE_URL=https://api.openai.com/v1
AURORA_LLM_API_KEY=替换为真实密钥
AURORA_LLM_MODEL=gpt-4.1-mini
```

启动 Runtime 和后端：

```bash
uv sync --extra dev
./scripts/runtime-up.sh
uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
# 或使用 ./scripts/dev-api.sh
```

### OpenAI 兼容接口（仅调试）

`openai_direct` 通过 Chat Completions 调用配置的兼容接口，适合调试提示词和上下文，不是最终的 Codex Worker 边界：

```dotenv
AURORA_WORKER_RUNTIME=openai_direct
AURORA_LLM_BASE_URL=https://api.openai.com/v1
AURORA_LLM_API_KEY=替换为真实密钥
AURORA_LLM_MODEL=gpt-4.1-mini
```

### Codex Harness（目标运行方式）

Codex Harness 在 Kali Worker 容器内执行。容器引擎和包含 Codex CLI 的镜像必须已经可用；该运行时不会回退到宿主机：

```dotenv
AURORA_WORKER_RUNTIME=codex
AURORA_WORKER_IMAGE=aurora-kali-codex:latest
AURORA_CONTAINER_NETWORK=aurora-runtime
AURORA_CODEX_COMMAND_TEMPLATE=/workspace/runtime/codex-via-cc-switch.sh {prompt_filename} {output_schema_filename} {last_message_filename}
AURORA_CODEX_PROXY_BASE_URL=http://aurora-cc-switch:15723/v1
AURORA_CODEX_TIMEOUT_SECONDS=1800
AURORA_LLM_BASE_URL=https://api.openai.com/v1
AURORA_LLM_API_KEY=替换为真实密钥
AURORA_LLM_MODEL=gpt-4.1-mini
```

构建和检查默认 Worker 镜像：

```bash
./scripts/runtime-up.sh
./scripts/verify-worker-image.sh
./scripts/check-codex-provider.sh
```

`AURORA_CODEX_TIMEOUT_SECONDS=0` 表示不设置 Harness 自身的超时上限；若 Intent 的 `hard_timeout_seconds` 有值，实际执行仍会受该预算和 Worker lease 约束。

## 3. 后端环境变量

### 数据与 Worker

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_DB_URL` | `sqlite:///./aurora.db` | SQLModel/SQLAlchemy 数据库 URL。默认数据库位于当前工作目录。使用其他数据库时还需要对应驱动。 |
| `AURORA_ARTIFACT_DIR` | `./artifacts` | 原始工具输出、导入附件和 Codex transcript 的存储目录。API 启动时会自动创建。 |
| `AURORA_WORKER_RUNTIME` | `codex` | `codex`/`codex_harness`/`harness` 使用 Codex Harness；`openai_direct`/`openai`/`llm`/`real` 使用直接兼容接口。其他值直接报错。 |
| `AURORA_WORKER_IMAGE` | `aurora-kali-codex:latest` | 包含 Codex CLI 的 Kali Worker 镜像名。只有本地已存在的镜像才会被容器执行器使用。 |
| `AURORA_CONTAINER_NETWORK` | `aurora-runtime` | Worker 与 CC Switch 共用的私有 Docker bridge 网络。 |
| `AURORA_WORKER_CONTAINER_CPUS` | `2` | Worker 容器的 CPU 限额，传给 `docker/podman run --cpus`。 |
| `AURORA_WORKER_CONTAINER_MEMORY` | `4g` | Worker 容器的内存限额，传给 `docker/podman run --memory`。 |
| `AURORA_BUILD_PROXY` | 未设置 | 可选的镜像构建 HTTP/HTTPS 代理；不会传入运行中的 Worker。 |
| `AURORA_CODEX_WORKSPACE_DIR` | `./codex-workspaces` | 每个项目和 Worker 的提示词、输入附件、输出 schema 和 transcript 工作目录。 |
| `AURORA_CODEX_PROXY_BASE_URL` | `http://aurora-cc-switch:15723/v1` | Worker 内 Codex 使用的 CC Switch Responses 地址。 |
| `AURORA_WORKER_REAP_INTERVAL_SECONDS` | `5` | API 后台线程清理过期 Worker lease 的间隔秒数。 |

### Codex 命令模板

`AURORA_CODEX_COMMAND_TEMPLATE` 默认调用 `codex-via-cc-switch.sh`。模板由 Harness 渲染后在 Worker 工作目录中执行，可用变量如下：

| 变量 | 含义 |
| --- | --- |
| `{prompt_file}` / `{prompt_filename}` | `aurora-intent.md` |
| `{output_schema_file}` / `{output_schema_filename}` | `aurora-output-schema.json` |
| `{last_message_file}` / `{last_message_filename}` | `aurora-last-message.json` |
| `{prompt_path}` | 相对于 Aurora 工作区根目录的提示词路径 |
| `{host_prompt_file}` | 宿主机绝对路径 |
| `{container_prompt_file}` | 容器内 `/workspace/...` 路径 |
| `{llm_model}` / `{llm_model_shell}` | 当前角色模型名，原始值或 shell 转义值 |
| `{llm_base_url}` / `{llm_base_url_shell}` | 规范化后的 OpenAI 兼容地址，原始值或 shell 转义值 |

`AURORA_CODEX_PROXY_BASE_URL` 默认为 `http://aurora-cc-switch:15723/v1`。上游可以只支持 Chat Completions，CC Switch 会转换 Codex 的 Responses 请求。可用 `./scripts/check-codex-provider.sh` 做真实预检。

### LLM 与角色路由

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_LLM_API_KEY` | 未设置；回退到 `OPENAI_API_KEY` | `openai_direct` 和 Codex Harness 的上游凭据。 |
| `AURORA_LLM_BASE_URL` | `https://api.openai.com/v1`；回退到 `OPENAI_BASE_URL` | OpenAI 兼容服务地址。 |
| `AURORA_LLM_MODEL` | `gpt-4.1-mini`；回退到 `OPENAI_MODEL` | 默认模型。 |
| `AURORA_PLANNER_MODEL` | 未设置 | Planner 角色模型；为空时使用 `AURORA_LLM_MODEL`。 |
| `AURORA_SOLVER_MODEL` | 未设置 | Solver 角色模型；为空时使用 `AURORA_LLM_MODEL`。 |
| `AURORA_LLM_TIMEOUT_SECONDS` | `120` | `openai_direct` 请求和部分 LLM 辅助请求的 HTTP 超时。 |

### Intent 执行预算

这些变量为新 Intent 提供默认预算；Intent 自身的 `budget` 字段可以覆盖它们：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_DEFAULT_SOFT_TIMEOUT_SECONDS` | `300` | Intent 的软超时预算字段；当前不会单独终止底层命令。 |
| `AURORA_DEFAULT_HARD_TIMEOUT_SECONDS` | `1800` | Intent 的硬超时预算，也用于 Codex Harness 的有效超时和 lease 计算。 |
| `AURORA_DEFAULT_MAX_TOOL_CALLS` | `12` | 单次 Worker 最多执行的工具请求数。 |
| `AURORA_DEFAULT_MAX_REPEAT_FAILURES` | `2` | 同一工具失败达到该次数后跳过后续重复请求。 |

### Hands-free Cataloger

Hands-free URL 导入需要单独的 Cataloger Agent。它只负责页面分类，不执行 Solver 工具：

| 变量 | 默认值 | 回退顺序 |
| --- | --- | --- |
| `AURORA_CATALOGER_LLM_API_KEY` | 未设置 | `AURORA_LLM_API_KEY` -> `OPENAI_API_KEY` |
| `AURORA_CATALOGER_LLM_BASE_URL` | `https://api.openai.com/v1` | `AURORA_LLM_BASE_URL` -> `OPENAI_BASE_URL` |
| `AURORA_CATALOGER_LLM_MODEL` | `gpt-4.1-mini` | `AURORA_LLM_MODEL` -> `OPENAI_MODEL` |
| `AURORA_CATALOGER_LLM_TIMEOUT_SECONDS` | `120` | `AURORA_LLM_TIMEOUT_SECONDS` |

只有 API Key、Base URL 和模型都存在时，Cataloger 才算已配置。

### FOFA 能力

两个凭据必须同时设置，`fofa.search` 才会对 Worker 暴露：

| 变量 | 默认值 |
| --- | --- |
| `AURORA_FOFA_EMAIL` | 未设置 |
| `AURORA_FOFA_KEY` | 未设置 |
| `AURORA_FOFA_BASE_URL` | `https://api.fofa.info/v1/search/all` |
| `AURORA_FOFA_TIMEOUT_SECONDS` | `20` |

FOFA 查询仍受项目允许的 host/domain/ip 范围和策略引擎限制。

### 同容器 Subagents

Subagents 需要同时满足全局开关、创建项目时的 `subagents_enabled=true`、Codex runtime 和主 Worker 条件：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_SUBAGENTS_ENABLED` | `false` | 接受 `1`、`true`、`yes`（不区分大小写）为启用。 |
| `AURORA_SUBAGENTS_MAX_CONCURRENT` | `2` | 每个 Worker 同时运行的子 Agent 数。 |
| `AURORA_SUBAGENTS_MAX_PER_WORKER` | `4` | 每个 Worker 的子 Agent 总数上限。 |
| `AURORA_SUBAGENT_CODEX_COMMAND` | 未设置 | 子 Agent 使用的 Codex 命令模板；应包含 `{prompt_filename}`、`{schema_filename}`、`{last_message_filename}`。默认 wrapper 会自动注入。 |

## 4. 前端与启动配置

### Vite 开发服务器

```bash
cd apps/web
npm install
AURORA_DEV_API_URL=http://localhost:8000 npm run dev
```

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `AURORA_DEV_API_URL` | `http://localhost:8001` | Vite `/api` 代理的后端地址。当前后端脚本默认监听 `8000`，因此本地同时启动前后端时建议显式设为 `http://localhost:8000`，或让 API 监听 `8001`。 |
| `VITE_API_BASE` | 空字符串 | 构建时写入前端的 API 前缀。空值表示使用同源 `/api`；跨域部署时设置为完整 API origin，例如 `https://api.example.com`。 |

生产/单体部署可先构建前端，再由 FastAPI 托管：

```bash
./scripts/build-web.sh
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
```

只有 `apps/web/dist/index.html` 和 `apps/web/dist/assets` 都存在时，FastAPI 才会挂载前端静态文件。

## 5. 未通过环境变量暴露的设置

`Settings.debug` 当前是代码内固定默认值，不会从 `.env` 读取：

| 设置 | 当前值 | 作用 |
| --- | --- | --- |
| `capture_full_context` | `true` | 预留字段；当前 ContextBuilder 始终生成所需上下文，不单独使用此开关。 |
| `redact_secrets` | `true` | 写入上下文快照前，按 `api_key`、token、password、secret 等模式脱敏。 |
| `max_context_snapshot_bytes` | `512000` | 上下文快照超过该大小时裁剪 facts 和 Artifact 摘要。 |
| `capture_tool_stdout` | `false` | 预留字段；当前未提供环境变量开关。 |

如需改变这些值，需要修改 `aurora/config.py` 或增加显式配置映射。

## 6. 安全与运维注意事项

- API 当前启用全开放 CORS（`*`），没有内置认证配置。不要直接把开发服务器暴露到不受信任的网络；生产环境应在反向代理层增加认证、TLS 和访问控制。
- `aurora-runtime` 是 Worker 与 CC Switch 共用的私有 bridge 网络；CC Switch 不向宿主机发布端口。
- `codex` runtime 使用容器执行且禁用本地回退；`openai_direct` 和手工工具路径仍可能在 Kali 镜像不可用时回退到本地执行，结果中的 `ToolTrace.backend` 会记录实际后端。
- 修改数据库、Artifact 目录或 Worker workspace 的权限后，确认运行 API 的用户可读写这些目录。
- 配置变更后的最小检查顺序：重启 API、访问 `GET /health`、运行 `./scripts/check.sh`；使用 Codex 时再运行 Worker 镜像和 Provider 预检。
