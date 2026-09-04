# Aurora MVP Acceptance Checklist

## Start

```bash
./scripts/check.sh
./scripts/build-web.sh
cd apps/web
node_modules/.bin/tsc --noEmit -p tsconfig.json
npm audit --audit-level=moderate
cd ../..
./scripts/runtime-up.sh
./scripts/verify-worker-image.sh
./scripts/check-codex-provider.sh
./scripts/dev-api.sh
```

For real Codex Harness testing, create `.env` first:

```bash
cp .env.example .env
```

Then set:

```bash
AURORA_WORKER_RUNTIME=codex
AURORA_WORKER_IMAGE=aurora-kali-codex:latest
AURORA_CONTAINER_NETWORK=aurora-runtime
AURORA_CODEX_COMMAND_TEMPLATE=/workspace/runtime/codex-via-cc-switch.sh {prompt_filename} {output_schema_filename} {last_message_filename}
AURORA_CODEX_PROXY_BASE_URL=http://aurora-cc-switch:15723/v1
AURORA_CODEX_TIMEOUT_SECONDS=1800
```

`runtime-up.sh` starts the private CC Switch service and builds the Codex Worker image. The runtime disables local fallback: a missing image, proxy, model, or credential fails explicitly. `openai_direct` remains an explicit debugging mode only.
It also builds the optional `aurora-openvpn:latest` gateway. Without an uploaded and manually connected profile, Worker networking remains unchanged. For VPN acceptance, verify the host routes and DNS are unchanged, configured CIDRs resolve through `tun0` inside the gateway, and a disconnected unhealthy gateway prevents new Solver Workers.

Restart the API after editing `.env`.

The current `PolicyEngine` allows every gateway request. `AuthorizationScope` is compatibility/context data, not an enforcement boundary, and the native Codex shell is also not target-scoped. Perform acceptance only in an externally restricted Worker network/VPN/proxy and against targets you are authorized to test. The API also has open CORS and no built-in authentication, so do not expose it directly to an untrusted network.

Open the Web UI at `http://localhost:8000` after `build-web.sh`, or run the Vite dev server from `apps/web`.

## Core Flow

1. Create a project.
2. Inject a Hint such as `Try http://127.0.0.1/ first.`.
3. Run Manager.
4. Confirm a deduplicated `http.request` Intent is created.
5. Run Next Intent.
6. Inspect Context Snapshot, LLM Trace, ToolTrace, Artifact, and Event Timeline.
7. Run Observer and verify an `observer.decision` event appears.

Alternatively, click `开始自动解题` to run Manager, Scheduler, Worker, and Observer in a loop until completion or escalation.

## Completion Flow

First run the deterministic verification/submission tests:

```bash
uv run --extra dev pytest tests/test_flag_verification.py tests/test_flag_submission.py
```

For an end-to-end Worker run, use a challenge with a trusted input Artifact and instruct the Solver to derive the answer with a Python script. Expected result:

- `flag.verify` references only same-project source Artifacts and a Worker workspace Python script. The script receives `inputs/manifest.json`, reads the listed paths, outputs exactly one computed flag, and does not contain the candidate value.
- Two isolated, networkless replays return the same value; a successful replay Artifact and a `LOCAL_VERIFIED` candidate are created. A Finding is created and project status becomes `FLAG_READY`.
- A managed competition Worker submits only `candidate_id`, or uses `candidate_id="latest_verified"` immediately after `flag.verify` in the same tool batch. Raw flag fields, stale/cross-project candidates, and a second submission of the same candidate are rejected.
- A platform rejection is preserved in `flag_validation_feedback` for the next turn. An unavailable adapter moves the item to `AWAITING_MANUAL_VALIDATION`. For Slab Match, verify an explicit brace-only rule submits only the inner payload and the default submits the full flag.
- Accept the candidate through `POST /api/projects/<project_id>/flag-candidates/<candidate_id>/validation` with `{"accepted": true}` when no competition adapter is available.
- Platform or manual acceptance changes a single-flag project to `COMPLETED`; a partially accepted multi-flag item remains active. On completion, remaining pending Intents are cancelled and additional Hint/Intent creation returns `409 Conflict`.
- `scheduler/run-next` returns `project_completed` after final acceptance.

Plain model text, a transcript, Blackboard summary, or `sandbox.exec` output alone must not create a trusted flag candidate.

## Review API

Use one endpoint for final inspection:

```bash
curl http://localhost:8000/api/projects/<project_id>/summary
```

The summary contains project status, counts, findings, recent facts, intent/attempt status counts, recent artifacts, traces, latest context snapshot, and recent events.

可靠性闸门可单独检查：

```bash
curl http://localhost:8000/api/projects/<project_id>/reliability
```

超时或动作预算触发后，终止 Attempt 必须有 checkpoint 或明确的 `checkpoint.failed`；完整性校验失败的恢复必须记录 `codex.resume_rejected` 并开启新 thread。

## Frozen Evaluation

配置 TSecBench 后，通过 `POST /api/evaluations/suites` 冻结尚未完成的互联网题目清单，再通过
`POST /api/evaluations/suites/{suite_id}/runs` 分别创建 `baseline` 和 `candidate` 运行。运行报告以平台确认的
完整解题为成功条件；`GET /api/evaluations/comparisons` 只有在同一冻结集上成功率提升至少 15 个百分点且
错误候选率不升高时才返回 `promoted=true`。默认单题最多执行 12/25/40 分钟三个阶段，共 77 分钟；P1 单 Agent，P2/P3 可多 Agent，hint 仅在 P3 获取。
同一套件禁止重叠运行；创建下一次运行前，平台会话必须没有已接受进度。当前不运行含远程附件但尚未物化为
Artifact 的套件，避免 baseline/candidate 输入不一致。少于 30 个有效题目时仍返回 95% Wilson 置信区间，
但 `promotion_eligible=false`，不会自动晋级。

Do not run baseline and candidate sequentially with the same platform Token/account after baseline has accepted any flag. Use independent clean sessions, or reset all accepted progress on the platform before creating the candidate run. Promotion also requires the repeated-request gate, 100% derived-candidate verification coverage, and 100% terminal-checkpoint coverage.
