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
AURORA_WORKER_IMAGE_CORE=aurora-kali-codex:core
AURORA_WORKER_IMAGE_HEAVY=aurora-kali-codex:heavy
AURORA_CONTAINER_NETWORK=aurora-runtime
AURORA_CODEX_COMMAND_TEMPLATE=/workspace/runtime/codex-via-cc-switch.sh {prompt_filename} {output_schema_filename} {last_message_filename}
AURORA_CODEX_MODEL_CONTEXT_WINDOW=1000000
AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT=800000
AURORA_CODEX_TRANSCRIPT_MAX_BYTES=2097152
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
| `AURORA_DB_JOURNAL_MODE` | `wal` | SQLite journal 模式。WAL 允许读写并发，避免多写入方（租约心跳、Worker 回调、题目组并发）触发 `database is locked`。 |
| `AURORA_DB_BUSY_TIMEOUT_MS` | `5000` | SQLite busy 超时（毫秒）。写入冲突时排队等待而非立即报错。 |
| `AURORA_API_HOST` | `0.0.0.0` | `start.sh`/应用入口使用的 API 监听地址；生产环境仍应由受保护的反向代理接入。 |
| `AURORA_API_PORT` | `8000` | API 监听端口，必须为 1–65535。 |
| `AURORA_ARTIFACT_DIR` | `./artifacts` | 原始工具输出、导入附件和 Codex transcript 的存储目录。API 启动时会自动创建。 |
| `AURORA_API_LOCK_DIR` | 系统临时目录 | API 单实例锁目录；测试和多环境部署应分别配置。 |
| `AURORA_WORKER_RUNTIME` | `codex` | `codex`/`codex_harness`/`harness` 使用 Codex Harness；`openai_direct`/`openai`/`llm`/`real` 使用直接兼容接口。其他值直接报错。 |
| `AURORA_TOOL_CONTRACT` | `kali_shell` | Codex 运行时的 `tool_requests` 契约。`kali_shell` 只暴露无法在 Worker 容器内完成的服务端能力：`flag.verify`、`flag.submit`、`fofa.search`、`browser.interact`、可选 `blackboard.query`；网络侦察由 Kali shell 直接执行。`native_privileged` 恢复旧 gateway 网络工具，`full_gateway` 恢复全量语义工具面。该设置不启用目标授权。 |
| `AURORA_NATIVE_ALLOW_BLACKBOARD_QUERY` | `true` | 在 `aurora_blackboard` MCP 可用性稳定前保留 `blackboard.query` 作为 `tool_requests` 退化回退；设为 `false` 关闭。 |
| `AURORA_WORKER_IMAGE` | `aurora-kali-codex:latest` | 包含 Codex CLI 的 Kali Worker 镜像名。只有本地已存在的镜像才会被容器执行器使用。 |
| `AURORA_WORKER_IMAGE_CORE` | `AURORA_WORKER_IMAGE` 或 `aurora-kali-codex:core` | Web 题和手工语义工具使用的常用 CTF 工具镜像。 |
| `AURORA_WORKER_IMAGE_HEAVY` | `aurora-kali-codex:heavy` | Pwn、Reverse、Crypto、Forensics、Misc 和未知题型使用的完整分析镜像。 |
| `AURORA_CONTAINER_NETWORK` | `aurora-runtime` | Worker 与 CC Switch 共用的私有 Docker bridge 网络。 |
| `AURORA_WORKER_CONTAINER_CPUS` | `2` | Worker 容器的 CPU 限额，传给 `docker/podman run --cpus`。 |
| `AURORA_WORKER_CONTAINER_MEMORY` | `4g` | Worker 容器的内存限额，传给 `docker/podman run --memory`。 |
| `AURORA_BUILD_PROXY` | 未设置 | 可选的镜像构建 HTTP/HTTPS 代理；不会传入运行中的 Worker。 |
| `AURORA_CODEX_WORKSPACE_DIR` | `./codex-workspaces` | 每个项目和 Worker 的提示词、输入附件、输出 schema 和 transcript 工作目录。 |
| `AURORA_WORKER_CONTROL_BASE_URL` | `http://host.docker.internal:8000` | Worker 内 `aurora_blackboard` MCP 回连 Aurora 控制面的地址；令牌按 Attempt 生成且仅保存哈希。 |
| `AURORA_CODEX_PROXY_BASE_URL` | `http://aurora-cc-switch:15723/v1` | Worker 内 Codex 使用的 CC Switch Responses 地址。 |
| `AURORA_WORKER_REAP_INTERVAL_SECONDS` | `5` | API 后台线程清理过期 Worker lease 的间隔秒数。 |

