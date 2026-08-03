# Aurora MVP Acceptance Checklist

## Start

```bash
./scripts/check.sh
./scripts/build-web.sh
./scripts/runtime-up.sh
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

Restart the API after editing `.env`.

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

Create a runnable `sandbox.exec` Intent:

```json
{
  "objective": "Emit a candidate flag.",
  "capability_tags": ["sandbox.exec"],
  "priority": 10,
  "risk_level": "low",
  "tool_request": {
    "command": "printf 'flag{acceptance_demo}'",
    "cwd": ".",
    "timeout_seconds": 5
  }
}
```

Then run Scheduler. Expected result:

- A Finding is created.
- Project status becomes `COMPLETED`.
- Remaining pending Intents are cancelled.
- Additional Hint/Intent creation returns `409 Conflict`.
- `scheduler/run-next` returns `project_completed`.

## Review API

Use one endpoint for final inspection:

```bash
curl http://localhost:8000/api/projects/<project_id>/summary
```

The summary contains project status, counts, findings, recent facts, intent/attempt status counts, recent artifacts, traces, latest context snapshot, and recent events.
