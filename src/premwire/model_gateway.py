from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .model_runtime import ModelRuntimeProfile, ModelTaskProfile


class ModelTaskType(str, Enum):
    COPY_GENERATION = "copy_generation"
    HEADLINE_GENERATION = "headline_generation"
    TRANSLATION = "translation"
    STRUCTURED_VALIDATION = "structured_validation"
    VISION_VALIDATION = "vision_validation"


class ModelFailureCode(str, Enum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    DISABLED_CAPABILITY = "disabled_capability"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class ModelRequest:
    task_type: ModelTaskType
    prompt: str
    schema: Mapping[str, Any]
    factual_contract: Mapping[str, Any]
    task_profile: ModelTaskProfile

    @classmethod
    def create(
        cls,
        *,
        task_type: ModelTaskType,
        prompt: str,
        schema: Mapping[str, Any],
        factual_contract: Mapping[str, Any],
        task_profile: ModelTaskProfile,
    ) -> "ModelRequest":
        return cls(
            task_type=task_type,
            prompt=prompt,
            schema=_deep_freeze(json.loads(json.dumps(schema, ensure_ascii=False))),
            factual_contract=_deep_freeze(
                json.loads(json.dumps(factual_contract, ensure_ascii=False))
            ),
            task_profile=task_profile,
        )


@dataclass(frozen=True, slots=True)
class ModelSuccess:
    payload: Mapping[str, Any]
    raw_response: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class ModelFailure:
    code: ModelFailureCode
    message: str
    retryable: bool
    latency_ms: float = 0.0


ModelResult = ModelSuccess | ModelFailure


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _jsonable(value: Any) -> Any:
    """Thaw immutable request data only at the provider transport boundary."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


class CopyGenerationPort(Protocol):
    def generate_copy(self, request: ModelRequest) -> ModelResult: ...


class HeadlineGenerationPort(Protocol):
    def generate_headline(self, request: ModelRequest) -> ModelResult: ...


class TranslationPort(Protocol):
    def translate(self, request: ModelRequest) -> ModelResult: ...


class VisionValidationPort(Protocol):
    def validate_vision(self, request: ModelRequest) -> ModelResult: ...


class ModelGateway(
    CopyGenerationPort,
    HeadlineGenerationPort,
    TranslationPort,
    VisionValidationPort,
    Protocol,
):
    pass


class OllamaModelGateway:
    """Ollama-specific transport. Its wire JSON does not escape this adapter."""

    def __init__(self, profile: ModelRuntimeProfile) -> None:
        if profile.provider != "ollama":
            raise ValueError("OllamaModelGateway requires an ollama profile")
        if not profile.base_url:
            raise ValueError("ollama base_url is required")
        self.profile = profile

    def _execute(self, request: ModelRequest) -> ModelResult:
        import time

        started = time.perf_counter()
        body = {
            "model": self.profile.model,
            "prompt": request.prompt,
            "stream": False,
            "think": False,
            "format": _jsonable(request.schema),
            "options": {
                "temperature": request.task_profile.temperature,
                "num_ctx": self.profile.context_length,
            },
        }
        transport = urllib.request.Request(
            f"{self.profile.base_url.rstrip('/')}/api/generate",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            from .http_transport import urlopen
            with urlopen(
                transport, timeout=self.profile.timeout_seconds
            ) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            raw = str(envelope["response"])
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("structured response must be an object")
            return ModelSuccess(
                payload=MappingProxyType(parsed),
                raw_response=raw,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except (TimeoutError, socket.timeout) as exc:
            return ModelFailure(
                ModelFailureCode.TIMEOUT,
                type(exc).__name__,
                True,
                (time.perf_counter() - started) * 1000,
            )
        except (urllib.error.URLError, ConnectionError) as exc:
            return ModelFailure(
                ModelFailureCode.UNAVAILABLE,
                type(exc).__name__,
                True,
                (time.perf_counter() - started) * 1000,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return ModelFailure(
                ModelFailureCode.INVALID_JSON,
                type(exc).__name__,
                False,
                (time.perf_counter() - started) * 1000,
            )
        except OSError as exc:
            return ModelFailure(
                ModelFailureCode.PROVIDER_ERROR,
                type(exc).__name__,
                True,
                (time.perf_counter() - started) * 1000,
            )

    def generate_copy(self, request: ModelRequest) -> ModelResult:
        return self._execute(request)

    def generate_headline(self, request: ModelRequest) -> ModelResult:
        return self._execute(request)

    def translate(self, request: ModelRequest) -> ModelResult:
        return self._execute(request)

    def validate_vision(self, request: ModelRequest) -> ModelResult:
        if not self.profile.vision_enabled:
            return ModelFailure(
                ModelFailureCode.DISABLED_CAPABILITY,
                "vision remains disabled until a separate Stage D gate",
                False,
            )
        return self._execute(request)


class MockModelGateway:
    def __init__(
        self,
        *,
        mode: str = "success",
        response: Mapping[str, Any] | None = None,
    ) -> None:
        self.mode = mode
        self.response = dict(response or {"text": "mock response"})

    def _result(self) -> ModelResult:
        if self.mode == "timeout":
            return ModelFailure(ModelFailureCode.TIMEOUT, "mock timeout", True)
        if self.mode == "unavailable":
            return ModelFailure(ModelFailureCode.UNAVAILABLE, "mock unavailable", True)
        if self.mode == "invalid_json":
            return ModelFailure(ModelFailureCode.INVALID_JSON, "mock invalid JSON", False)
        if self.mode != "success":
            return ModelFailure(ModelFailureCode.PROVIDER_ERROR, "unknown mock mode", False)
        raw = json.dumps(self.response, ensure_ascii=False)
        return ModelSuccess(MappingProxyType(dict(self.response)), raw, 0.0)

    def generate_copy(self, request: ModelRequest) -> ModelResult:
        return self._result()

    def generate_headline(self, request: ModelRequest) -> ModelResult:
        return self._result()

    def translate(self, request: ModelRequest) -> ModelResult:
        return self._result()

    def validate_vision(self, request: ModelRequest) -> ModelResult:
        return ModelFailure(
            ModelFailureCode.DISABLED_CAPABILITY,
            "vision is disabled in test_mock",
            False,
        )


def create_model_gateway(
    profile: ModelRuntimeProfile,
    *,
    mock_mode: str = "success",
    mock_response: Mapping[str, Any] | None = None,
) -> ModelGateway:
    if profile.provider == "mock":
        return MockModelGateway(mode=mock_mode, response=mock_response)
    if profile.provider == "ollama":
        return OllamaModelGateway(profile)
    raise ValueError(f"unsupported model provider: {profile.provider}")


__all__ = [
    "CopyGenerationPort",
    "HeadlineGenerationPort",
    "MockModelGateway",
    "ModelFailure",
    "ModelFailureCode",
    "ModelGateway",
    "ModelRequest",
    "ModelResult",
    "ModelSuccess",
    "ModelTaskType",
    "OllamaModelGateway",
    "TranslationPort",
    "VisionValidationPort",
    "create_model_gateway",
]