运行时网络出口通过 Web 界面的“网络代理”设置，或通过 `GET/PUT /api/settings/network-proxy` 管理。`direct` 强制直连，`system` 继承 API 进程的代理环境，`custom` 将指定 HTTP(S) 代理用于新 Worker、Cataloger、Playwright 和附件下载。设置持久化在数据库中；已运行的 Worker 不会被重启。`127.0.0.1`、`localhost` 和 `aurora-cc-switch` 始终加入 `NO_PROXY`。宿主机代理若绑定在回环地址，Worker 容器会自动改用直连，避免把容器自身的 `127.0.0.1` 误当作宿主代理。

### Browser interaction

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_BROWSER_NAVIGATION_TIMEOUT_SECONDS` | `15` | 首次页面导航等待时间；超时后保留部分响应并执行一次轻量重试。 |
| `AURORA_BROWSER_RETRY_TIMEOUT_SECONDS` | `5` | 导航超时后的 `commit` 重试等待时间。 |
| `AURORA_BROWSER_DOM_TIMEOUT_SECONDS` | `5` | DOM 文本读取和点击后页面状态等待时间。 |
| `AURORA_BROWSER_ACTION_TIMEOUT_SECONDS` | `8` | 启动/创建控件点击等待时间。 |

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

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_CODEX_MODEL_CONTEXT_WINDOW` | `1000000` | 传给 Codex 的自定义模型上下文上限，避免未知模型使用错误的 fallback 元数据。 |
| `AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT` | `800000` | 达到此 token 数后触发 Codex 自动压缩，为最终结构化输出保留空间。 |
| `AURORA_CODEX_REQUIRE_EXPLICIT_MODEL_METADATA` | `true` | 要求显式设置上面两个模型预算变量；缺失时启动失败，禁止静默使用未知模型 fallback。 |
| `AURORA_CODEX_TRANSCRIPT_MAX_BYTES` | `2097152` | 单份 Harness transcript 的存储上限；超限时保留头尾并写入截断标记。 |

### LLM 与角色路由

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_LLM_API_KEY` | 未设置；回退到 `OPENAI_API_KEY` | `openai_direct` 和 Codex Harness 的上游凭据。 |
| `AURORA_LLM_BASE_URL` | `https://api.openai.com/v1`；回退到 `OPENAI_BASE_URL` | OpenAI 兼容服务地址。 |
| `AURORA_LLM_MODEL` | `gpt-4.1-mini`；回退到 `OPENAI_MODEL` | 默认模型。 |
| `AURORA_TRIAGE_MODEL` | 未设置 | 5 分钟分诊阶段模型；为空时使用 `AURORA_LLM_MODEL`。 |
| `AURORA_PLANNER_MODEL` | 未设置 | Planner 角色模型；为空时使用 `AURORA_LLM_MODEL`。 |
| `AURORA_SOLVER_MODEL` | 未设置 | Solver 角色模型；为空时使用 `AURORA_LLM_MODEL`。 |
| `AURORA_REVIEWER_MODEL` | 未设置 | 收尾、证据审查和 flag 验证修复模型；为空时使用 `AURORA_LLM_MODEL`。 |
| `AURORA_<ROLE>_MODEL_CONTEXT_WINDOW` | 未设置 | 可分别为 `TRIAGE`、`SOLVER`、`REVIEWER` 声明上下文窗口；必须与对应压缩阈值同时设置。 |
| `AURORA_<ROLE>_AUTO_COMPACT_TOKEN_LIMIT` | 未设置 | 对应角色的自动压缩阈值；为空时使用全局 Codex 元数据。 |
| `AURORA_LLM_TIMEOUT_SECONDS` | `120` | `openai_direct` 请求和部分 LLM 辅助请求的 HTTP 超时。 |

角色元数据变量的实际名称为 `AURORA_TRIAGE_MODEL_CONTEXT_WINDOW`、`AURORA_TRIAGE_AUTO_COMPACT_TOKEN_LIMIT`、
`AURORA_SOLVER_MODEL_CONTEXT_WINDOW`、`AURORA_SOLVER_AUTO_COMPACT_TOKEN_LIMIT`、
`AURORA_REVIEWER_MODEL_CONTEXT_WINDOW` 和 `AURORA_REVIEWER_AUTO_COMPACT_TOKEN_LIMIT`；同一角色的两个值必须成对设置。

