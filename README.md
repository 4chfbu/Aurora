# Aurora v2 MVP

This repository starts the Aurora v2 implementation from the design document.

中文使用指南见 [`docs/USAGE.md`](docs/USAGE.md)；配置变量和部署矩阵见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。

The first milestone implements a minimal Fact-Intent blackboard loop with:

- SQLite-backed project, fact, intent, attempt, worker, artifact, and debug trace models.
- A local artifact store for raw evidence.
- A lease-based scheduler skeleton.
- A Kali-first capability gateway with a restricted `sandbox.exec` interface and semantic tool stubs.
- A Kali-first command runner that uses `kalilinux/kali-rolling` through Docker/Podman when the image is already available locally, with local fallback for development.
- A real Codex Harness runtime backed by a private CC Switch protocol-conversion service.
- FastAPI endpoints and a simple React/Vite UI scaffold.

## Backend

```bash
uv sync --extra dev
uv run uvicorn apps.api.main:app --reload
```

Configuration is loaded from `.env` if present. Copy the example and fill in runtime settings when needed:

```bash
cp .env.example .env
```

See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for the complete environment-variable reference, runtime matrix, and deployment notes.

Target runtime: Solver Worker runs inside Codex Harness:

```bash
AURORA_WORKER_RUNTIME=codex
AURORA_WORKER_IMAGE=aurora-kali-codex:latest
AURORA_CONTAINER_NETWORK=aurora-runtime
AURORA_CODEX_COMMAND_TEMPLATE=/workspace/runtime/codex-via-cc-switch.sh {prompt_filename} {output_schema_filename} {last_message_filename}
AURORA_CODEX_PROXY_BASE_URL=http://aurora-cc-switch:15723/v1
AURORA_CODEX_TIMEOUT_SECONDS=1800
```

Start the private CC Switch service and build the Kali/Codex Worker image with `./scripts/runtime-up.sh`. CC Switch converts Codex Responses requests to the configured upstream Chat Completions API. It is reachable only on the `aurora-runtime` Docker network and injects the real upstream key, so Worker containers receive only a placeholder credential. Aurora mounts only the current Worker workspace and saves Codex's stdout/stderr transcript as an Artifact.

Set `AURORA_CODEX_TIMEOUT_SECONDS=0` to disable the Codex Harness subprocess timeout. In that mode, long-running Solver Workers are only stopped by user action or external process/container control.

Available template variables:

- `{prompt_filename}`: prompt file name in the current Worker directory, usually `aurora-intent.md`.
- `{output_schema_filename}`: JSON Schema file passed to Codex `--output-schema`.
- `{last_message_filename}`: file used by Codex `--output-last-message`; Aurora parses this final answer.
- `{prompt_path}`: path relative to the Aurora workspace root.
- `{container_prompt_file}`: `/workspace/...` path when using the container mount.
- `{host_prompt_file}`: host absolute path, mainly for local debugging.
- `{llm_model}` / `{llm_model_shell}`: configured model name, raw or shell-quoted.
- `{llm_base_url}` / `{llm_base_url_shell}`: configured OpenAI-compatible base URL, raw or shell-quoted.

Codex uses `AURORA_CODEX_PROXY_BASE_URL`; CC Switch owns the upstream base URL and protocol conversion.

Check whether the configured provider is compatible with Codex CLI:

```bash
./scripts/check-codex-provider.sh
```

The check performs a real `/v1/responses` request through CC Switch. The configured upstream may expose only Chat Completions because CC Switch performs the conversion.

Debug-only direct OpenAI-compatible runtime:

```bash
AURORA_WORKER_RUNTIME=openai_direct
AURORA_LLM_BASE_URL=https://api.openai.com/v1
AURORA_LLM_API_KEY=sk-...
AURORA_LLM_MODEL=gpt-4.1-mini
```

`openai_direct` is not the final Solver boundary; it exists only for debugging prompt/context behavior without Codex. The intended production boundary is `codex`, where Codex runs inside the Kali Worker environment and owns the agent loop and Worker execution.

Or use the helper script:

```bash
./scripts/dev-api.sh
```

Useful endpoints:

