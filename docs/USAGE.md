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

使用 TSecBench 题目时，额外设置 `AURORA_TSECBENCH_BASE_URL` 和 `AURORA_TSECBENCH_TOKEN`。从 TSecBench 根地址或
`/openapi/v1/challenges` 创建导入批次即可读取题目；确认题目后，Runner 会按平台生命周期启动容器、提交 Flag
并关闭环境。`container_addr` 仅作为 TSecBench 授权靶机写入该项目的目标和授权范围。

也可以在 Web 左侧打开 `TSecBench`，填写 Base URL 和 Benchmark Token，保存后点击“测试连接”。如果列表中存在
状态为 `available` 的容器，界面会同时检测其 SSLVPN 地址是否可达。网页 Token 仅在当前 API 进程内有效；长期配置仍应写入 `.env`。

如不希望 SSLVPN 修改宿主机网络，可在 Web 左侧打开独立的 `OpenVPN` 设置：上传包含内联证书的 `.ovpn`，
设置至少 10 字符的加密主密码，逐行填写需要转发的 IPv4/CIDR，并按“保存配置 → 连接”操作。
只有列出的网段会进入隧道；未连接时保持原有网络。API 重启后配置仍在，但必须重新输入主密码解锁并手动连接。
连接、断开或修改配置前应停止所有 Solver Worker，VPN 掉线时 Aurora 会阻止新 Worker 而不会回退直连。

## 2. 启动服务

### 一体化启动

配置好 `.env` 后，只需运行：

```bash
./start.sh
```

脚本会自动同步 Python 依赖、按需安装前端依赖、构建 Web，并检查 Worker、OpenVPN 与 CC Switch 镜像。
镜像存在且 Worker 工具清单标签匹配时直接复用，缺失或过期时才构建；随后启动私有运行时并以前台方式启动 API。
可用 `AURORA_API_HOST`、`AURORA_API_PORT` 调整监听地址和端口，或用 `./start.sh --rebuild` 强制重建所有镜像。
按 `Ctrl+C` 停止 API；运行 `./scripts/runtime-down.sh` 停止运行时容器。

### 推荐：Codex + Kali Worker

该模式让 Codex 在 Kali Worker 容器中运行，CC Switch 负责把 Codex Responses 请求转换为上游 Chat Completions 请求。

```bash
./scripts/runtime-up.sh
./scripts/verify-worker-image.sh
./scripts/check-codex-provider.sh
uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
```

`runtime-up.sh` 会构建共享层的 `aurora-kali-codex:core` 与 `aurora-kali-codex:heavy` 镜像、准备 CC Switch，并启动私有的 `aurora-runtime` 网络。Web 题路由到 core，其余题型路由到 heavy；选中的镜像和实际能力会写入 Solver 的 `tool_environment`。API 启动后可用 `http://localhost:8000/health` 检查：

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
6. 在“证据/结论”面板逐条填写支撑观察，为每条观察关联原始 Artifact，再形成可追溯结论。

界面中的“刷新”只重新读取数据库；实时动作通过项目事件流自动更新。项目进入 `FLAG_READY`、`AWAITING_MANUAL_VALIDATION`、`COMPLETED`、`FAILED` 或 `CANCELLED` 后，新增 Intent、Hint 和证据事实等黑板写入会被锁定。

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

`POST /scheduler/run-next` 每次只领取并运行一个 Intent。模型和工具产生的原始输出会存为 Artifact，但只有题目附件、可信目标响应或成功的 `flag.verify` 双重重放结果可以证明候选 flag。候选通过本地校验后形成 Finding 并进入 `FLAG_READY`；比赛平台或人工接受后才完成项目并取消剩余 Intent。模型文本、transcript、黑板摘要和普通 `sandbox.exec` 回显不能证明 flag。

每个主 Solver 轮次进入 `SUCCESS`、`PARTIAL`、`FAILED` 或 `TIMEOUT` 后，系统都会在 blackboard 中写入一条轮次反思（API 中沿用 `checkpoints` 字段）。反思会整理本轮总结、证据结论、假设、失败路线和下一步，并审核 Solver 的候选建议后创建最多 3 个后续 Intent。Solver 输出的 `suggested_intents` 本身不会绕过反思直接进入调度队列；反思模型不可用时，系统使用确定性 fallback 规则校验这些建议。项目已经完成时仍保留最终反思，但不会再创建 Intent。Subagent 报告由主轮汇总，不单独触发反思。

Fact 使用 `statement` 保存结论，使用 `evidence_items` 保存支撑结论的可观察证据。每个证据项包含 `description` 和至少一个 `artifact_refs`；例如证据可以是“参数 `id=1'` 返回 SQL 语法错误”，结论则是“目标接口的 `id` 参数存在 SQL 注入”。顶层 `evidence_refs` 是所有证据项引用的并集，用于兼容执行图和旧客户端。仅提交 `evidence_refs` 的旧格式仍然有效，但界面会标记为“仅关联原始材料”。

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

Web 界面的“解放双手”用于从赛事、题库或题目列表 URL 归集候选题目和附件。系统按以下顺序处理：已知平台 API 适配器（当前包括 CTF+ 和 CTFd）、浏览器同域 JSON 响应解析、Cataloger 分类，以及最后的受限浏览器 Agent。Agent 只进行只读分页、筛选和题目详情展开，不执行 Solver 工具，也不会登录、提交答案、启动实例或执行任意 URL/脚本。

1. 打开“解放双手”，输入题目列表 URL。
2. 选择匿名、Cookie 或账号密码方式并点击“识别”。
3. 系统从当前筛选视图开始采集并继续处理分页，直到没有新题目或达到配置上限。通过进度条等待 `READY`；若返回 `NEEDS_SESSION`，补充 Cookie/登录信息后点击“继续抓取”。
4. 检查候选题目、题型、置信度和附件来源，取消不需要的项目并修改项目名称。
5. 点击“批量创建项目”。系统会创建项目组并把项目加入题目组；靶机不是启动解题的前置条件。
6. 在题目组面板点击“开始组解题”或“停止”。

导入项目会先使用题目描述、已暂存附件和现有 Artifact 开始本地分析。没有靶机、自动识别缺少登录会话或平台环境暂不可用时，系统只记录告警并继续解题；需要网络交互时，可在项目的“靶机（可选）”面板自动识别或人工指定地址。具体网络请求仍必须通过项目授权策略。

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

候选必须具备平台 API、已观察 JSON 对象或明确题目详情链接等身份依据；低置信度推测、当前列表页、导航链接、Logo、脚本、样式和外部平台链接都会被过滤。若无法验证任何题目，批次仍以 `READY` 返回并展示诊断信息，但候选列表为空且不能执行“批量创建项目”。

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

左侧“网络代理”可切换直连、系统代理和自定义 HTTP(S) 代理。保存后，新启动的 Solver、题目归集浏览器和附件下载使用同一网络出口；人工审查附件的下载图标通过 Aurora 流式转发，来源图标仍直接打开原始地址。

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

### 导入完成但候选为零

查看导入窗口中的诊断信息。零候选表示系统没有找到足够证据证明某个对象是题目，而不是自动把导航和资源链接降级为候选。确认 URL 保留了需要的筛选参数；若页面需要登录或登录后才加载题目，请提供有效短期 Cookie 后重新识别。

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