### Intent 执行预算

这些变量是通用回退值；Intent 自身的 `budget` 字段可以覆盖它们，但题目组和 Evaluation 还会按阶段设置更严格的上限：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_DEFAULT_SOFT_TIMEOUT_SECONDS` | `300` | 到期后写入强制 checkpoint、向 Codex 发送 SIGINT 并进入收尾宽限。 |
| `AURORA_DEFAULT_HARD_TIMEOUT_SECONDS` | `1800` | Intent 的硬超时预算，也用于 Codex Harness 的有效超时和 lease 计算。 |
| `AURORA_DEFAULT_MAX_TOOL_CALLS` | `12` | 单次 Worker 最多执行的工具请求数。 |
| `AURORA_DEFAULT_MAX_REPEAT_FAILURES` | `2` | 同一工具失败达到该次数后跳过后续重复请求。 |
| `AURORA_DEFAULT_MAX_AGENT_ACTIONS` | `20` | Codex 内部 shell 动作预算；达到后进入结构化收尾。 |
| `AURORA_DEFAULT_MAX_ROUTE_REPEATS` | `2` | 同一路线连续失败的 Observer 纠偏阈值。 |
| `AURORA_DEFAULT_MAX_NO_PROGRESS_ACTIONS` | `5` | 没有新增 Fact 或实时 checkpoint 时允许的连续 Codex shell 动作数；达到后强制收尾。 |
| `AURORA_DEFAULT_FINALIZE_GRACE_SECONDS` | `60` | 软截止后的收尾宽限时间。 |
| `AURORA_RESUME_MAX_FILES` | `100` | 单个 Attempt 可进入恢复 manifest 的工作文件和 Codex 状态文件数量上限。 |
| `AURORA_RESUME_MAX_BYTES` | `67108864` | `/workspace/work` 持久文件的总字节上限。 |
| `AURORA_MAX_CHALLENGE_GROUP_CONCURRENT` | `2` | 题目组并行项目的全局上限（最大并发 Solver Agent 数）；单项目仍保持单 Worker。也可在 Web 题目组控制台直接调整，下次派发生效，重启后回退到环境变量。 |

当前有效预算层级如下，越靠后的阶段钳制优先级越高：

| 执行路径 | Phase 1 | Phase 2 | Phase 3 | 动作预算 |
| --- | --- | --- | --- | --- |
| 独立项目/普通 Scheduler 默认 | soft 3600s / hard 5400s | soft 3600s / hard 5400s | soft 3600s / hard 5400s | `max_agent_actions=0`、`max_no_progress_actions=0`，即默认不按命令数截断 |
| 普通题目组 | soft 1500s / hard 1800s | soft 3300s / hard 3600s | soft 3300s / hard 3600s | 同上；仍由超时与 `max_route_repeats` 约束 |
| Evaluation | soft 240s / hard 300s | soft 1140s / hard 1200s | soft 1440s / hard 1500s | 同上；对应 5/20/25 分钟阶段 |

如果 Intent 显式给出更小的阶段超时，题目组会保留更小值；更大的值会被阶段上限钳制。当前 Phase 1–4 默认关闭固定 shell 动作数与“无进展动作数”截断，因此 `AURORA_DEFAULT_MAX_AGENT_ACTIONS` 和 `AURORA_DEFAULT_MAX_NO_PROGRESS_ACTIONS` 主要是兼容回退值；要启用动作上限，应在 Intent `budget` 中显式设置 `max_agent_actions`。题目组会强制把 `max_no_progress_actions` 设为 `0`。

### Hands-free Cataloger

Hands-free URL 导入优先使用平台适配器和浏览器已观察到的 JSON 响应；这些确定性路径不要求配置 LLM。Cataloger LLM 用于通用静态页面分类，受限浏览器 Agent 只会在平台适配器和响应解析都没有得到可验证题目时运行，不执行 Solver 工具：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_CATALOGER_LLM_API_KEY` | 未设置 | 回退到 `AURORA_LLM_API_KEY`、`OPENAI_API_KEY`。为空时仍可使用确定性导入。 |
| `AURORA_CATALOGER_LLM_BASE_URL` | `https://api.openai.com/v1` | 回退到 `AURORA_LLM_BASE_URL`、`OPENAI_BASE_URL`。 |
| `AURORA_CATALOGER_LLM_MODEL` | `gpt-4.1-mini` | 回退到 `AURORA_LLM_MODEL`、`OPENAI_MODEL`。 |
| `AURORA_CATALOGER_LLM_TIMEOUT_SECONDS` | `120` | 回退到 `AURORA_LLM_TIMEOUT_SECONDS`。 |
| `AURORA_CATALOGER_AGENT_ENABLED` | `true` | 是否允许 Cataloger 执行受限的只读分页、筛选和题目详情展开。 |
| `AURORA_CATALOGER_MAX_AGENT_STEPS` | `12` | 单次导入允许的最大浏览器 Agent 动作数。 |
| `AURORA_CATALOGER_MAX_PAGES` | `20` | 从当前筛选页开始允许扫描的最大页数。 |
| `AURORA_CATALOGER_MAX_CANDIDATES` | `500` | 单次导入最多保留的已验证候选数。 |
| `AURORA_CATALOGER_MAX_RESPONSE_BYTES` | `2097152` | 单个同域 JSON 响应允许采集的最大字节数。 |
| `AURORA_CATALOGER_ATTACHMENT_TIMEOUT_SECONDS` | `20` | 单个附件从连接到读取完成的硬超时；超时后转为人工处理，不阻塞整批导入。 |

