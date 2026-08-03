# Aurora 使用指南

本文档面向第一次运行 Aurora 的开发者和题目分析人员。Aurora 是一个以 Fact-Intent 黑板为中心的安全/CTF 任务执行平台：用户创建项目并声明授权范围，Manager 生成可执行意图，Scheduler 分配 Worker，Worker 通过 Kali 工具或 Codex 收集证据，结果再写回事实、Artifact 和 Finding。

## 1. 开始前

### 必需软件

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Docker（使用默认的 Codex Harness 时必需）
- Node.js 和 npm（只在单独运行前端开发服务器时需要）

默认运行时还需要一个 OpenAI 兼容的模型服务地址和 API Key。请确认目标属于你有权测试的范围；Aurora 不会替代授权审批。

### 安装依赖

在项目根目录执行：

```bash
uv sync --extra dev
cp .env.example .env
```

然后编辑 `.env`。最小的 Codex Harness 配置如下（把密钥和模型替换成实际值）：

```dotenv
AURORA_DB_URL=sqlite:///./aurora.db
AURORA_ARTIFACT_DIR=./artifacts
AURORA_WORKER_RUNTIME=codex
AURORA_CONTAINER_NETWORK=aurora-runtime
AURORA_LLM_BASE_URL=https://api.openai.com/v1
AURORA_LLM_API_KEY=替换为真实密钥
AURORA_LLM_MODEL=gpt-4.1-mini
AURORA_CODEX_PROXY_BASE_URL=http://aurora-cc-switch:15723/v1
```

配置规则：环境变量优先于 `.env`；配置在进程第一次读取时缓存，因此修改 `.env` 后必须重启 API。不要把 `.env`、Cookie 或 API Key 提交到仓库。完整变量说明见 [`CONFIGURATION.md`](CONFIGURATION.md)。

## 2. 启动服务

### 推荐：Codex + Kali Worker

该模式让 Codex 在 Kali Worker 容器中运行，CC Switch 负责把 Codex Responses 请求转换为上游 Chat Completions 请求。

```bash
./scripts/runtime-up.sh
./scripts/verify-worker-image.sh
./scripts/check-codex-provider.sh
uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
```

`runtime-up.sh` 会构建 `aurora-kali-codex` 镜像、准备 CC Switch，并启动私有的 `aurora-runtime` 网络。API 启动后可用 `http://localhost:8000/health` 检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

停止容器运行时：

```bash
./scripts/runtime-down.sh
```

### 调试：直接调用 OpenAI 兼容接口

如果只想调试提示词和上下文，可以暂时使用直接 LLM Runtime：

```dotenv
AURORA_WORKER_RUNTIME=openai_direct
AURORA_LLM_BASE_URL=https://api.openai.com/v1
AURORA_LLM_API_KEY=替换为真实密钥
AURORA_LLM_MODEL=gpt-4.1-mini
```

然后重启 API：

```bash
uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
```

`openai_direct` 是调试路径，不是最终的 Solver Worker 边界；生产执行建议使用 Codex Harness。手工工具在容器不可用时可能回退到本地执行，实际后端会记录在 ToolTrace 中。

## 3. 使用 Web 界面

### 单体模式

构建前端后由 FastAPI 托管：

```bash
./scripts/build-web.sh
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
```

浏览器打开 <http://localhost:8000/>。

### 前后端分离开发

终端一启动 API，终端二启动 Vite：

```bash
# 终端一
uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

# 终端二
cd apps/web
npm install
AURORA_DEV_API_URL=http://localhost:8000 npm run dev
```

打开 <http://localhost:5173/>。Vite 默认把 `/api` 代理到 8001；API 使用 8000 时请像上面一样设置 `AURORA_DEV_API_URL`。跨域部署时可在构建阶段设置 `VITE_API_BASE`。

### 第一个项目

1. 在左侧项目区域填写目标并点击“新建项目”。
2. 从项目下拉框选择项目；执行图会显示 Intent、Worker、Attempt、Artifact 和 Finding 的关系。
3. 使用“运行规划器”消费线索并创建 Intent，使用“运行观察器”检查策略拒绝、重复调用或缺少证据等异常。
4. 点击“开始自动解题”运行后台循环；需要立即终止时点击“停止自动解题”。
5. 在“控制台”中可以更新题目站 Cookie、手动加入 Intent，或直接执行一个受策略检查的工具请求。
6. 在“证据/结论”面板查看 Artifact 原文，并用选中的 Artifact 推导可追溯事实。

界面中的“刷新”只重新读取数据库；实时动作通过项目事件流自动更新。项目进入 `COMPLETED`、`FAILED` 或 `CANCELLED` 后，新增 Intent、Hint 和证据事实等黑板写入会被锁定。

## 4. API 基本流程

以下示例假设 API 在 `http://localhost:8000`。先创建项目，并明确允许访问的主机：

```bash
curl -sS -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "本地 Web 题",
    "goal": "分析授权的本地 Web 题并寻找可验证证据",
    "challenge_type": "web",
    "allowed_hosts": ["127.0.0.1"],
    "hint": "先请求本地首页"
  }'
```

