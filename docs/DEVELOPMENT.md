# Aurora v2 开发文档

本文档面向需要理解、修改或扩展 Aurora 的开发人员。运行和配置请分别参考 [`USAGE.md`](USAGE.md) 与 [`CONFIGURATION.md`](CONFIGURATION.md)。

## 1. 项目概述

Aurora 是一个以 Fact-Intent 黑板为中心的 CTF/安全任务执行平台。用户导入题目或手工创建项目后，系统生成可执行 Intent，Scheduler 分配 Worker，Worker 使用 Kali 原生工具或 Codex 收集证据，结果写回 Fact、Artifact、Finding、Checkpoint 和 FlagCandidate，最终由平台适配器完成 Flag 提交。

核心原则：

- **Intent-first**：每轮只解决一个意图，不把整个题目直接丢给模型。
- **证据优先**：只有可信 Artifact 中的 flag 才能成为候选；`flag.verify` 通过隔离重放验证推导结果。
- **状态可审计**：主要动作都落库或写 `WorkerEvent` / `ChallengeGroupEvent`，便于追踪和恢复。
- **靶机生命周期归 phase**：TSecBench/Slab Match 容器在 phase 状态归并后再释放，部分 Flag 接受时保留环境。

## 2. 仓库结构

```
aurora/
  config.py                 # 配置模型 Settings，环境变量读取与缓存
  db.py                     # SQLModel engine、SQLite pragma、schema 迁移/回填
  models.py                 # 全部 SQLModel 表
  api.py                    # FastAPI 路由与生命周期
  prompts/                  # Manager/Observer/Solver/Cataloger 提示词
  services/                 # 业务服务，详见服务层地图
apps/
  api/main.py               # create_app() 的进程入口
  web/                      # React/Vite 前端
container/
  kali-codex/               # Kali + Codex Worker 镜像
  openvpn/                  # 容器化 OpenVPN 网关
  cc-switch/                # Codex Responses -> Chat Completions 转换服务
scripts/                    # 启动、构建、诊断脚本
tests/                      # pytest 测试
docs/                       # 使用、配置、开发和验收文档
```

## 3. 环境与常用命令

```bash
uv sync --extra dev
cp .env.example .env
./start.sh                 # 构建/启动完整栈
uv run uvicorn apps.api.main:app --reload
```

测试：

```bash
uv run --extra dev pytest -q
uv run --extra dev pytest -q tests/test_flag_verification.py
```

关键启动脚本：

- `./scripts/runtime-up.sh`：启动 CC Switch、OpenVPN 等运行时容器
- `./scripts/runtime-down.sh`：停止运行时容器
- `./scripts/dev-api.sh`：开发态 API 辅助启动
- `./scripts/check-codex-provider.sh`：检查上游 provider 是否兼容 Codex CLI

## 4. 数据模型

`aurora/models.py` 定义全部表。核心实体：

| 实体 | 作用 |
| --- | --- |
| `Project` | 一个解题任务，含目标 URL、状态、题型 |
| `AuthorizationScope` | 项目网络授权范围 |
| `ProjectRuntimePolicy` | 子代理、并发、预算等运行时策略 |
| `Fact` | 黑板事实，带证据引用 |
| `Intent` | 可执行意图，带优先级、能力标签和预算 |
| `Attempt` | 一次 Worker 执行尝试 |
| `AttemptCheckpoint` | 回合检查点，用于续跑 |
| `Worker` | Scheduler 分配的租约载体 |
| `Artifact` | 文件型证据 |
| `Finding` | 漏洞、凭据或候选 flag 结论 |
| `FlagCandidate` | 平台提交候选，含状态和 provenance |
| `ContextSnapshot` | 每轮发送给模型的结构化上下文 |
| `LLMTrace` | 模型输入/输出追踪 |
| `ToolTrace` | 服务端工具调用追踪 |
| `WorkerEvent` | 项目执行事件 |
| `ChallengeGroup` / `ChallengeGroupItem` / `ChallengeGroupEvent` | 批量题目组调度与事件 |
| `ImportBatch` / `ImportCandidate` / `ImportArtifact` | Hands-free/Slab Match 导入 |
| `EvaluationSuite` / `EvaluationRun` / `EvaluationItemResult` | 评测 |
| `DiscoveredTarget` | 靶机候选 |