只有 API Key、Base URL 和模型都存在时，LLM/Agent 路径才算已配置。Agent 只能操作当前 DOM 中已观察到的控件，并拒绝登录、注册、提交答案/flag、启动题目环境和创建实例等动作。

### TSecBench

设置 `AURORA_TSECBENCH_BASE_URL`（默认 `https://tsecbench.zc.tencent.com`）和
`AURORA_TSECBENCH_TOKEN` 后，Cataloger 会识别平台根地址或 `/openapi/v1/challenges` 地址并读取题目列表。
也可直接使用平台跑分任务下发的 `BENCHMARK_BASE_URL` 和 `BENCHMARK_TOKEN`；若两组变量同时存在，
`AURORA_TSECBENCH_*` 优先，以保持现有部署行为不变。
也可以在 Web 左侧的 `TSecBench` 设置中配置 Base URL、Token、请求超时和实例并发上限。网页提交的 Token
只保存在当前 API 进程内存，重启后回退到环境变量；GET 配置接口不会返回 Token。Token 不会写入
ImportBatch、Project、ChallengeGroupItem、数据库设置或 Artifact。
`AURORA_TSECBENCH_TIMEOUT_SECONDS` 控制 API 请求超时，`AURORA_TSECBENCH_MAX_CONCURRENT` 最大为 `3`，对应平台同时最多启动 3 道题的限制。

导入候选保存 `platform=tsecbench` 和 `unique_code`。Runner 在 Solver 前调用 start，使用返回的 `container_addr`
建立项目目标并同步兼容用的授权主机记录，按阶段获取 Hint、提交 Flag，并在终态调用 close。Token 缺失或认证失败时导入进入
`NEEDS_SESSION`；Runner 的启动/Hint/关闭失败只记录诊断，不覆盖题目结果。

平台下发的 `container_addr` 是 SSLVPN 内的直连地址。Web 的“测试连接”会先验证 Challenge API；若题目列表中已有
`available` 容器，则从 API 宿主机对第一个地址执行短 TCP 探测并显示 VPN 路由状态。Aurora 不保存 VPN 凭据，也不自动拨号；
具体 SSLVPN 客户端或配置文件格式未包含在 Challenges API 文档中，需要先在宿主机建立平台下发的 VPN 连接。

### Slab Match Agent API

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_SLAB_MATCH_BASE_URL` | `https://example.com/slab-match/api/v1/agent` | Agent API 根地址；也兼容 `SLAB_MATCH_BASE_URL`。 |
| `AURORA_SLAB_MATCH_ACCESS_KEY` | 未设置 | `X-Agent-AccessKey` 凭据；也兼容 `SLAB_MATCH_ACCESS_KEY`。 |
| `AURORA_SLAB_MATCH_TIMEOUT_SECONDS` | `20` | 控制面请求、环境轮询和附件请求的基础超时秒数。 |
| `AURORA_SLAB_MATCH_MAX_CONCURRENT` | `1` | 默认动态靶机预算，运行时配置可覆盖。 |
| `AURORA_SLAB_MATCH_NOTICE_POLL_SECONDS` | `15` | 活动题组公告轮询间隔，必须为正数。 |