- `GET /health`
- `POST /api/projects`
- `POST /api/projects/{project_id}/run-demo`
- `POST /api/projects/{project_id}/scheduler/run-next`
- `POST /api/projects/{project_id}/intents`
- `POST /api/projects/{project_id}/hints`
- `GET /api/projects/{project_id}/hints`
- `GET /api/projects/{project_id}/blackboard`
- `GET /api/projects/{project_id}/debug/context-snapshots`
- `GET /api/projects/{project_id}/debug/llm-traces`
- `GET /api/projects/{project_id}/debug/tool-traces`
- `GET /api/artifacts/{artifact_id}/content`
- `GET /api/projects/{project_id}/findings`
- `GET /api/projects/{project_id}/summary`
- `POST /api/projects/{project_id}/tools/{tool_name}/execute`
- `POST /api/projects/{project_id}/observer/run`
- `POST /api/projects/{project_id}/manager/run`
- `POST /api/projects/{project_id}/autorun/start`
- `POST /api/projects/{project_id}/autorun/step`
- `GET /api/projects/{project_id}/autorun/status`
- `POST /api/projects/solve`

## Kali Worker Tooling

The capability gateway prefers Kali native tooling over MCP. It currently maps these semantic tools to restricted shell commands:

- `network.scan` -> `nmap`
- `http.request` -> `curl`
- `web.enumerate` -> `ffuf` with `dirb` fallback
- `binary.inspect` -> `file`, `sha256sum`, `strings`
- `forensic.inspect` -> `file`, `exiftool`, `binwalk`

The Worker build produces two shared-layer profiles. `core` contains the common Web, network, crypto, reverse, pwn, and forensic CLI suite. `heavy` adds Ghidra, angr, Volatility3, jadx/apktool, and hashcat with a Mesa CPU OpenCL backend. Web challenges use `core`; all other or unknown challenge types use `heavy`. The selected profile and command manifest are included in each Solver context.

Stateful local analysis is exposed to Codex through stdio MCP servers: `aurora_reverse` maintains Rizin sessions and optionally uses Ghidra decompilation, `aurora_debug` maintains GDB/MI sessions, and `aurora_blackboard` reads and updates the scoped project state while the Worker is running. MCP calls are imported into Aurora as `ToolTrace` and Artifact records after the Worker exits.

## MCP Capabilities and FOFA

Aurora exposes its Worker-visible capabilities through a small MCP-compatible registry. `blackboard.query` is read-only and returns Artifact-backed results. Native Kali capabilities remain the preferred implementation for local execution.

Set both credentials below to expose `fofa.search` to Workers:

```bash
AURORA_FOFA_EMAIL=you@example.com
AURORA_FOFA_KEY=your-fofa-key
```

FOFA is deliberately restricted to a single `host="..."`, `domain="..."`, or `ip="..."` expression that matches the project's allowed host/domain scope. Its raw response is stored as an Artifact and its request is audited like every other tool call.

## Same-Container Subagents

Subagents are disabled by default. Enable the global guard and then opt in per project with `"subagents_enabled": true` when creating or solving a project:

```bash
AURORA_SUBAGENTS_ENABLED=true
AURORA_SUBAGENTS_MAX_CONCURRENT=2
AURORA_SUBAGENTS_MAX_PER_WORKER=4
```

An enabled Solver may start a non-recursive child through the local `subagent.spawn` capability. The child runs as a Codex subprocess in the same Worker container and shared `/workspace`, while receiving a separate minimal context. There is no independent subagent timeout: child processes end only when they complete or when their parent Worker, project, or container is stopped. Each child transcript and structured result is imported as its own Worker, Attempt, Trace, Event, and Artifact record.

The bundled `codex-via-cc-switch.sh` wrapper makes children reuse the private CC Switch proxy. With another wrapper, set `AURORA_SUBAGENT_CODEX_COMMAND` to an equivalent command template containing `{prompt_filename}`, `{schema_filename}`, and `{last_message_filename}`.

## Prompt Assets and Intent DSL

Prompt templates live in `aurora/prompts/`; the `solver.general` profile renders them at runtime and records the resulting prompt hash. `aurora.services.intent_dsl.IntentDSL` is the typed, declarative contract for an Intent. It validates objectives, capabilities, dependencies, risk and tool request data; it is not executable code.

The runner only uses Docker/Podman if the official image is already present locally:

```bash
docker pull kalilinux/kali-rolling
```

If Docker/Podman or the image is unavailable, commands fall back to local execution and the actual backend is recorded in `ToolTrace` and Artifact content.

