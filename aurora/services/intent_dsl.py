from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class IntentDSL(BaseModel):
    """Declarative, validated task contract. It is data, never executable code."""

    objective: str = Field(min_length=1, max_length=2_000)
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    depends_on_facts: list[str] = Field(default_factory=list, max_length=32)
    priority: float = Field(default=1.0, ge=0, le=100)
    risk_level: Literal["low", "medium", "high"] = "low"
    tool_request: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must be unique")
        return value