常见状态：

- `Project.status`：`ACTIVE`、`WORKING`、`FLAG_READY`、`COMPLETED`、`FAILED`、`CANCELLED`、`WAITING_INPUT`、`WAITING_RESOURCE`、`AWAITING_MANUAL_VALIDATION`
- `ChallengeGroupItem.fused_status`：`PENDING`、`RUNNING`、`WAITING_INPUT`、`WAITING_RESOURCE`、`COMPLETED`、`FAILED`、`AWAITING_MANUAL_VALIDATION`
- `FlagCandidate.status`：`LOCAL_VERIFIED`、`SUBMITTED`、`ACCEPTED`、`REJECTED`、`AWAITING_MANUAL_VALIDATION`
- `Intent.status`：`PENDING`、`CLAIMED`、`RUNNING`、`CONCLUDING`、`COMPLETED`、`FAILED`、`CANCELLED`

## 5. 核心执行链路

### 5.1 单项目自动运行

`AutoRunnerService.run_until_stop` 是单项目外层循环：

1. 检查项目终态、截止时间和停止信号。
2. 若无 `PENDING` Intent，则调用 `ManagerService.run_project`；仍为空则播种 fallback Intent。
3. 将预算和 deadline 写入 `Intent.budget`。
4. 调用 `demo.run_one_demo_step` 执行一轮。
5. 用 `ResultProcessor` 落库本轮事实、候选、检查点。
6. 根据新增事实/Artifact/Finding 更新无进展计数，直至完成、超时或停止。

### 5.2 单轮执行

`demo.run_one_demo_step`：

- `Scheduler.claim_next` 领取 Intent，创建 `Worker` 和 `Attempt`。
- `ContextBuilder.build` 生成 `ContextSnapshot`。
- `WorkerRuntime.execute` 运行模型。
- `CapabilityGateway.execute` 执行模型请求的 `tool_requests`。
- `ResultProcessor.apply` 处理输出；`RoundReflectionService.create` 保存检查点和续跑 Intent。

### 5.3 题目组调度

`ChallengeGroupRunner` 负责批量题目：

- 顺序模式：一次调度一个 item。
- 并发模式：按 `max_concurrent` 保持有界并发。
- phase 1/2/3 分别有软/硬超时和最大路线重复数。
- 每个 item 在派发前申请靶机，phase 结束后释放靶机；`FLAG_PARTIAL` 时保留环境并重置 phase 时间窗。

### 5.4 状态与恢复

- `Scheduler.reap_expired` / `reconcile_orphans` 清理过期租约和孤儿 Worker。
- `project_rethink.py` 重新打开失败项目并清理不可恢复的运行态。
- `project_rethink_registry.py` 在请求外执行 stop/reset/restart，避免请求内长任务阻塞。
- `api_instance_lock.py` 保证同一数据库只有单个 API 进程持有实例锁。

## 6. 服务层地图