Raw stdout/stderr is stored in Artifact files. The model-facing context and UI debug panel only receive summaries and artifact references unless an artifact is explicitly opened.

Only trusted challenge/target artifacts and successful `flag.verify` replay artifacts are scanned for flag candidates. A locally verified candidate creates a `Finding`, emits `finding.flag_candidate`, and moves the project to `FLAG_READY`; only platform or manual acceptance marks it `COMPLETED`.

When a project is completed, remaining pending Intents are cancelled and `scheduler/run-next` returns a `project_completed` no-op response instead of claiming more work.

Container execution uses Docker/Podman `bridge` networking by default so authorized CTF targets can be reached. Override with:

```bash
AURORA_CONTAINER_NETWORK=none uv run uvicorn apps.api.main:app --reload
```

## Authorization Policy

Semantic tools are checked against `AuthorizationScope` before command generation:

- `http.request` and `web.enumerate` validate URL hostnames.
- `network.scan` validates the target host.
- Metadata and management targets such as `169.254.169.254` are denied by default.
- `sandbox.exec` is still available as a restricted escape hatch, with command-level deny rules and full audit logging.

Manual tool execution is available in the Web UI and API. Example:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/tools/http.request/execute \
  -H 'Content-Type: application/json' \
  -d '{"request":{"url":"http://127.0.0.1/","timeout_seconds":3}}'
```

## Runnable Intents

The scheduler can execute the highest-priority pending Intent through:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/scheduler/run-next
```

For the MVP, executable tool input is stored in `Intent.budget.tool_request` via the create Intent API:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/intents \
  -H 'Content-Type: application/json' \
  -d '{
    "objective":"Request the authorized local HTTP endpoint",
    "capability_tags":["http.request"],
    "priority":2,
    "risk_level":"low",
    "tool_request":{"url":"http://127.0.0.1/","timeout_seconds":3}
  }'
```

Runtime output selects authorized tools from the context and routes requests through the same `CapabilityGateway`, `PolicyEngine`, `ToolTrace`, and `ArtifactStore` path as manual execution.

## Auto Run

Use `开始自动解题` in the Web UI or call:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/autorun/start \
  -H 'Content-Type: application/json' \
  -d '{"max_iterations":20,"max_minutes":0,"no_progress_limit":4,"stop_on_observer_escalate":true}'
```

The loop runs Observer, Manager, Scheduler, and Codex Worker repeatedly until the project completes, hits a limit, has no progress, or Observer escalates. Observer `ESCALATE` stops the run and waits for human review.
Set `max_minutes` to `0` to disable wall-clock autorun timeout.

## Observer

The MVP includes a deterministic event-driven Observer. It does not call an LLM yet; it inspects recent ToolTrace and Attempt records, then writes an `observer.decision` event.

Current decisions:

- `ESCALATE` when a recent tool request was denied by policy.
- `REDIRECT` when the two most recent tool calls are identical.
- `REQUEST_EVIDENCE` when a recent failed/partial Attempt has no artifact evidence.
- `CONTINUE` when no immediate issue is detected.

Run it from the Web UI or API:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/observer/run
```

## Manager

The MVP includes a deterministic Manager/Reasoner. It does not call an LLM yet; it reads unconsumed `Hint` records and proposes deduplicated Intents through the blackboard repository.

Current behavior:

- URL hints create `http.request` Intents with `tool_request.url` populated.
- Non-URL hints create low-risk `sandbox.exec` review Intents.
- If no hints exist and no runnable Intent is present, it creates a low-priority blackboard review Intent.
- Decisions are written as `manager.decision` events.

Run it from the Web UI or API:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/manager/run
```

Hints can be injected after project creation:

```bash
curl -X POST http://localhost:8000/api/projects/<project_id>/hints \
  -H 'Content-Type: application/json' \
  -d '{"content":"Try http://127.0.0.1/ first.","source":"user"}'

curl -X POST http://localhost:8000/api/projects/<project_id>/manager/run
```

The Web UI has an `Inject Hint` panel and shows whether hints are active or consumed.

## Web UI

```bash
cd apps/web
npm install
npm run dev
```

Set `VITE_API_BASE` if the API is not served from `http://localhost:8000`.

To build the UI and let FastAPI serve it from `/`:

```bash
./scripts/build-web.sh
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
```

Run the full local verification suite:

```bash
./scripts/check.sh
```