从返回 JSON 的 `id` 得到 `PROJECT_ID`，然后按需执行：

```bash
PROJECT_ID=proj_xxx

# 查看项目黑板
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/blackboard

# 让 Manager 根据 Hint 生成 Intent
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/manager/run

# 执行最高优先级的一个 Intent
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/scheduler/run-next

# 查看汇总结果
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/summary
```

也可以直接加入一个带工具输入的 Intent。工具输入保存在 `budget.tool_request` 中：

```bash
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/intents \
  -H 'Content-Type: application/json' \
  -d '{
    "objective": "读取授权首页的响应头",
    "capability_tags": ["http.request"],
    "priority": 2,
    "risk_level": "low",
    "tool_request": {
      "url": "http://127.0.0.1/",
      "timeout_seconds": 3
    }
  }'
```

`POST /scheduler/run-next` 每次只领取并运行一个 Intent。模型和工具产生的原始输出会存为 Artifact，系统会把候选 `flag{...}`/`ctf{...}` 交给 Flag Validator；确认有效的候选会形成 Finding 并推动项目完成，随后取消剩余待执行 Intent。

## 5. 自动解题

### 同步运行

适合脚本或一次性任务。请求会一直等待到项目完成、达到限制或被阻塞：

```bash
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/autorun/start \
  -H 'Content-Type: application/json' \
  -d '{
    "max_iterations": 20,
    "max_minutes": 0,
    "no_progress_limit": 4,
    "stop_on_observer_escalate": true,
    "background": false
  }'
```

### 后台运行

Web 界面使用后台模式。启动后通过状态接口轮询，或在界面观察实时事件：

```bash
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/autorun/start \
  -H 'Content-Type: application/json' \
  -d '{"max_iterations":20,"max_minutes":0,"no_progress_limit":4,"background":true}'

curl -sS http://localhost:8000/api/projects/$PROJECT_ID/autorun/status
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/autorun/stop
```

循环每轮依次运行 Observer、Manager 和一个 Scheduler/Worker。常见停止原因包括：

- `project_completed`：项目已完成；
- `max_iterations` 或 `max_minutes`：达到用户设置的上限；
- `no_runnable_work`：没有可领取的 Intent；
- `no_progress`：连续多轮没有新增事实、Artifact 或 Finding；
- `observer_escalate`：策略拒绝或其他异常需要人工复核；
- `runtime_error`：Worker Runtime 启动或执行失败。

Observer 是确定性的，不调用 LLM。它会在最近工具请求被策略拒绝、连续重复调用或失败尝试没有证据时提出 `ESCALATE`/`REDIRECT`/`REQUEST_EVIDENCE`。

## 6. 手工工具与授权范围

所有语义工具先经过项目 `AuthorizationScope` 检查。当前公开的创建项目接口只接收 `allowed_hosts`；请在创建项目时列出每个获授权的主机或域名，再执行工具。底层策略也支持 `allowed_domains`，但尚无独立的公开编辑接口。元数据和管理网络（例如 `169.254.169.254`）默认禁止。

| 工具 | 常用请求字段 | 说明 |
| --- | --- | --- |
| `http.request` | `url`, `method` | MVP 仅允许 GET、HEAD、OPTIONS。 |
| `network.scan` | `target`, `ports` | 使用 Kali `nmap`，默认端口为 `80,443,8080`。 |
| `web.enumerate` | `url`, `wordlist`, `extensions` | 优先 `ffuf`，没有时回退 `dirb`。 |
| `binary.inspect` | `path` | 使用 `file`、`sha256sum`、`strings`。 |
| `forensic.inspect` | `path` | 使用 `file`、`exiftool`、`binwalk`。 |
| `browser.interact` | `url`, `locator`, `wait_seconds` | 必须先为项目设置题目站 Cookie。 |
| `sandbox.exec` | `command`, `cwd`, `timeout_seconds` | 受命令拒绝规则和工作区路径限制。 |
| `blackboard.query` | `limit` | 只读查询当前项目事实，并生成 Artifact。 |
| `fofa.search` | `query`, `size` | 需配置 FOFA 凭据，查询必须是单个 host/domain/ip 等式。 |

直接执行工具的 API 形式：

```bash
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/tools/http.request/execute \
  -H 'Content-Type: application/json' \
  -d '{"request":{"url":"http://127.0.0.1/","timeout_seconds":3}}'
```

响应中的 `artifact_refs` 指向原始证据，`trace_id` 可用于追踪策略决定和后端执行结果。`sandbox.exec` 的 `cwd` 必须位于项目工作区根目录下，危险命令会被拒绝；不要把它当作不受限制的宿主机 Shell。

### 浏览器会话

题目站需要登录时，可在 Web 界面的“题目站浏览器会话”中填写详情页 URL 和 Cookie，也可调用：

```bash
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/browser/session \
  -H 'Content-Type: application/json' \
  -d '{"source_url":"https://ctf.example/challenge/1","cookie":"session=..."}'
```

浏览器交互只允许停留在已认证题目域名及其子域名。没有会话时，`browser.interact` 会返回 `browser session required`；从题目页发现的目标会写入 `DiscoveredTarget`，后续仍会经过项目策略检查。

