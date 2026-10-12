"""Stage D.2 shadow-only editorial wording over frozen D.1 facts."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Iterable, Literal

from premwire.model_gateway import ModelFailure, ModelGateway, ModelRequest, ModelSuccess, ModelTaskType
from premwire.model_runtime import ModelRuntimeProfile, ModelTaskProfile

from .c4a_entities import StrategyBEntityRegistry
from .database import V2Database
from .schema import utc_iso
from .stage_c_repository import StageCRepository
from .stage_d1_shadow import FACTUAL_CORE_VERSION


PREVIOUS_EDITORIAL_PROMPT_VERSION = "stage-d2-shadow-editorial-v1"
EDITORIAL_PROMPT_VERSION = "stage-d2-shadow-editorial-v2"
TRUSTED_FACT_VERSION = "stage-d23-trusted-editorial-facts-v1"
DETERMINISTIC_FALLBACK_VERSION = "stage-d24-safe-fallback-v1"
_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "body"],
    "properties": {
        "headline": {"type": "string", "minLength": 1},
        "body": {"type": "string", "minLength": 1},
    },
}
_UPGRADE_WORDS = re.compile(r"\b(signed|completed|official\w*|announced|confirmed transfer|\u043f\u0435\u0440\u0435\u0448[\u0451\u0435]\u043b|\u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043b|\u043e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d)\b", re.I)
_OFFICIAL_STATUS_WORDS = re.compile(r"\b(official|officially|announced|confirmed|deal done|transfer completed|\u043e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\w*|\u043e\u0431\u044a\u044f\u0432\u0438\u043b\w*|\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\w*|\u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\w*)\b", re.I)
_NEGATION_WORDS = re.compile(r"\b(not|no |never|denied|\u043d\u0435 |\u043d\u0435\u0442 |\u0431\u0435\u0437 |\u043e\u0442\u0440\u0438\u0446\w*)", re.I)
_MONEY_WORDS = re.compile(r"(?:[€£$]|\b(?:eur|gbp|usd|million|m)\b)", re.I)
_CURRENCY_MARKER = re.compile(r"(?:[€£$]|\b(?:eur|gbp|usd)\b)", re.I)
_YEAR_WORD = re.compile(r"\b20\d{2}\b")
_INTERNAL_ID_WORD = re.compile(r"\b(?:player|club)-[a-z0-9][a-z0-9-]*\b", re.I)
_NATIONALITY_WORDS = re.compile(r"\b(english|german|french|italian|spanish|brazilian|argentine|russian|\u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\w*|\u043d\u0435\u043c\u0435\u0446\u043a\w*|\u0444\u0440\u0430\u043d\u0446\u0443\u0437\u0441\u043a\w*|\u0438\u0442\u0430\u043b\u044c\u044f\u043d\u0441\u043a\w*|\u0438\u0441\u043f\u0430\u043d\u0441\u043a\w*)\b", re.I)
_POSITION_WORDS = re.compile(r"\b(winger|forward|midfielder|defender|goalkeeper|striker|\u0432\u0438\u043d\u0433\u0435\u0440\w*|\u043d\u0430\u043f\u0430\u0434\u0430\u044e\u0449\w*|\u043f\u043e\u043b\u0443\u0437\u0430\u0449\u0438\u0442\u043d\u0438\u043a\w*|\u0437\u0430\u0449\u0438\u0442\u043d\u0438\u043a\w*|\u0432\u0440\u0430\u0442\u0430\u0440\w*)\b", re.I)
_LEAGUE_WORDS = re.compile(r"\b(premier league|bundesliga|la liga|serie a|champions league|\u043f\u0440\u0435\u043c\u044c\u0435\u0440-\u043b\u0438\u0433\w*|\u0431\u0443\u043d\u0434\u0435\u0441\u043b\u0438\u0433\w*|\u043b\u0430 \u043b\u0438\u0433\w*|\u0441\u0435\u0440\u0438\u044f \u0430)\b", re.I)
_CLUB_COUNTRY_WORDS = re.compile(r"\b(?:english|german|french|italian|spanish|\u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\w*|\u043d\u0435\u043c\u0435\u0446\u043a\w*|\u0444\u0440\u0430\u043d\u0446\u0443\u0437\u0441\u043a\w*|\u0438\u0442\u0430\u043b\u044c\u044f\u043d\u0441\u043a\w*|\u0438\u0441\u043f\u0430\u043d\u0441\u043a\w*)\s+(?:club|\u043a\u043b\u0443\u0431\w*)\b", re.I)
_COMPLETED_TRANSFER_WORDS = re.compile(
    r"\b(?:\u043f\u0435\u0440\u0435\u0448[\u0435\u0451]\u043b\w*|\u0441\u043e\u0432\u0435\u0440\u0448\u0438\u043b\w*\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434|\u0437\u0430\u0432\u0435\u0440\u0448\u0438\u043b\w*\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434|\u0441\u0442\u0430\u043b\w*\s+\u0438\u0433\u0440\u043e\u043a\w*|joined|joins|moved|completed\s+(?:(?:his|the)\s+)?(?:move|transfer)|signed\s+for)\b",
    re.I,
)
_COMPLETED_TRANSFER_DOWNGRADE_WORDS = re.compile(
    r"\b(?:\u043c\u043e\u0436\u0435\u0442\s+(?:\u043f\u0435\u0440\u0435\u0439\u0442\u0438|\u043f\u043e\u0434\u043f\u0438\u0441\u0430\u0442\u044c)|\u0440\u0430\u0441\u0441\u043c\u0430\u0442\u0440\u0438\u0432\u0430\u0435\u0442\w*\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434|\u0438\u043d\u0442\u0435\u0440\u0435\u0441\u0443\u0435\u0442\u0441\u044f|\u0437\u0430\u0438\u043d\u0442\u0435\u0440\u0435\u0441\u043e\u0432\u0430\u043d\w*|\u0432\u0435\u0434[\u0435\u0451]\u0442\w*\s+\u043f\u0435\u0440\u0435\u0433\u043e\u0432\u043e\u0440\w*|\u0431\u043b\u0438\u0437\u043e\u043a\w*\s+\u043a\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434\w*|\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u0441\u044f\s+\u043a\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434\w*|\u0432\u043e\u0437\u043c\u043e\u0436\u043d\w*\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434|\u043f\u043e\u0442\u0435\u043d\u0446\u0438\u0430\u043b\u044c\u043d\w*\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434|\u0445\u043e\u0447\u0435\u0442\s+\u043f\u043e\u0434\u043f\u0438\u0441\u0430\u0442\u044c|\u043d\u0430\u043c\u0435\u0440\u0435\u043d\w*\s+\u043f\u043e\u0434\u043f\u0438\u0441\u0430\u0442\u044c|may\s+(?:join|move|sign)|interested|interest|in\s+talks|potential\s+(?:move|transfer)|considering|close\s+to)\b",
    re.I,
)
_LOAN_WORDS = re.compile(r"\b(?:\u0430\u0440\u0435\u043d\u0434\w*|loan(?:ed)?|on\s+loan)\b", re.I)
_REPORT_WORDS = re.compile(r"\b(?:\u0441\u043e\u043e\u0431\u0449\u0430\u0435\u0442\w*|\u043f\u043e\s+\u0434\u0430\u043d\u043d\u044b\u043c|\u043e\u0436\u0438\u0434\u0430\u0435\u0442\w*|reports?|reportedly|according\s+to|expected)\b", re.I)
_RUMOUR_WORDS = re.compile(r"\b(?:\u043c\u043e\u0436\u0435\u0442\s+\u043f\u0435\u0440\u0435\u0439\u0442\u0438|\u0441\u0432\u044f\u0437\u044b\u0432\u0430\u044e\u0442\w*|\u0432\u043e\u0437\u043c\u043e\u0436\u043d\w*\s+\u043f\u0435\u0440\u0435\u0445\u043e\u0434|rumou?r|linked\s+with|may\s+(?:join|move)|potential\s+(?:move|transfer))\b", re.I)
_INTEREST_WORDS = re.compile(r"\b(?:\u0438\u043d\u0442\u0435\u0440\u0435\u0441\u0443\u0435\u0442\u0441\u044f|\u043f\u0440\u043e\u044f\u0432\u043b\u044f\u0435\u0442\w*\s+\u0438\u043d\u0442\u0435\u0440\u0435\u0441|\u0440\u0430\u0441\u0441\u043c\u0430\u0442\u0440\u0438\u0432\u0430\u0435\u0442\w*\s+\u0432\u043e\u0437\u043c\u043e\u0436\u043d\u043e\u0441\u0442\u044c\s+\u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043d\u0438\u044f|interest(?:ed)?|targeting|monitor\w*)\b", re.I)
_CONTRACT_WORDS = re.compile(r"\b(?:\u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\w*|\u0441\u043e\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435\w*|contract|agreement)\b", re.I)
_RELEASE_WORDS = re.compile(r"\b(?:\u043f\u043e\u043a\u0438\u043d\u0443\u043b\w*|\u043e\u0442\u043f\u0443\u0449\u0435\u043d\w*|\u0440\u0430\u0441\u0442\u043e\u0440\u0433\w*|released|release\w*|terminated|left|leave)\b", re.I)
_FREE_AGENT_WORDS = re.compile(r"\b(?:\u0441\u0432\u043e\u0431\u043e\u0434\u043d\w*\s+\u0430\u0433\u0435\u043d\u0442\w*|free\s+agent)\b", re.I)
_RETURN_FROM_LOAN_WORDS = re.compile(r"\b(?:\u0432\u0435\u0440\u043d\u0443\u043b\w*\s+(?:\u0438\u0437\s+)?\u0430\u0440\u0435\u043d\u0434\w*|\u0438\u0437\s+\u0430\u0440\u0435\u043d\u0434\w*|returned?\s+from\s+loan)\b", re.I)
_REJECTED_BID_WORDS = re.compile(r"\b(?:\u043e\u0442\u043a\u043b\u043e\u043d\u0438\u043b\w*\s+\u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435|rejected\s+(?:a\s+)?bid|turned\s+down\s+(?:a\s+)?bid)\b", re.I)
_ENTITY_SURFACE = r"[A-Z\u0410-\u042f\u0401][\w'’\-]*(?:\s+[A-Z\u0410-\u042f\u0401][\w'’\-]*){0,3}"
_PLAYER_SUBJECT_SURFACE = re.compile(
    rf"(?<!\w)({_ENTITY_SURFACE})\s+(?:\u043f\u0435\u0440\u0435\u0448[\u0435\u0451]\u043b\w*|joined|joins|signed\s+for|may\s+(?:join|move))\b"
)
_FROM_CLUB_SURFACE = re.compile(rf"(?<!\w)(?:\u0438\u0437|\u0418\u0437|from|From)\s+[«\"“]?({_ENTITY_SURFACE})")
_TO_CLUB_SURFACE = re.compile(rf"(?<!\w)(?:\u0432|\u0412|to|To)\s+[«\"“]?({_ENTITY_SURFACE})")


@dataclass(frozen=True, slots=True)
class EditorialShadowConfig:
    editorial_shadow_enabled: bool
    auto_publish: bool = False

    def __post_init__(self) -> None:
        if self.auto_publish:
            raise ValueError("Stage D.2 editorial shadow never permits auto_publish")

    @classmethod
    def from_app_config(cls, app_config: Any) -> "EditorialShadowConfig":
        return cls(editorial_shadow_enabled=bool(app_config.pipeline_v2.shadow_enabled))


@dataclass(frozen=True, slots=True)
class ShadowEditorialResult:
    story_id: str
    editorial_status: Literal["generated", "generated_fallback", "blocked_validation", "model_error", "skipped", "disabled"]
    headline: str | None = None
    body: str | None = None
    validation_errors: tuple[str, ...] = ()
    latency_ms: float = 0.0
    deduplicated: bool = False
    llm_status: str = "not_attempted"
    llm_validation_errors: tuple[str, ...] = ()
    fallback_attempted: bool = False
    fallback_latency_ms: float = 0.0
    generation_source: str = "none"
    validation_latency_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class EditorialResolution:
    editorial_status: Literal["generated", "generated_fallback", "blocked_validation", "model_error"]
    generation_source: Literal["llm", "deterministic_fallback", "none"]
    headline: str | None
    body: str | None
    llm_status: str
    llm_validation_errors: tuple[str, ...]
    fallback_attempted: bool
    fallback_reason: tuple[str, ...]
    fallback_validation_errors: tuple[str, ...]
    fallback_latency_ms: float = 0.0


class FrozenShadowEditorialPipeline:
    """Existing ModelGateway plus additive, non-publishing shadow persistence."""

    def __init__(
        self, *, database: V2Database, project_root, config: EditorialShadowConfig,
        gateway: ModelGateway, runtime_profile: ModelRuntimeProfile,
        task_profile: ModelTaskProfile,
    ) -> None:
        self.database = database
        self.project_root = project_root
        self.config = config
        self.gateway = gateway
        self.runtime_profile = runtime_profile
        self.task_profile = task_profile
        self.registry = StrategyBEntityRegistry(project_root)

    def process_many(self, story_ids: Iterable[str]) -> tuple[ShadowEditorialResult, ...]:
        return tuple(self.process(story_id) for story_id in story_ids)

    def process(self, story_id: str) -> ShadowEditorialResult:
        if not self.config.editorial_shadow_enabled:
            return ShadowEditorialResult(story_id, "disabled")
        self.database.initialise()
        with self.database.transaction() as db:
            repository = StageCRepository(db)
            factual = repository.load_factual_shadow_result(story_id, FACTUAL_CORE_VERSION)
            if factual is None or factual["decision"] != "routed":
                return ShadowEditorialResult(story_id, "skipped")
            existing = repository.load_shadow_editorial_audit(
                story_id, FACTUAL_CORE_VERSION, EDITORIAL_PROMPT_VERSION,
                DETERMINISTIC_FALLBACK_VERSION,
            )
            if existing is not None:
                return self._result(existing, deduplicated=True)
        try:
            contract = build_editorial_input(factual, self.registry)
        except ValueError:
            resolution = EditorialResolution(
                "blocked_validation", "none", None, None, "not_attempted",
                ("TRUSTED_FACT_CONTRACT_INVALID",), False, (), (),
            )
            return self._persist_resolution(factual, resolution, 0.0, 0.0)
        request = ModelRequest.create(
            task_type=ModelTaskType.COPY_GENERATION,
            prompt=build_editorial_prompt(contract), schema=_OUTPUT_SCHEMA,
            factual_contract=contract, task_profile=self.task_profile,
        )
        try:
            response = self.gateway.generate_copy(request)
        except Exception as exc:
            validation_started = perf_counter()
            resolution = resolve_editorial_output(
                contract, None, self.registry,
                (f"MODEL_GATEWAY_EXCEPTION:{type(exc).__name__}",),
            )
            validation_latency_ms = (perf_counter() - validation_started) * 1000
            return self._persist_resolution(factual, resolution, 0.0, validation_latency_ms)
        validation_started = perf_counter()
        resolution = resolve_editorial_output(contract, response, self.registry)
        validation_latency_ms = (perf_counter() - validation_started) * 1000
        return self._persist_resolution(
            factual, resolution, response.latency_ms, validation_latency_ms,
        )

    def _persist_resolution(
        self, factual: dict[str, Any], resolution: EditorialResolution, latency_ms: float,
        validation_latency_ms: float,
    ) -> ShadowEditorialResult:
        payload = self._payload(
            factual, resolution.editorial_status, resolution.headline, resolution.body,
            resolution.llm_validation_errors if resolution.generation_source != "deterministic_fallback"
            else resolution.fallback_validation_errors,
            latency_ms,
        )
        audit = {
            **payload,
            "deterministic_fallback_version": DETERMINISTIC_FALLBACK_VERSION,
            "trusted_fact_version": TRUSTED_FACT_VERSION,
            "llm_attempted": resolution.llm_status != "not_attempted",
            "llm_status": resolution.llm_status,
            "llm_validation_errors": list(resolution.llm_validation_errors),
            "fallback_attempted": resolution.fallback_attempted,
            "fallback_reason": list(resolution.fallback_reason),
            "fallback_validation_errors": list(resolution.fallback_validation_errors),
            "generation_source": resolution.generation_source,
            "fallback_latency_ms": resolution.fallback_latency_ms,
            "validation_latency_ms": max(0.0, validation_latency_ms),
        }
        with self.database.transaction() as db:
            repository = StageCRepository(db)
            compatibility_payload = dict(payload)
            if compatibility_payload["editorial_status"] == "generated_fallback":
                compatibility_payload["editorial_status"] = "generated"
            repository.save_shadow_editorial_result(compatibility_payload)
            repository.save_shadow_editorial_audit(audit)
        return self._result(audit)

    def _payload(self, factual: dict[str, Any], status: str, headline: str | None,
                 body: str | None, errors: Iterable[str], latency_ms: float) -> dict[str, Any]:
        return {
            "story_id": factual["story_id"], "factual_core_version": FACTUAL_CORE_VERSION,
            "editorial_prompt_version": EDITORIAL_PROMPT_VERSION,
            "factual_result_processed_at": factual["processed_at"], "editorial_status": status,
            "headline": headline, "body": body, "model_backend": self.runtime_profile.provider,
            "model_name": self.runtime_profile.model, "validation_errors": list(errors),
            "generated_at": utc_iso(), "latency_ms": max(0.0, latency_ms),
        }

    @staticmethod
    def _result(payload: dict[str, Any], *, deduplicated: bool = False) -> ShadowEditorialResult:
        errors = payload.get("validation_errors")
        if errors is None:
            errors = [
                *payload.get("llm_validation_errors", []),
                *payload.get("fallback_validation_errors", []),
            ]
        return ShadowEditorialResult(
            story_id=str(payload["story_id"]), editorial_status=str(payload["editorial_status"]),
            headline=payload.get("headline"), body=payload.get("body"),
            validation_errors=tuple(str(item) for item in errors),
            latency_ms=float(payload.get("latency_ms", 0.0)), deduplicated=deduplicated,
            llm_status=str(payload.get("llm_status", "not_attempted")),
            llm_validation_errors=tuple(
                str(item) for item in payload.get("llm_validation_errors", [])
            ),
            fallback_attempted=bool(payload.get("fallback_attempted", False)),
            fallback_latency_ms=float(payload.get("fallback_latency_ms", 0.0)),
            generation_source=str(payload.get("generation_source", "none")),
            validation_latency_ms=float(payload.get("validation_latency_ms", 0.0)),
        )


def build_editorial_input(
    factual: dict[str, Any], registry: StrategyBEntityRegistry,
) -> dict[str, Any]:
    """The explicit allow-listed TRUSTED_FACTS object; no raw source authority."""
    def entity(entity_id: object, entity_type: str) -> dict[str, str] | None:
        if entity_id is None:
            return None
        matched = next(
            (item for item in registry.entities
             if item.entity_id == str(entity_id) and item.entity_type == entity_type),
            None,
        )
        if matched is None:
            raise ValueError("unresolved trusted entity")
        return {"id": matched.entity_id, "display_name": matched.canonical_name}

    player = entity(factual.get("player_entity_id"), "player")
    from_club = entity(factual.get("from_club_id"), "club")
    to_clubs = [entity(value, "club") for value in factual.get("to_club_ids", [])]
    if player is None:
        raise ValueError("routed factual result lacks player")
    contract: dict[str, Any] = {
        "event_family": factual.get("event_family"),
        "transfer_direction": factual.get("transfer_direction"),
        "negated": bool(factual.get("negated")),
        "player": player,
        "from_club": from_club,
        "to_club": to_clubs[0] if len(to_clubs) == 1 else None,
        "interested_clubs": to_clubs if factual.get("transfer_direction") == "interest" else [],
        "evidence": [
            {"field": item.get("field"), "source_text": item.get("source_text")}
            for item in factual.get("evidence", [])
        ],
    }
    if factual.get("amount_value") is not None:
        contract["amount"] = factual["amount_value"]
    if factual.get("currency") is not None:
        contract["currency"] = factual["currency"]
    if factual.get("contract_until") is not None:
        contract["contract_until"] = factual["contract_until"]
    return contract


def build_editorial_prompt(contract: dict[str, Any]) -> str:
    """V2: neutral short prose is safer than unsupported enrichment."""
    return (
        "Верни JSON с полями headline и body. Пиши по-русски, нейтрально и кратко: "
        "короткий headline и 1–2 коротких предложения body. Используй ТОЛЬКО TRUSTED_FACTS. "
        "Every factual statement in headline/body must be directly supported by TRUSTED_FACTS. "
        "If a fact is not explicitly present, omit it. Не используй внешние знания, память модели, "
        "догадки, характеристики игрока или клуба. Не добавляй nationality, position, age, country, "
        "league, fee, currency, contract, quote, source attribution или official status, если поля нет в TRUSTED_FACTS. "
        "Сохраняй event_family, transfer_direction и negated. Используй display_name ровно из TRUSTED_FACTS; "
        "id — только для машинной проверки и никогда не должен попадать в текст. "
        "Не украшай текст и не восстанавливай source text.\n\nTRUSTED_FACTS:\n"
        f"{json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
    )


def _structured_output(response: ModelSuccess) -> tuple[str | None, str | None, list[str]]:
    payload = response.payload
    if set(payload) != {"headline", "body"}:
        return None, None, ["MODEL_OUTPUT_SCHEMA_INVALID"]
    headline, body = payload.get("headline"), payload.get("body")
    if not isinstance(headline, str) or not isinstance(body, str) or not headline.strip() or not body.strip():
        return None, None, ["MODEL_OUTPUT_SCHEMA_INVALID"]
    return headline.strip(), body.strip(), []


def resolve_editorial_output(
    contract: dict[str, Any], response: ModelSuccess | ModelFailure | None,
    registry: StrategyBEntityRegistry, gateway_errors: tuple[str, ...] = (),
) -> EditorialResolution:
    """Resolve one LLM attempt; a fallback never makes a second model request."""
    if gateway_errors:
        return _resolve_with_fallback(contract, registry, "model_error", gateway_errors)
    if isinstance(response, ModelFailure):
        return _resolve_with_fallback(
            contract, registry, "model_error", (f"MODEL_{response.code.value.upper()}",),
        )
    if response is None:
        return _resolve_with_fallback(contract, registry, "model_error", ("MODEL_NO_RESPONSE",))
    headline, body, schema_errors = _structured_output(response)
    if schema_errors:
        return _resolve_with_fallback(contract, registry, "model_error", tuple(schema_errors))
    errors = tuple(validate_editorial_output(contract, headline, body, registry))
    if not errors:
        return EditorialResolution(
            "generated", "llm", headline, body, "accepted", (), False, (), (), 0.0,
        )
    return _resolve_with_fallback(contract, registry, "blocked_validation", errors)


def _resolve_with_fallback(
    contract: dict[str, Any], registry: StrategyBEntityRegistry, llm_status: str,
    llm_errors: tuple[str, ...],
) -> EditorialResolution:
    started = perf_counter()
    fallback = build_deterministic_fallback(contract)
    fallback_latency_ms = (perf_counter() - started) * 1000
    if fallback is None:
        return EditorialResolution(
            "blocked_validation" if llm_status == "blocked_validation" else "model_error",
            "none", None, None, llm_status, llm_errors, False, llm_errors, (), fallback_latency_ms,
        )
    headline, body = fallback
    fallback_errors = tuple(validate_editorial_output(contract, headline, body, registry))
    if fallback_errors:
        return EditorialResolution(
            "blocked_validation", "none", None, None, llm_status, llm_errors,
            True, llm_errors, fallback_errors, fallback_latency_ms,
        )
    return EditorialResolution(
        "generated_fallback", "deterministic_fallback", headline, body,
        llm_status, llm_errors, True, llm_errors, (), fallback_latency_ms,
    )


def build_deterministic_fallback(contract: dict[str, Any]) -> tuple[str, str] | None:
    """Small templates over TRUSTED_FACTS only.  Unknown fields are omitted."""
    player = _display(contract.get("player"))
    from_club = _display(contract.get("from_club"))
    to_club = _display(contract.get("to_club"))
    interested = [name for name in (_display(item) for item in contract.get("interested_clubs", [])) if name]
    family = str(contract.get("event_family") or "")
    if not player:
        return None
    if contract.get("negated"):
        if family in {"club_interest", "interest"} and to_club:
            return f"{to_club} не планирует подписывать {player}", f"{to_club} не планирует подписывать {player}."
        if family in {"confirmed_transfer_report", "confirmed_report"} and to_club:
            return f"Сообщения о переходе {player} отрицаются", f"Сообщения о переходе {player} в {to_club} отрицаются."
        return None
    if family == "completed_transfer":
        if to_club:
            headline = f"{player} перешёл в {to_club}"
            body = f"{player} перешёл из {from_club} в {to_club}." if from_club else f"{player} перешёл в {to_club}."
            return headline, _append_trusted_amount(body, contract)
        return None
    if family in {"completed_loan", "loan"}:
        if to_club:
            headline = f"{player} перешёл в {to_club} на правах аренды"
            body = f"{player} перешёл из {from_club} в {to_club} на правах аренды." if from_club else f"{player} перешёл в {to_club} на правах аренды."
            return headline, body
        return None
    if family in {"confirmed_transfer_report", "confirmed_report"} and to_club:
        headline = f"Сообщается о переходе {player} в {to_club}"
        body = f"Сообщается, что {player} может перейти из {from_club} в {to_club}." if from_club else f"Сообщается, что {player} может перейти в {to_club}."
        return headline, body
    if family in {"transfer_rumour", "rumour"} and to_club:
        headline = f"{player} может перейти в {to_club}"
        body = f"{player} связывают с возможным переходом из {from_club} в {to_club}." if from_club else f"{player} связывают с возможным переходом в {to_club}."
        return headline, body
    if family in {"club_interest", "interest"}:
        if len(interested) == 1:
            club = interested[0]
            return f"{club} интересуется {player}", f"{club} проявляет интерес к {player}."
        if len(interested) >= 2:
            clubs = ", ".join(interested)
            return f"Несколько клубов интересуются {player}", f"{clubs} проявляют интерес к {player}."
        return None
    if family in {"contract_extension", "new_contract_without_transfer", "contract"} and from_club:
        verb = "продлил контракт с" if family != "new_contract_without_transfer" else "подписал контракт с"
        suffix = f" до {contract['contract_until']}" if contract.get("contract_until") else ""
        return f"{player} {verb} {from_club}", f"{player} {verb} {from_club}{suffix}."
    if family in {"player_release", "contract_termination", "free_agent_release"} and from_club:
        return f"{player} покинул {from_club}", f"{player} покинул {from_club}."
    if family == "free_agent" and from_club:
        return f"{player} стал свободным агентом", f"{player} стал свободным агентом после ухода из {from_club}."
    if family == "return_from_loan" and from_club and to_club:
        return f"{player} вернулся в {to_club}", f"{player} вернулся из аренды в {to_club} из {from_club}."
    return None


def _display(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("display_name")
    return str(name) if isinstance(name, str) and name else None


def _append_trusted_amount(body: str, contract: dict[str, Any]) -> str:
    amount, currency = contract.get("amount"), contract.get("currency")
    if isinstance(amount, int) and isinstance(currency, str) and currency in {"EUR", "GBP", "USD"}:
        return f"{body[:-1]} за {amount} {currency}."
    return body


def _semantic_status_errors(contract: dict[str, Any], headline: str, body: str) -> list[str]:
    """Keep every editorial field within its non-linear trusted event family."""
    family = str(contract.get("event_family") or "")
    parts = (headline, body)

    def each(pattern: re.Pattern[str]) -> bool:
        return all(pattern.search(part) is not None for part in parts)

    errors: list[str] = []
    if contract.get("negated"):
        if not each(_NEGATION_WORDS):
            errors.append("NEGATION_LOST")
        return errors
    if any(_NEGATION_WORDS.search(part) for part in parts):
        errors.append("POSITIVE_EVENT_NEGATED")

    if family == "completed_transfer":
        if any(_COMPLETED_TRANSFER_DOWNGRADE_WORDS.search(part) for part in parts) or not each(_COMPLETED_TRANSFER_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family in {"completed_loan", "loan"}:
        if not each(_LOAN_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family in {"confirmed_transfer_report", "confirmed_report"}:
        if not each(_REPORT_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family in {"transfer_rumour", "rumour"}:
        if not each(_RUMOUR_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family in {"club_interest", "interest"}:
        if not each(_INTEREST_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family in {"contract_extension", "new_contract_without_transfer", "contract"}:
        if not each(_CONTRACT_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family in {"player_release", "contract_termination", "free_agent_release"}:
        if not each(_RELEASE_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family == "free_agent":
        if not each(_FREE_AGENT_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family == "return_from_loan":
        if not each(_RETURN_FROM_LOAN_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    elif family == "rejected_bid":
        if not each(_REJECTED_BID_WORDS):
            errors.append("SEMANTIC_STATUS_DOWNGRADE")
    return errors


def _closed_world_entity_role_errors(text: str, registry: StrategyBEntityRegistry) -> list[str]:
    """Fail closed for explicit transfer-role surfaces absent from the registry.

    Exact registry hits are checked separately against TRUSTED_FACTS IDs.  This
    small contextual guard covers a capitalized transfer subject or FROM/TO
    surface that cannot be resolved at all (for example, an unregistered club
    substituted into an ``из … в …`` clause).  It deliberately does not infer
    a canonical identity from an unknown surface.
    """
    errors: set[str] = set()
    for surface in _PLAYER_SUBJECT_SURFACE.findall(text):
        if not registry.scan(surface, "player"):
            errors.add("UNSUPPORTED_PLAYER_ENTITY")
    for pattern in (_FROM_CLUB_SURFACE, _TO_CLUB_SURFACE):
        for surface in pattern.findall(text):
            if not registry.scan(surface, "club"):
                errors.add("UNSUPPORTED_CLUB_ENTITY")
    return sorted(errors)


def validate_editorial_output(contract: dict[str, Any], headline: str, body: str,
                               registry: StrategyBEntityRegistry) -> list[str]:
    """Narrow deterministic V2 gate. Unknown facts always fail closed."""
    text = f"{headline}\n{body}"
    errors: list[str] = []
    player = contract.get("player") or {}
    from_club = contract.get("from_club") or {}
    to_club = contract.get("to_club") or {}
    interested = contract.get("interested_clubs") or []
    allowed_players = {player.get("id")} - {None}
    allowed_clubs = {
        from_club.get("id"), to_club.get("id"),
        *(item.get("id") for item in interested if isinstance(item, dict)),
    } - {None}
    if _INTERNAL_ID_WORD.search(text):
        errors.append("INTERNAL_ID_LEAK")
    player_hits = {hit.entity_id for hit in registry.scan(text, "player")}
    club_hits = {hit.entity_id for hit in registry.scan(text, "club")}
    if player_hits - allowed_players:
        errors.append("UNSUPPORTED_PLAYER_ENTITY")
    if club_hits - allowed_clubs:
        errors.append("UNSUPPORTED_CLUB_ENTITY")
    errors.extend(_closed_world_entity_role_errors(text, registry))
    if _player_display_expanded(text, player, registry):
        errors.append("UNTRUSTED_PLAYER_DISPLAY")
    family = str(contract.get("event_family") or "")
    if family in {"club_interest", "transfer_rumour", "interest", "rumour", "confirmed_report"} and _UPGRADE_WORDS.search(text):
        errors.append("EVENT_STATUS_UPGRADED")
    if family == "completed_transfer" and _OFFICIAL_STATUS_WORDS.search(text):
        errors.append("STATUS_ESCALATION_OFFICIAL")
    errors.extend(_semantic_status_errors(contract, headline, body))
    amount, currency = contract.get("amount"), contract.get("currency")
    if amount is None and _MONEY_WORDS.search(text):
        errors.append("INVENTED_AMOUNT")
    elif amount is not None:
        expected = _amount_literals(int(amount))
        if _MONEY_WORDS.search(text) and not any(item in text.casefold() for item in expected):
            errors.append("AMOUNT_MISMATCH")
        if currency is None and _CURRENCY_MARKER.search(text):
            errors.append("INVENTED_CURRENCY")
        if currency and _MONEY_WORDS.search(text) and not _currency_present(text, str(currency)):
            errors.append("CURRENCY_MISMATCH")
    contract_until = contract.get("contract_until")
    years = set(_YEAR_WORD.findall(text))
    if contract_until is None and years:
        errors.append("INVENTED_CONTRACT")
    elif contract_until is not None and years and str(contract_until) not in years:
        errors.append("CONTRACT_MISMATCH")
    if _direction_inverted(text, contract):
        errors.append("CLUB_DIRECTION_INVERTED")
    if _NATIONALITY_WORDS.search(text):
        errors.append("UNSUPPORTED_NATIONALITY_DESCRIPTOR")
    if _POSITION_WORDS.search(text):
        errors.append("UNSUPPORTED_POSITION_DESCRIPTOR")
    if _CLUB_COUNTRY_WORDS.search(text):
        errors.append("UNSUPPORTED_CLUB_COUNTRY_DESCRIPTOR")
    if _LEAGUE_WORDS.search(text):
        errors.append("UNSUPPORTED_LEAGUE_DESCRIPTOR")
    if _unbalanced_quotes(text):
        errors.append("UNBALANCED_QUOTES")
    if len(headline) > 180 or len(body) > 900:
        errors.append("EDITORIAL_LENGTH_EXCEEDED")
    if re.search(r"\b(?:placeholder|test player|todo)\b", text, re.I):
        errors.append("PLACEHOLDER_ARTIFACT")
    return errors


def _amount_literals(amount: int) -> set[str]:
    literals = {str(amount)}
    if amount % 1_000_000 == 0:
        millions = amount // 1_000_000
        literals.update({f"{millions}m", f"{millions} million"})
    return {item.casefold() for item in literals}


def _currency_present(text: str, currency: str) -> bool:
    expected = {"EUR": ("€", "eur"), "GBP": ("£", "gbp"), "USD": ("$", "usd")}.get(currency, ())
    lowered = text.casefold()
    return any(item in lowered for item in expected)


def _direction_inverted(text: str, contract: dict[str, Any]) -> bool:
    if contract.get("transfer_direction") not in {"from_to", "permanent"}:
        return False
    from_name = str((contract.get("from_club") or {}).get("display_name") or "")
    to_name = str((contract.get("to_club") or {}).get("display_name") or "")
    lowered = text.casefold()
    return bool(from_name and to_name and (
        f"{from_name.casefold()} from {to_name.casefold()}" in lowered
        or f"{from_name.casefold()} из {to_name.casefold()}" in lowered
    ))


def _player_display_expanded(text: str, player: dict[str, Any], registry: StrategyBEntityRegistry) -> bool:
    entity_id = player.get("id")
    entity = next((item for item in registry.entities if item.entity_id == entity_id), None)
    if entity is None:
        return True
    allowed = {alias.casefold() for alias in entity.aliases}
    suffixes = {alias.split()[-1] for alias in entity.aliases if " " in alias}
    for suffix in suffixes:
        pattern = re.compile(rf"(?<!\w)(?:[\w\-']+[ \t]+){{1,2}}{re.escape(suffix)}(?!\w)", re.I)
        if any(match.group(0).casefold() not in allowed for match in pattern.finditer(text)):
            return True
    return False


def _unbalanced_quotes(text: str) -> bool:
    return (
        text.count("«") != text.count("»")
        or text.count("“") != text.count("”")
        or text.count('"') % 2 != 0
    )


__all__ = [
    "EDITORIAL_PROMPT_VERSION", "PREVIOUS_EDITORIAL_PROMPT_VERSION", "EditorialShadowConfig",
    "TRUSTED_FACT_VERSION", "DETERMINISTIC_FALLBACK_VERSION", "FrozenShadowEditorialPipeline",
    "ShadowEditorialResult", "EditorialResolution", "build_editorial_input", "build_editorial_prompt",
    "build_deterministic_fallback", "resolve_editorial_output", "validate_editorial_output",
]
