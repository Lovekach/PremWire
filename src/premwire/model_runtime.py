from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    supports_structured_output: bool
    supports_json_schema: bool
    supports_tool_calls: bool
    context_window: int
    recommended_parallelism: int
    runtime_provider: str
    model_role: str


@dataclass(frozen=True, slots=True)
class ModelRuntimeProfile:
    name: str
    provider: str
    model: str
    base_url: str | None
    context_length: int
    timeout_seconds: int
    max_parallel_requests: int
    max_loaded_models: int
    runtime_root: Path
    production_auto_publish: bool
    capabilities: ModelCapabilities


@dataclass(frozen=True, slots=True)
class ModelTaskProfile:
    name: str
    temperature: float
    max_output_chars: int


def _positive_int(value: Any, field: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{field} must be positive")
    return parsed


def load_runtime_profile(
    project_root: Path,
    profile_name: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> ModelRuntimeProfile:
    env = os.environ if environ is None else environ
    payload = json.loads(
        (project_root / "config" / "model_runtime_profiles.json").read_text(
            encoding="utf-8"
        )
    )
    name = profile_name or env.get("PREMWIRE_MODEL_PROFILE") or payload["active_profile"]
    if name not in payload["profiles"]:
        raise ValueError(f"unknown model runtime profile: {name}")
    raw = payload["profiles"][name]
    provider = str(raw["provider"])
    model = str(env.get(str(raw.get("model_env") or ""), raw["model"])).strip()
    if not model:
        raise ValueError("model name must come from profile or configured environment")
    runtime_value = env.get(str(raw["runtime_root_env"]))
    runtime_root = Path(runtime_value or str(raw["runtime_root_default"]))
    if not runtime_root.is_absolute():
        runtime_root = project_root / runtime_root
    caps = raw["capabilities"]
    capabilities = ModelCapabilities(
        supports_structured_output=bool(caps["supports_structured_output"]),
        supports_json_schema=bool(caps["supports_json_schema"]),
        supports_tool_calls=bool(caps["supports_tool_calls"]),
        context_window=_positive_int(caps["context_window"], "context_window"),
        recommended_parallelism=_positive_int(
            caps["recommended_parallelism"], "recommended_parallelism"
        ),
        runtime_provider=str(caps["runtime_provider"]),
        model_role=str(caps["model_role"]),
    )
    profile = ModelRuntimeProfile(
        name=name,
        provider=provider,
        model=model,
        base_url=str(raw["base_url"]) if raw.get("base_url") else None,
        context_length=_positive_int(raw["context_length"], "context_length"),
        timeout_seconds=_positive_int(raw["timeout_seconds"], "timeout_seconds"),
        max_parallel_requests=_positive_int(
            raw["max_parallel_requests"], "max_parallel_requests"
        ),
        max_loaded_models=_positive_int(raw["max_loaded_models"], "max_loaded_models"),
        runtime_root=runtime_root.resolve(),
        production_auto_publish=bool(raw["production_auto_publish"]),
        capabilities=capabilities,
    )
    if profile.context_length > profile.capabilities.context_window:
        raise ValueError("context_length exceeds configured capability")
    if profile.max_parallel_requests > profile.capabilities.recommended_parallelism:
        raise ValueError("parallelism exceeds configured safe capability")
    if profile.production_auto_publish:
        raise ValueError("model profile must not enable production publishing")
    return profile


def load_task_profiles(project_root: Path) -> dict[str, ModelTaskProfile]:
    payload = json.loads(
        (project_root / "config" / "model_task_profiles.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        name: ModelTaskProfile(
            name=name,
            temperature=float(raw["temperature"]),
            max_output_chars=_positive_int(raw["max_output_chars"], "max_output_chars"),
        )
        for name, raw in payload["tasks"].items()
    }


__all__ = [
    "ModelCapabilities",
    "ModelRuntimeProfile",
    "ModelTaskProfile",
    "load_runtime_profile",
    "load_task_profiles",
]
