from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model_gateway import (
    MockModelGateway,
    ModelFailure,
    ModelFailureCode,
    ModelGateway,
    ModelRequest,
    ModelSuccess,
    ModelTaskType,
)
from .model_runtime import ModelTaskProfile


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    valid_structured_output: bool
    hallucinated_entity: bool
    hallucinated_amount: bool
    hallucinated_contract: bool
    strengthened_status: bool
    lost_negation: bool
    unsupported_factual_addition: bool
    factual_omission: bool
    language_ok: bool
    schema_ok: bool
    length_chars: int
    accepted: bool


_STATUS_STRENGTH = {
    "unknown": 0,
    "interest": 1,
    "reported": 2,
    "confirmed_report": 3,
    "official": 4,
}


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list | tuple):
        return set()
    return {str(item) for item in value}


def _language_ok(text: str, language: str) -> bool:
    cyrillic = any("а" <= char.lower() <= "я" or char.lower() == "ё" for char in text)
    latin = any("a" <= char.lower() <= "z" for char in text)
    if language == "ru":
        return cyrillic
    if language == "en":
        return latin and not cyrillic
    return True


def assess_model_payload(
    payload: Mapping[str, Any],
    factual_contract: Mapping[str, Any],
    *,
    expected_language: str,
    max_output_chars: int,
) -> SafetyAssessment:
    required = {"text", "entities", "amounts", "contracts", "status", "negation"}
    schema_ok = required.issubset(payload) and isinstance(payload.get("text"), str)
    text = str(payload.get("text", ""))
    allowed_entities = _string_set(factual_contract.get("entities", []))
    actual_entities = _string_set(payload.get("entities", []))
    allowed_amounts = _string_set(factual_contract.get("amounts", []))
    actual_amounts = _string_set(payload.get("amounts", []))
    allowed_contracts = _string_set(factual_contract.get("contracts", []))
    actual_contracts = _string_set(payload.get("contracts", []))
    hallucinated_entity = not actual_entities.issubset(allowed_entities)
    hallucinated_amount = not actual_amounts.issubset(allowed_amounts)
    hallucinated_contract = not actual_contracts.issubset(allowed_contracts)
    expected_status = str(factual_contract.get("status", "unknown"))
    actual_status = str(payload.get("status", "unknown"))
    strengthened_status = _STATUS_STRENGTH.get(actual_status, 99) > _STATUS_STRENGTH.get(
        expected_status, 0
    )
    expected_negation = bool(factual_contract.get("negation", False))
    actual_negation = bool(payload.get("negation", False))
    lost_negation = expected_negation and not actual_negation
    factual_omission = bool(
        (allowed_entities - actual_entities)
        or (allowed_amounts - actual_amounts)
        or (allowed_contracts - actual_contracts)
    )
    unsupported = any(
        (
            hallucinated_entity,
            hallucinated_amount,
            hallucinated_contract,
            strengthened_status,
            lost_negation,
        )
    )
    language_ok = _language_ok(text, expected_language)
    length_ok = 0 < len(text) <= max_output_chars
    valid = schema_ok and language_ok and length_ok
    return SafetyAssessment(
        valid_structured_output=valid,
        hallucinated_entity=hallucinated_entity,
        hallucinated_amount=hallucinated_amount,
        hallucinated_contract=hallucinated_contract,
        strengthened_status=strengthened_status,
        lost_negation=lost_negation,
        unsupported_factual_addition=unsupported,
        factual_omission=factual_omission,
        language_ok=language_ok,
        schema_ok=schema_ok,
        length_chars=len(text),
        accepted=valid and not unsupported,
    )


def _invoke(gateway: ModelGateway, request: ModelRequest):
    if request.task_type is ModelTaskType.COPY_GENERATION:
        return gateway.generate_copy(request)
    if request.task_type is ModelTaskType.HEADLINE_GENERATION:
        return gateway.generate_headline(request)
    if request.task_type is ModelTaskType.TRANSLATION:
        return gateway.translate(request)
    if request.task_type is ModelTaskType.STRUCTURED_VALIDATION:
        return gateway.generate_copy(request)
    return gateway.validate_vision(request)