## 7. “解放双手”批量导入

Web 界面的“解放双手”用于从赛事、题库或题目列表 URL 归集候选题目和附件。Cataloger 只负责分类和归集，不执行 Solver 工具。

1. 打开“解放双手”，输入题目列表 URL。
2. 选择匿名、Cookie 或账号密码方式并点击“识别”。
3. 通过进度条等待 `READY`；若返回 `NEEDS_SESSION`，补充 Cookie/登录信息后点击“继续抓取”。
4. 检查候选题目、题型、置信度和附件来源，取消不需要的项目并修改项目名称。
5. 点击“批量创建项目”。系统会创建项目组、执行授权目标校验，并把项目加入题目组。
6. 在题目组面板点击“开始组解题”或“停止”。

对应 API：

```text
POST /api/hands-free/imports
GET  /api/hands-free/imports/{batch_id}
GET  /api/hands-free/imports/{batch_id}/events
POST /api/hands-free/imports/{batch_id}/continue
POST /api/hands-free/imports/{batch_id}/confirm
POST /api/challenge-groups/{group_id}/start
POST /api/challenge-groups/{group_id}/stop
```

登录凭据只用于本次隔离浏览器抓取；仍应使用短期 Cookie，并在任务结束后撤销或更换凭据。

## 8. 查看证据和调试记录

常用查询如下：

```bash
# 只看发现和事实
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/findings
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/targets

# 原始 Artifact 列表和内容
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/artifacts
curl -sS 'http://localhost:8000/api/artifacts/artifact_xxx/content?max_bytes=64000'

# 执行链和调试信息
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/attempts
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/events
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/debug/llm-traces
curl -sS http://localhost:8000/api/projects/$PROJECT_ID/debug/tool-traces
curl -sS 'http://localhost:8000/api/projects/'$PROJECT_ID'/runtime/logs?tail=200'
```

Artifact 保存原始工具输出、导入附件和 Codex transcript；上下文快照和调试面板默认只展示摘要及引用。安全相关字段会在上下文快照中脱敏。

需要清空当前工作黑板但保留原始证据和审计记录时，可调用：

```bash
curl -sS -X POST http://localhost:8000/api/projects/$PROJECT_ID/rethink
```

该操作会停止自动运行和项目容器，重新创建一个 Bootstrap Intent；执行前请确认没有仍在运行的 Worker。

## 9. 配置可选能力

### FOFA

同时配置以下变量后，Worker 才会看到 `fofa.search`：

```dotenv
AURORA_FOFA_EMAIL=you@example.com
AURORA_FOFA_KEY=your-fofa-key
```

查询仍必须严格匹配一个 `host="..."`、`domain="..."` 或 `ip="..."` 表达式，并且目标在项目授权范围内。

### 同容器 Subagents

先开启全局开关，再在创建项目时传入 `subagents_enabled: true`：

```dotenv
AURORA_SUBAGENTS_ENABLED=true
AURORA_SUBAGENTS_MAX_CONCURRENT=2
AURORA_SUBAGENTS_MAX_PER_WORKER=4
```

子 Agent 与父 Worker 共享容器和 `/workspace`，不会递归创建子 Agent；每个子 Agent 的结果和 transcript 都会写入独立的 Worker、Attempt、Trace 和 Artifact 记录。

## 10. 常见问题

### Provider 检查失败

确认 `.env` 中 `AURORA_LLM_BASE_URL`、`AURORA_LLM_API_KEY`、`AURORA_LLM_MODEL` 有效，CC Switch 已运行且容器健康。重新执行：

```bash
./scripts/runtime-up.sh
./scripts/check-codex-provider.sh
```

### Worker 无法启动

检查 Docker 是否运行、`aurora-kali-codex:latest` 是否存在，以及 `AURORA_CONTAINER_NETWORK` 是否与 compose 创建的网络一致。`GET /api/projects/{project_id}/runtime/logs` 可查看容器输出。

### 请求被拒绝

查看项目的 ToolTrace 和 Observer 事件。常见原因是目标不在 `allowed_hosts`/`allowed_domains`、访问元数据网络，或 FOFA 查询格式不符合限制。不要通过关闭策略来绕过授权问题，应修正项目范围。

### 自动解题停在 `no_progress` 或 `observer_escalate`

打开最近的事件、ToolTrace、Artifact 和运行时警告，确认是否缺少 Hint、工具输入错误或目标不可达。补充 Hint/Intent 后可再次启动；需要保留现有证据并换一条思路时使用 `rethink`。

### 导入停在 `NEEDS_SESSION`

目标题库需要登录。回到导入窗口选择 Cookie 或账号密码，提交后点击“继续抓取”。如果站点使用多因素认证或动态登录，先在浏览器完成登录，再粘贴有效 Cookie。

## 11. 本地验证

运行后端测试和前端构建：

```bash
./scripts/check.sh
```

也可以分别执行：

```bash
uv run --extra dev pytest
cd apps/web && npm run build
```

生产部署前，请在反向代理层添加认证、TLS 和访问控制。当前 API 开放全量 CORS，不能直接暴露到不受信任的公网。
