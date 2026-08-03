#!/usr/bin/env python3
"""Run a structured, non-recursive Codex child in the current Worker container."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import shlex
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid request JSON: {exc}")
    objective = str(request.get("objective", "")).strip()
    if not objective or len(objective) > 2000:
        raise SystemExit("objective must be 1..2000 characters")
    cwd = Path.cwd()
    context_path = cwd / "subagent-context.json"
    if not context_path.exists():
        raise SystemExit("subagent context is unavailable in this Worker directory")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    if not context.get("subagents_enabled") or os.getenv("AURORA_SUBAGENTS_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        raise SystemExit("same-container subagents are disabled for this project")
    allowed = {tool["name"] for tool in context.get("visible_tools", []) if tool.get("name") != "subagent.spawn"}
    tags = request.get("capability_tags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) or tag not in allowed for tag in tags):
        raise SystemExit("capability_tags must be a subset of the parent visible tools")
    root = cwd / "subagents"
    root.mkdir(exist_ok=True)
    manifest = root / "manifest.jsonl"
    state_file = root / ".state.json"
    with state_file.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lock.seek(0)
        try:
            state = json.load(lock)
        except json.JSONDecodeError:
            state = {"active": 0, "total": 0}
        if state.get("total", 0) >= int(os.getenv("AURORA_SUBAGENTS_MAX_PER_WORKER", "4")):
            raise SystemExit("subagent limit reached")
        if state.get("active", 0) >= int(os.getenv("AURORA_SUBAGENTS_MAX_CONCURRENT", "2")):
            raise SystemExit("subagent concurrent limit reached")
        state["active"] = state.get("active", 0) + 1
        state["total"] = state.get("total", 0) + 1
        lock.seek(0)
        lock.truncate()
        json.dump(state, lock)
        lock.flush()
    run_id = f"subagent_{secrets.token_hex(8)}"
    run_dir = root / run_id
    run_dir.mkdir()
    child_context = {"project_goal": context.get("project_goal"), "parent_intent": context.get("current_intent"), "objective": objective, "facts": context.get("facts", []), "artifact_summaries": context.get("artifact_summaries", []), "authorization_scope": context.get("authorization_scope"), "visible_tools": [tool for tool in context.get("visible_tools", []) if tool.get("name") != "subagent.spawn"]}
    prompt = "You are a non-recursive Aurora Solver subagent. Work only on the assigned objective and authorization scope. Do not create subagents. Return strict JSON only.\n\n" + json.dumps(child_context, ensure_ascii=False, indent=2)
    prompt_file, schema_file, output_file = run_dir / "prompt.md", run_dir / "output-schema.json", run_dir / "last-message.json"
    prompt_file.write_text(prompt, encoding="utf-8")
    schema_file.write_text(json.dumps(context.get("output_schema", {}), ensure_ascii=False), encoding="utf-8")
    command = os.getenv("AURORA_SUBAGENT_CODEX_COMMAND", "codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --output-schema {schema_filename} --output-last-message {last_message_filename} - < {prompt_filename}")
    command = command.format(prompt_filename=shlex.quote(prompt_file.name), schema_filename=shlex.quote(schema_file.name), last_message_filename=shlex.quote(output_file.name))
    try:
        completed = subprocess.run(command, shell=True, cwd=run_dir, text=True, capture_output=True, check=False)
    finally:
        with state_file.open("r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = json.load(lock)
            except json.JSONDecodeError:
                state = {"active": 1, "total": 1}
            state["active"] = max(0, state.get("active", 1) - 1)
            lock.seek(0)
            lock.truncate()
            json.dump(state, lock)
            lock.flush()
    try:
        output = json.loads(output_file.read_text(encoding="utf-8")) if output_file.exists() else json.loads(completed.stdout)
    except (OSError, json.JSONDecodeError):
        output = {"status": "failed", "summary": "Subagent did not return strict JSON.", "decision_summary": {"selected_intent": objective, "reason_summary": "Missing structured output.", "next_tool_plan": []}}
    record = {"run_id": run_id, "objective": objective, "capability_tags": tags, "exit_code": completed.returncode, "output": output, "prompt": prompt, "transcript": f"[stdout]\\n{completed.stdout}\\n\\n[stderr]\\n{completed.stderr}"}
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({"run_id": run_id, "status": output.get("status", "failed"), "summary": output.get("summary", "")}))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