class ModelBenchmark:
    def __init__(
        self,
        gateway: ModelGateway,
        task_profiles: Mapping[str, ModelTaskProfile],
        *,
        provider: str,
        model: str,
        runtime_profile: str,
    ) -> None:
        self.gateway = gateway
        self.task_profiles = task_profiles
        self.provider = provider
        self.model = model
        self.runtime_profile = runtime_profile

    def evaluate(self, fixtures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for fixture in fixtures:
            task_type = ModelTaskType(str(fixture["task_type"]))
            task_profile = self.task_profiles[task_type.value]
            factual_contract = fixture["factual_contract"]
            request = ModelRequest.create(
                task_type=task_type,
                prompt=str(fixture["prompt"]),
                schema=fixture["schema"],
                factual_contract=factual_contract,
                task_profile=task_profile,
            )
            started = time.perf_counter()
            result = _invoke(self.gateway, request)
            measured_latency = (time.perf_counter() - started) * 1000
            row: dict[str, Any] = {
                "fixture_id": fixture["fixture_id"],
                "task_type": task_type.value,
                "success": isinstance(result, ModelSuccess),
                "latency_ms": result.latency_ms if result.latency_ms else measured_latency,
                "failure_code": result.code.value if isinstance(result, ModelFailure) else None,
            }
            if isinstance(result, ModelSuccess):
                assessment = assess_model_payload(
                    result.payload,
                    factual_contract,
                    expected_language=str(fixture["expected_language"]),
                    max_output_chars=task_profile.max_output_chars,
                )
                row.update(asdict(assessment))
            else:
                row.update(
                    asdict(
                        SafetyAssessment(
                            False, False, False, False, False, False, False,
                            False, False, False, 0, False
                        )
                    )
                )
            records.append(row)
        latencies = [float(row["latency_ms"]) for row in records]
        total = len(records)
        count = lambda key: sum(bool(row[key]) for row in records)
        return {
            "runtime_profile": self.runtime_profile,
            "provider": self.provider,
            "model": self.model,
            "records_total": total,
            "metrics": {
                "success_rate": count("success") / total if total else 0.0,
                "valid_structured_output_rate": count("valid_structured_output") / total if total else 0.0,
                "timeout_rate": sum(row["failure_code"] == "timeout" for row in records) / total if total else 0.0,
                "hallucinated_entity_count": count("hallucinated_entity"),
                "hallucinated_amount_count": count("hallucinated_amount"),
                "hallucinated_contract_count": count("hallucinated_contract"),
                "strengthened_status_count": count("strengthened_status"),
                "lost_negation_count": count("lost_negation"),
                "unsupported_factual_addition_count": count("unsupported_factual_addition"),
                "factual_omission_count": count("factual_omission"),
                "language_compliance_rate": count("language_ok") / total if total else 0.0,
                "schema_compliance_rate": count("schema_ok") / total if total else 0.0,
                "average_length_chars": statistics.fmean(row["length_chars"] for row in records) if records else 0.0,
                "average_latency_ms": statistics.fmean(latencies) if latencies else 0.0,
                "accepted_rate": count("accepted") / total if total else 0.0,
            },
            "records": records,
        }


class FixtureMockGateway:
    """Deterministic ordered responses for the tracked benchmark scaffold."""

    def __init__(self, responses: Sequence[Mapping[str, Any]]) -> None:
        self.responses = [dict(response) for response in responses]
        self.index = 0

    def _next(self):
        if self.index >= len(self.responses):
            return ModelFailure(
                code=ModelFailureCode.PROVIDER_ERROR,
                message="mock fixture response exhausted",
                retryable=False,
            )
        response = self.responses[self.index]
        self.index += 1
        return MockModelGateway(response=response).generate_copy(None)  # type: ignore[arg-type]

    def generate_copy(self, request: ModelRequest):
        return self._next()

    def generate_headline(self, request: ModelRequest):
        return self._next()

    def translate(self, request: ModelRequest):
        return self._next()

    def validate_vision(self, request: ModelRequest):
        return ModelFailure(
            code=ModelFailureCode.DISABLED_CAPABILITY,
            message="vision is disabled in benchmark fixtures",
            retryable=False,
        )


def load_benchmark_fixtures(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["fixtures"])


__all__ = [
    "FixtureMockGateway",
    "ModelBenchmark",
    "SafetyAssessment",
    "assess_model_payload",
    "load_benchmark_fixtures",
]