| 服务 | 职责 |
| --- | --- |
| `artifact_store.py` | Artifact 文件读写、大小限制 |
| `blackboard_repository.py` | Fact/Intent upsert、路由指纹和证据归一化 |
| `manager.py` | 从 hint/checkpoint 生成续跑 Intent |
| `observer.py` | 重复路线、策略拒绝、无证据尝试检测与重定向 |
| `scheduler.py` | 租约、心跳、回收、孤儿调和 |
| `autorunner.py` | 单项目自动循环和 fallback Intent |
| `harvester_runner.py` | 把 AutoRunner 适配成 ChallengeGroupRunner 的 Harvester |
| `challenge_group_runner.py` | 题目组、phase、靶机生命周期和 Flag 归并 |
| `context_builder.py` | 生成模型上下文 |
| `worker_runtime.py` | OpenAI 直连与 Codex Harness 两个运行时 |
| `result_processor.py` | 模型输出规范化、Fact/Finding/FlagCandidate 落库 |
| `round_summary.py` | 回合检查和 checkpoint 生成 |
| `capability_gateway.py` | 服务端门禁工具执行 |
| `flag_validator.py` | Flag 语法校验、可信 Artifact 扫描、诱饵/黑名单 |
| `flag_submission.py` | 平台提交与结果回写 |
| `flag_rejection.py` | 拒绝记录 |
| `competition_adapter.py` | Local/TSecBench/Slab Match 适配 |
| `tsecbench.py` | TSecBench OpenAPI 客户端 |
| `slab_match.py` / `slab_match_import.py` / `slab_match_notices.py` | Slab Match 控制面、导入和公告 |
| `hands_free.py` | 页面扫描、候选提取和批量导入 |
| `target_management.py` | 靶机候选提取、探测、确认 |
| `project_deletion.py` | 项目及其关联数据删除 |
| `project_repair.py` | 错误 Flag 后重新打开项目 |
| `project_rethink.py` / `project_rethink_registry.py` | rethink 状态恢复 |
| `evaluation.py` | baseline/candidate 评测对比 |
| `reliability.py` | 项目可靠性报告 |
| `subagent_collector.py` | 子代理结果收集 |
| `browser_interaction.py` / `browser_sessions.py` | 受控浏览器会话与靶机页面操作 |
| `openvpn_gateway.py` | OpenVPN 配置、解锁、连接和健康检查 |
| `network_proxy.py` | 全局代理配置 |
| `command_runner.py` | 本地/Kali 容器/自动命令执行 |
| `worker_control.py` | Worker 黑板的内部回调认证与写入 |
| `tool_contract.py` / `tool_profiles.py` | 工具合同与运行环境声明 |
| `mcp_registry.py` | 可见 MCP 工具计算 |
| `agent_profiles.py` | Agent profile 注册 |

## 7. Worker 运行时

- `OpenAICompatibleRuntime`：调试用，直接调用 OpenAI-compatible API。
- `CodexHarnessRuntime`：生产用，把 prompt/output schema 写入 Worker workspace，通过 `codex-via-cc-switch.sh` 运行 Codex。
- `get_worker_runtime()` 根据 `AURORA_WORKER_RUNTIME` 返回实现。
- Codex 运行时会准备 resume manifest、恢复 Codex 状态、解析结构化输出、处理软/硬超时，并把 transcript 保存为 Artifact。

## 8. 服务端门禁工具

`CapabilityGateway.execute` 处理不能在 Worker 本地完成的工具：

- `flag.verify`：隔离目录中运行验证脚本两次，要求输出一致且合法。
- `flag.submit`：仅允许 `candidate_id` 或 `value` 之一，且候选必须属于当前项目。
- `fofa.search`：代理携带凭据的 FOFA 请求。
- `browser.interact`：受控浏览器会话。
- `blackboard.query`：Worker 无法直接使用 MCP 时的黑板回退。

网络类 Kali 工具仍由 Worker 原生执行，不进入 `tool_requests`。

## 9. Flag 全链路

1. `FlagValidator.extract_candidate_flags` 扫描可信 Artifact。
2. `FlagValidator.is_valid_flag_value` 做语法、可读性、占位符和前缀黑名单校验。
3. `ResultProcessor.apply` 把模型输出中的候选写入 `FlagCandidate`。
4. `CapabilityGateway._flag_verify` 对推导候选做双次隔离重放。
5. `FlagSubmissionService.submit` 通过平台适配器提交候选。
6. 平台结果回写候选、项目、题目组状态，并记录拒绝原因到下一轮上下文。

多 Flag 部分接受的关键逻辑：

- `competition_adapter.py` 把 `correct_flag_count` / `flag_count` / `is_completed` 写入 `ChallengeGroupItem.competition_meta`。
- `context_builder.py` 在 `competition_context.progress` 暴露给模型。
- `challenge_group_runner.py` 在 `FLAG_PARTIAL` 时不释放靶机，并清空 phase 时间窗。

## 10. 平台适配

- `LocalCompetitionAdapter`：本地/manual 流程，不调用外部平台。
- `TSecBenchCompetitionAdapter`：按需 start/close 容器，支持 hint、submit_result 和多 Flag 进度。
- `SlabMatchCompetitionAdapter`：导入后轮询 endpoint/公告，按赛事规则规范 Flag 格式。