设置 `AURORA_SLAB_MATCH_BASE_URL` 和 `AURORA_SLAB_MATCH_ACCESS_KEY` 后，直接导入服务会从
`/slab-match/api/v1/agent` 读取开放题目列表与详情，并保存 `platform=slab_match` 和 `exercise_id`；
Runner 会在 Solver 前按需调用 `build-exercise-env`，
轮询题目详情直到 `isNeedCheck=false` 且 `endpoints` 可用，再把目标、账号密码摘要和附件注入项目上下文。
每个 Solver turn 结束后立即调用 `recover-exercise-env` 并清除旧目标记录；环境准备超时或缺少可用 endpoint 时也会回收。

也可以在 Web 左侧的 `Slab Match` 设置中配置 Base URL、Access Key、请求超时和 Planner 动态靶机预算；对应接口为
`GET/PUT /api/settings/slab-match`。连接测试使用 `POST /api/settings/slab-match/test`，只读取一次题目列表，不扫描题目详情或靶机。网页提交的 Access Key 只保存在
当前 API 进程内存，重启后回退到环境变量；GET 配置接口不会返回明文 Key。

Slab Match 使用独立的直接导入接口 `POST /api/slab-match/import`，不经过“解放双手”的页面扫描和候选确认。
该接口只顺序读取详情、下载附件并创建 `READY` 题组，不启动靶机、Planner 或 Agent。平台请求在进程内严格串行且间隔至少一秒。
`AURORA_SLAB_MATCH_MAX_CONCURRENT` 只作为 Planner 的动态靶机预算写入题组元数据；附件直接写入项目的 `challenge_input` Artifact。
API 还会按 `AURORA_SLAB_MATCH_NOTICE_POLL_SECONDS`（默认 15 秒）轮询公告列表；新公告正文、修正信息和公告附件会去重后写入所有活动
Slab Match 项目的 Blackboard 与 `challenge_input` Artifact，使运行中的 Solver 也能读取最新题目信息。

提交时，Runner 会读取 `match_info.rule`。只有规则明确要求“仅提交 `{}` 内内容”时，才从
`DASCTF{...}`/`flag{...}` 中提取花括号 payload；否则把本地验证的完整 flag 原样提交。平台拒绝不会自动重试同一候选，
拒绝值与原因会进入下一轮 Solver 上下文。

Slab Match 的控制面重定向必须保持与配置的 Agent API 同源，否则请求失败，避免 `X-Agent-AccessKey` 泄漏。
附件的初始 URL 和每次重定向都必须是无用户凭据、无 fragment 的绝对 HTTP(S) URL，并拒绝 localhost、
metadata 主机以及字面量私网、回环、link-local、reserved IP。AccessKey 只发送给 Agent API 同源附件；跨域附件和重定向会移除该请求头。

### 容器化 OpenVPN（可选）

Web 左侧的 `OpenVPN` 设置可上传单文件 OVPN、配置可选账号密码和需要经隧道访问的 IPv4/CIDR。
配置使用网页主密码加密后落库，主密码只驻留当前 API 进程；重启后必须重新解锁并手动连接。
VPN 运行在独立容器网络命名空间中，不修改宿主机路由。连接时强制忽略服务端下发的默认路由和 DNS，
仅网页列出的网段走隧道。连接健康时新 Solver Worker 共享该网络命名空间；掉线时阻止新 Worker，
不会回退直连。存在运行中的 Solver Worker 时不能连接、断开或修改配置。

首版只接受带内联证书/私钥的 TUN 配置；不支持 ZIP、外部证书路径、脚本/plugin 或 OVPN 内自定义路由。
运行 `scripts/runtime-up.sh` 会构建默认镜像 `aurora-openvpn:latest`，也可通过
`AURORA_OPENVPN_IMAGE`、`AURORA_OPENVPN_CONTAINER_NAME` 和 `AURORA_OPENVPN_CONNECT_TIMEOUT_SECONDS` 调整。连接等待默认 75 秒，且不会低于 OpenVPN 默认的 60 秒 TLS 握手窗口。

### FOFA 能力

两个凭据必须同时设置，`fofa.search` 才会对 Worker 暴露：

