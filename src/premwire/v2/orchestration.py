from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .models import LlmTaskType
from .stage_c_repository import StageCRepository


T = TypeVar("T", bound=BaseModel)


class JsonLlmClient(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        seed: int | None,
        timeout: int,
    ) -> str: ...


class OllamaJsonClient:
    def __init__(self, *, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def generate(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        seed: int | None,
        timeout: int,
    ) -> str:
        options: dict[str, Any] = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(
                {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "format": schema,
                    "options": options,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        from ..http_transport import urlopen
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["response"])


class LlmOutputError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LlmResult:
    value: BaseModel
    run_ids: tuple[int, ...]
    repaired: bool


class StructuredLlmOrchestrator:
    prompt_version = "stage-c-1.0"
    schema_version = "1.0"

    def __init__(
        self,
        *,
        client: JsonLlmClient,
        repository: StageCRepository,
        model_name: str = "qwen3.5:4b",
        temperature: float = 0.0,
        seed: int | None = 42,
        timeout: int = 240,
    ) -> None:
        self.client = client
        self.repository = repository
        self.model_name = model_name
        self.temperature = temperature
        self.seed = seed
        self.timeout = timeout

    def run_json(
        self,
        *,
        task_type: LlmTaskType,
        story_id: str,
        story_version: int,
        prompt: str,
        contract: type[T],
    ) -> LlmResult:
        schema = contract.model_json_schema()
        run_ids: list[int] = []
        raw = self._call(
            story_id=story_id,
            story_version=story_version,
            task_type=task_type,
            prompt=prompt,
            schema=schema,
            attempt=1,
            run_ids=run_ids,
        )
        try:
            parsed = self._parse(raw, contract)
            self._mark_last_run_valid(run_ids[-1], parsed)
            return LlmResult(parsed, tuple(run_ids), False)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            self._mark_last_run_invalid(run_ids[-1], type(exc).__name__)

        repair_prompt = (
            "Return only corrected JSON matching the supplied schema. "
            "Do not add, remove, reinterpret or improve facts. "
            "Original invalid response:\n" + raw
        )
        repaired_raw = self._call(
            story_id=story_id,
            story_version=story_version,
            task_type=LlmTaskType.JSON_REPAIR,
            prompt=repair_prompt,
            schema=schema,
            attempt=2,
            run_ids=run_ids,
        )
        try:
            parsed = self._parse(repaired_raw, contract)
            self._mark_last_run_valid(run_ids[-1], parsed)
            return LlmResult(parsed, tuple(run_ids), True)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            self._mark_last_run_invalid(run_ids[-1], type(exc).__name__)
            raise LlmOutputError("BLOCKED_LLM_INVALID_OUTPUT") from exc

    def _call(
        self,
        *,
        story_id: str,
        story_version: int,
        task_type: LlmTaskType,
        prompt: str,
        schema: dict[str, Any],
        attempt: int,
        run_ids: list[int],
    ) -> str:
        started = time.perf_counter()
        raw = self.client.generate(
            prompt=prompt,
            schema=schema,
            temperature=self.temperature,
            seed=self.seed,
            timeout=self.timeout,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        run_id = self.repository.save_llm_run(
            {
                "story_id": story_id,
                "story_version": story_version,
                "task_type": task_type.value,
                "model_name": self.model_name,
                "prompt_version": self.prompt_version,
                "schema_version": self.schema_version,
                "temperature": self.temperature,
                "seed": self.seed,
                "input_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "raw_response": raw,
                "parsed_response_json": None,
                "validation_status": "PENDING",
                "error_code": None,
                "duration_ms": duration_ms,
                "attempt_number": attempt,
            }
        )
        run_ids.append(run_id)
        return raw

    @staticmethod
    def _parse(raw: str, contract: type[T]) -> T:
        if raw.lstrip().startswith("```"):
            raise ValueError("markdown fences are forbidden")
        return contract.model_validate(json.loads(raw))

    def _mark_last_run_valid(self, run_id: int, parsed: BaseModel) -> None:
        self.repository.db.execute(
            """
            UPDATE llm_runs SET validation_status='VALID',
                parsed_response_json=?, error_code=NULL WHERE id=?
            """,
            (parsed.model_dump_json(), run_id),
        )

    def _mark_last_run_invalid(self, run_id: int, error_code: str) -> None:
        self.repository.db.execute(
            "UPDATE llm_runs SET validation_status='INVALID', error_code=? WHERE id=?",
            (error_code, run_id),
        )