## 11. Prompt 与上下文

- `ContextBuilder` 组装 `project_goal`、`target_access`、`competition_context`、`flag_submission`、`current_intent`、`facts`、`artifact_summaries`、`recent_checkpoints`、`flag_validation_feedback` 等。
- `aurora/prompts/` 保存 Manager、Observer、Solver、Cataloger 的系统提示。
- 所有模型输出必须是单一 JSON，字段由 `context_builder.OUTPUT_SCHEMA` 声明。

## 12. API 概览

主要路由定义在 `aurora/api.py`：

- `/api/projects`、`/api/projects/{project_id}`、`/api/projects/solve`
- `/api/projects/{project_id}/intents`、`/hints`、`/facts`、`/blackboard`、`/summary`
- `/api/projects/{project_id}/run-demo`、`/scheduler/run-next`、`/manager/run`、`/observer/run`
- `/api/projects/{project_id}/autorun/start|step|stop|status`
- `/api/projects/{project_id}/tools/{tool_name}/execute`
- `/api/challenge-groups`、`/start`、`/stop`
- `/api/hands-free/imports`
- `/api/slab-match/import`
- `/api/settings/...`、`/api/tsecbench/...`、`/api/openvpn/...`
- 调试路由：`/api/projects/{project_id}/debug/context-snapshots`、`/debug/llm-traces`、`/debug/tool-traces`

## 13. 测试约定

- 使用 SQLite 内存库或临时目录数据库，测试之间避免共享 `/tmp/aurora_test.db`。
- 针对 Flag 逻辑至少运行 `tests/test_result_processor.py` 和 `tests/test_flag_verification.py`。
- 针对题目组/平台逻辑至少运行 `tests/test_tsecbench.py`、`tests/test_slab_match.py`、`tests/test_target_management.py`。
- 新增服务端工具必须补 `ToolTrace`、成功/失败路径和拒绝路径测试。

## 14. 开发注意

- 不要绕过 `FlagValidator.is_valid_flag_value` 另写一套 Flag 语法。
- `flag.verify` 和候选提取共用同一校验入口，黑名单扩展只改 `FLAG_PREFIX_BLACKLIST`。
- 新增平台适配器时，必须同步写回 `competition_meta` 的多 Flag 进度字段。
- 释放靶机、恢复租约、删除项目这类操作要幂等，并在失败时记录事件而不是静默忽略。
- 修改数据库模型后，`db.init_db` 只做增量列/索引回填；真正的破坏性 schema 变更应提升 `SCHEMA_VERSION`。
- 模型输出字段由 `OUTPUT_SCHEMA` 严格控制，避免新增非声明字段进入 `ContextSnapshot`。

## 15. 2026-08 评测复盘与待修复项

### 15.1 TSecBench 平台任务终态

- TSecBench 的 `BENCHMARK_TOKEN` 对应一个平台跑分任务，`unique_code` 才是单题标识。
- 平台任务有总时限；任务到期后，剩余题目的 `POST /openapi/v1/challenges/start` 会返回任务级终态错误，
  Aurora 会将剩余 item 收敛为 `FAILED`，清除失效目标并停止继续派发。
- `start` 的 `invalid_state` 也可能表示三个实例额度已满，因此适配器会再调用题目列表确认：列表正常时进入
  `WAITING_RESOURCE`，列表同样返回任务终态时才结束整个任务。
- 这不是单题 `close` 后无法 `start` 的问题；Challenges API 允许 `start/submit/close` 对单题重复执行。

### 15.2 phase 环境释放过早

- 当前 `ChallengeGroupRunner._resolve_phase` 除 `FLAG_PARTIAL` 外都会释放环境，导致可重试 phase 失败后
  下一 phase 还要重新 `start`，增加平台状态抖动和调度延迟。
- 修复方向：只在题目真正终态时释放环境；`FAILED/TIMEOUT/CRASHED` 且仍有下一 phase 时保留环境。

### 15.3 `flag.verify` 入参契约