| 变量 | 默认值 |
| --- | --- |
| `AURORA_FOFA_EMAIL` | 未设置 |
| `AURORA_FOFA_KEY` | 未设置 |
| `AURORA_FOFA_BASE_URL` | `https://api.fofa.info/v1/search/all` |
| `AURORA_FOFA_TIMEOUT_SECONDS` | `20` |

凭据只决定 `fofa.search` 是否对 Worker 可见。当前实现把 `query` 原样发送给 FOFA，并把 `size` 限制为 1–100；
`PolicyEngine` 不再将查询约束为项目的 host/domain/ip。请通过 FOFA 账号权限、出站代理和 ToolTrace 审计控制使用范围。

### 同容器 Subagents

Subagents 需要同时满足全局开关、创建项目时的 `subagents_enabled=true`、Codex runtime 和主 Worker 条件：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AURORA_SUBAGENTS_ENABLED` | `false` | 接受 `1`、`true`、`yes`（不区分大小写）为启用。 |
| `AURORA_SUBAGENTS_MAX_CONCURRENT` | `2` | 每个 Worker 同时运行的子 Agent 数。 |
| `AURORA_SUBAGENTS_MAX_PER_WORKER` | `2` | 每个 Worker 的子 Agent 总数上限。 |
| `AURORA_SUBAGENT_CODEX_COMMAND` | 未设置 | 子 Agent 使用的 Codex 命令模板；应包含 `{prompt_filename}`、`{schema_filename}`、`{last_message_filename}`。默认 wrapper 会自动注入。 |

### Evaluation 运行约束

Evaluation 没有单独环境变量，使用 TSecBench 配置和角色模型配置。`POST /api/evaluations/suites` 会冻结当时的题目元数据和输入版本，
默认排除已完成或已有正确 flag 进度的题目；创建 run 时再记录当次角色模型、prompt contract 与工具清单哈希，并重新读取平台状态，执行以下硬检查：

- 同一 suite 不得存在另一个未进入 `COMPLETED`、`FAILED` 或 `STOPPED` 的 run；
- 每道题当前必须仍存在且没有平台已接受进度；
- suite 中不能包含尚未物化为本地 Artifact 的远程附件；
- variant 只能是 `baseline` 或 `candidate`。

一次成功的 baseline 会污染同一平台账号的题目状态，因此 candidate 必须使用独立的干净 Token/账号，或在平台重置接受进度后再创建。
单题阶段预算固定为 5/20/25 分钟，共 50 分钟。晋级需要至少 30 个有效样本、成功率提升 15 个百分点、错误提交率不变差、
重复请求达到降低门槛，并且派生候选验证覆盖率与终态 checkpoint 覆盖率都为 100%。

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
- 项目目标授权当前已禁用：`PolicyEngine` 对全部网关请求返回 `allow`。`AuthorizationScope`、`allowed_hosts`、`allowed_domains` 和 metadata 拒绝字段只保留为上下文/兼容数据，不是安全边界；原生 Codex shell 同样不按项目目标限流。必须在 Worker 网络、出站防火墙、VPN 或代理层强制允许范围。
- `aurora-runtime` 是 Worker 与 CC Switch 共用的私有 bridge 网络；CC Switch 不向宿主机发布端口。
- `codex` runtime 使用容器执行且禁用本地回退；`openai_direct` 和手工工具路径仍可能在 Kali 镜像不可用时回退到本地执行，结果中的 `ToolTrace.backend` 会记录实际后端。
- 不要提交 `.env`、数据库、Artifact、Codex workspace 或运行历史；`.gitignore` 已包含 ffuf 的运行时配置目录 `runtime/home/.config/ffuf/`。
- Worker 镜像内 vendored `glibc-all-in-one` 使用禁止商业使用的上游许可。分发镜像或用于商业环境前，应审查 `container/kali-codex/vendor/glibc-all-in-one/LICENSE` 并确认场景相容。
- 修改数据库、Artifact 目录或 Worker workspace 的权限后，确认运行 API 的用户可读写这些目录。
- 配置变更后的最小检查顺序：重启 API、访问 `GET /health`、运行 `./scripts/check.sh`；前端发布前进入 `apps/web` 执行 `node_modules/.bin/tsc --noEmit -p tsconfig.json`、`npm run build` 和 `npm audit --audit-level=moderate`；使用 Codex 时还要运行 Worker 镜像与 Provider 预检。
