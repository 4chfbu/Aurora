from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aurora.models import ContextSnapshot, Worker
from aurora.services.agent_profiles import get_agent_profile


class PromptRenderer:
    def __init__(self, prompt_dir: Path | None = None) -> None:
        self.prompt_dir = prompt_dir or Path(__file__).resolve().parents[1] / "prompts"

    def render_messages(self, *, worker: Worker, snapshot: ContextSnapshot) -> list[dict[str, str]]:
        profile = get_agent_profile(worker.agent_profile_id)
        if profile.role != "solver" or not profile.developer_template:
            raise ValueError(f"profile does not support solver messages: {profile.id}")
        system = self._read(profile.system_template)
        developer = self._render(
            self._read(profile.developer_template),
            output_schema=self._json(snapshot.output_schema_json),
            visible_tools=self._json(snapshot.visible_tools_json),
        )
        user = self._json(
            {
                "context_snapshot_id": snapshot.id,
                "context": snapshot.sections_json,
                "section_metrics": snapshot.section_metrics_json,
            }
        )
        return [
            {"role": "system", "content": system},
            {"role": "developer", "content": developer},
            {"role": "user", "content": user},
        ]

    def read_prompt(self, name: str) -> str:
        return self._read(name)

    def render_codex_task(self, *, worker: Worker, snapshot: ContextSnapshot) -> str:
        profile = get_agent_profile(worker.agent_profile_id)
        if profile.role != "solver" or not profile.developer_template or not profile.codex_template:
            raise ValueError(f"profile does not support Codex tasks: {profile.id}")
        system = self._read(profile.system_template)
        developer = self._render(
            self._read(profile.developer_template),
            output_schema=self._json(snapshot.output_schema_json),
            visible_tools=self._json(snapshot.visible_tools_json),
        )
        payload = self._json(
            {
                "context_snapshot_id": snapshot.id,
                "context": snapshot.sections_json,
            },
        )
        return self._render(
            self._read(profile.codex_template),
            system_prompt=system,
            developer_prompt=developer,
            context_payload=payload,
        )

    def version_hash(self, *, worker: Worker) -> str:
        profile = get_agent_profile(worker.agent_profile_id)
        names = [profile.system_template, profile.developer_template, profile.codex_template]
        content = "\n".join(self._read(name) for name in names if name)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _read(self, name: str) -> str:
        return (self.prompt_dir / name).read_text(encoding="utf-8").strip()

    def _render(self, template: str, **values: str) -> str:
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", value)
        return template

    def _json(self, value: Any, indent: int | None = None) -> str:
        return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True)