- 最新评测中大量 `flag.verify` 失败原因是模型把 `/workspace/work/...` 路径当成 `source_artifact_refs`，
  而服务端只接受当前项目的 `Artifact ID`。
- 修复方向：在工具描述和上下文里明确要求 `Artifact ID`；网关可增加 workspace 路径到 Artifact 的兼容解析，
  但兼容层不得放宽“仅当前项目可信 Artifact”这一门禁。

### 15.4 `flag.submit("latest_verified")` 门禁误杀

- `latest_verified` 当前只匹配同一 Attempt 内的 `LOCAL_VERIFIED` 候选。模型在 `flag.verify` 失败后仍调用
  `flag.submit("latest_verified")` 时，会得到 `execution_error`。
- 修复方向：没有 `LOCAL_VERIFIED` 候选时不要暴露 `latest_verified`；或让 `latest_verified` 回退到项目内最近
  一条 `LOCAL_VERIFIED` 候选，并返回明确的可用 `candidate_id`。

### 15.5 拒绝候选后的无进展空转

- 单一候选被平台拒绝且没有新候选时，`AutoRunnerService` 会继续派发新 Attempt，直到 phase 超时。
- 修复方向：检测“已拒绝且无新本地候选/无新证据”的阻塞态，提前结束当前 phase，释放并发槽位。

### 15.6 Resume manifest 完整性

- `codex_state_complete` 会因子目录中存在 SQLite WAL/SHM、锁文件等非续跑文件而误判为 false；
  `work_state_complete` 会因文件数超过 `AURORA_RESUME_MAX_FILES` 而拒绝恢复。
- 修复方向：`_is_resumable_codex_state` 排除临时/锁/WAL/SHM 文件；工作文件超限时允许恢复已保存文件，
  而不是整体拒绝 manifest。

## 16. Flag 前缀白名单配置与 API

`v1.6.2` 新增 flag 前缀白名单，用于过滤非目标前缀的伪 Flag。所有校验统一走
`aurora/services/flag_prefix_config.py`，不要再在调用点单独复制前缀判断。

### 16.1 环境变量

- `AURORA_FLAG_PREFIXES`：全局允许的 flag 前缀，逗号分隔；默认 `flag`。
- 启动时读取并写入 `Settings.flag_prefixes`，每个前缀会被去空格并转为小写。
- 匹配始终不区分大小写；提交时保留解出的原始大小写格式。
- 运行时可修改全局白名单，但环境变量设置会在下次启动时重新覆盖运行时修改。

### 16.2 全局前缀 API

- `GET /api/settings/flag-prefixes`
  - 返回 `prefixes`、`case_insensitive`、`submit_preserves_case`、`default`、`source`。
  - `source` 为 `environment`、`runtime` 或 `default`。
- `PUT /api/settings/flag-prefixes`
  - 请求体：`{"prefixes": ["flag", "DASCTF"]}`。
  - 更新 `Settings.flag_prefixes`，返回与 GET 相同结构。
  - 空列表或非法前缀（含空格、花括号，或以非字母/数字开头）返回 400。

### 16.3 题目组覆盖 API

- `GET /api/challenge-groups/{group_id}/flag-prefixes`
  - 返回该组的有效前缀、`overridden`、`override` 和 `global`。
  - 组不存在返回 404。
- `PUT /api/challenge-groups/{group_id}/flag-prefixes`
  - 请求体：`{"prefixes": ["DASCTF", "flag"]}`，写入 `ChallengeGroup.flag_prefixes`。
  - 传空列表 `[]` 会清除覆盖，恢复继承全局白名单。
  - 非法前缀返回 400，组不存在返回 400（组级查询 404）。

### 16.4 生效优先级

1. 项目属于某个 `ChallengeGroupItem` 且该组 `flag_prefixes` 非空：使用组级覆盖。
2. 否则使用 `Settings.flag_prefixes` 的全局白名单。
3. 全局白名单为空或未设置时，回退到默认 `["flag"]`。

Hands-free 导入确认时，前端会把用户输入的逗号分隔前缀写入 `HandsFreeConfirmRequest.flag_prefixes`，
`HandsFreeService.confirm` 归一化后保存到新建的 `ChallengeGroup.flag_prefixes`。
