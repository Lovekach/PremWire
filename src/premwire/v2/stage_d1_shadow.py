from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Iterable, Literal, Protocol

from .c4a_extraction import StrategyBFactualExtractor, StrategyBSourceEnvelope
from .c4a_models import (
    ContractEvent,
    PlayerLifecycleEvent,
    RouteDecision,
    SourceRole,
    TransferEvent,
    TransferInterestEvent,
)
from .database import V2Database
from .models import SourceEventInput
from .schema import utc_iso
from .services import StoryIngestService
from .stage_c_repository import StageCRepository


FACTUAL_CORE_VERSION = "stage-c4b-corrective-factual-core-freeze"
FACTUAL_CORE_FREEZE_COMMIT = "1124cce14b60601ecc172cfb3cf1f9d2f8f64ccc"
SHADOW_STAGE_RUN_KEY = FACTUAL_CORE_VERSION
LOGGER = logging.getLogger(__name__)


class FactualCore(Protocol):
    def parse(self, envelope: StrategyBSourceEnvelope): ...


@dataclass(frozen=True, slots=True)
class FactualShadowConfig:
    factual_shadow_enabled: bool
    auto_publish: bool = False

    def __post_init__(self) -> None:
        if self.auto_publish:
            raise ValueError("Stage D.1 factual shadow never permits auto_publish")

    @classmethod
    def from_app_config(cls, app_config) -> "FactualShadowConfig":
        """Use the existing V2 shadow switch; publishing remains hard-disabled."""
        return cls(factual_shadow_enabled=bool(app_config.pipeline_v2.shadow_enabled))


@dataclass(frozen=True, slots=True)
class FactualShadowInput:
    story_id: str
    platform: str
    source_id: str
    source_language: Literal["ru", "en", "mixed", "unknown"]
    source_text: str
    source_url: str
    external_message_id: str
    published_at: str
    collected_at: str
    source_category: str = "unknown"
    channel_id: str | None = None
    sport: str | None = None
    category: str | None = None
    translation_ru: str | None = None


@dataclass(frozen=True, slots=True)
class FactualShadowResult:
    story_id: str
    processed_at: str | None
    factual_core_version: str
    factual_core_freeze_commit: str
    decision: Literal["routed", "blocked", "error", "disabled"]
    source_event_id: int | None
    event_family: str | None = None
    transfer_direction: str | None = None
    player_entity_id: str | None = None
    entities: tuple[str, ...] = ()
    from_club_id: str | None = None
    to_club_ids: tuple[str, ...] = ()
    amount_value: int | None = None
    currency: str | None = None
    contract_until: str | None = None
    negated: bool = False
    evidence: tuple[dict[str, object], ...] = ()
    block_reason: str | None = None
    error_code: str | None = None
    latency_ms: float = 0.0
    deduplicated: bool = False


class FrozenFactualShadowPipeline:
    """Stage D.1 adapter: existing V2 ingestion plus the frozen C.4B parser.

    It writes only the additive shadow table and existing shadow-stage log.  It
    never calls editorial, media, publisher, scheduler, or any model gateway.
    """

    def __init__(
        self,
        *,
        database: V2Database,
        project_root,
        config: FactualShadowConfig,
        factual_core: FactualCore | None = None,
    ) -> None:
        self.database = database
        self.project_root = project_root
        self.config = config
        self.factual_core = factual_core or StrategyBFactualExtractor(project_root)

    def process_many(self, values: Iterable[FactualShadowInput]) -> tuple[FactualShadowResult, ...]:
        return tuple(self.process(value) for value in values)

    def process(self, value: FactualShadowInput) -> FactualShadowResult:
        if not self.config.factual_shadow_enabled:
            return FactualShadowResult(
                story_id=value.story_id, processed_at=None,
                factual_core_version=FACTUAL_CORE_VERSION,
                factual_core_freeze_commit=FACTUAL_CORE_FREEZE_COMMIT,
                decision="disabled", source_event_id=None,
            )
        self.database.initialise()
        event = SourceEventInput(
            platform=value.platform,
            source_account=value.source_id,
            external_message_id=value.external_message_id,
            source_url=value.source_url,
            original_text=value.source_text,
            original_media_url=None,
            published_at=value.published_at,
            collected_at=value.collected_at,
            content_hash=hashlib.sha256(value.source_text.encode("utf-8")).hexdigest(),
        )
        story_id, source_event_id, event_created = StoryIngestService(self.database).ingest(
            event, story_id=value.story_id, dedupe_key=event.content_hash,
        )
        if not event_created:
            existing = self._load(story_id)
            if existing is not None:
                return self._result(existing, deduplicated=True)

        started = perf_counter()
        try:
            parsed = self.factual_core.parse(StrategyBSourceEnvelope(
                story_id=story_id,
                source_event_id=str(source_event_id),
                source_id=value.source_id,
                source_role=self._source_role(value.source_category),
                published_at=value.published_at,
                language=value.source_language,
                text=value.source_text,
            ))
            latency_ms = parsed.latency_ms
            if parsed.route_decision is RouteDecision.BLOCKED:
                payload = self._blocked_payload(
                    story_id, source_event_id, value, parsed.blocking_reasons, latency_ms,
                )
            elif parsed.event is None:
                payload = self._error_payload(
                    story_id, source_event_id, value, "MISSING_ROUTED_EVENT", latency_ms,
                )
            else:
                validation_error = self._validate_event(parsed.event, value.source_text, source_event_id)
                payload = (
                    self._error_payload(story_id, source_event_id, value, validation_error, latency_ms)
                    if validation_error
                    else self._routed_payload(story_id, source_event_id, value, parsed.event, latency_ms)
                )
        except Exception as exc:  # isolate one factual-core failure from later stories
            latency_ms = (perf_counter() - started) * 1000
            error_code = f"FACTUAL_CORE_EXCEPTION:{type(exc).__name__}"
            LOGGER.warning("shadow factual error story_id=%s stage=factual_core error=%s", story_id, error_code)
            payload = self._error_payload(story_id, source_event_id, value, error_code, latency_ms)
        self._save(payload)
        return self._result(payload)

    @staticmethod
    def _source_role(category: str) -> SourceRole:
        try:
            return SourceRole(category)
        except ValueError:
            return SourceRole.UNKNOWN

    @staticmethod
    def _validate_event(event, source_text: str, source_event_id: int) -> str | None:
        for span in event.evidence_spans:
            if (
                span.source_event_id != str(source_event_id)
                or source_text[span.start_offset:span.end_offset] != span.source_text
            ):
                return "FACTUAL_EVIDENCE_VALIDATION_FAILED"
        return None

    def _routed_payload(self, story_id: str, source_event_id: int, value: FactualShadowInput, event, latency_ms: float) -> dict[str, object]:
        entities = [event.subject.entity_id]
        from_club_id: str | None = None
        to_club_ids: list[str] = []
        direction: str | None = None
        amount = getattr(event, "amount", None)
        contract_until: str | None = None
        if isinstance(event, TransferEvent):
            from_club_id, to_club_ids, direction = event.from_club.entity_id, [event.to_club.entity_id], "from_to"
            entities.extend((from_club_id, event.to_club.entity_id))
            contract_until = event.contract.new_expiry if event.contract else None
        elif isinstance(event, TransferInterestEvent):
            from_club_id = event.current_club.entity_id if event.current_club else None
            to_club_ids, direction = [club.entity_id for club in event.interested_clubs], "interest"
            entities.extend(to_club_ids)
            if from_club_id:
                entities.append(from_club_id)
        elif isinstance(event, ContractEvent):
            from_club_id, direction, contract_until = event.club.entity_id, "contract", event.terms.new_expiry
            entities.append(event.club.entity_id)
        elif isinstance(event, PlayerLifecycleEvent):
            direction = "lifecycle"
            for club in (event.club, event.previous_loan_club, event.parent_club):
                if club is not None:
                    entities.append(club.entity_id)
            if event.previous_loan_club:
                from_club_id = event.previous_loan_club.entity_id
            if event.parent_club:
                to_club_ids.append(event.parent_club.entity_id)
        return self._base_payload(story_id, source_event_id, value, "routed", latency_ms) | {
            "event_family": event.event_type.value,
            "transfer_direction": direction,
            "player_entity_id": event.subject.entity_id,
            "entities": sorted(set(entities)),
            "from_club_id": from_club_id,
            "to_club_ids": sorted(set(to_club_ids)),
            "amount_value": amount.value if amount else None,
            "currency": amount.currency if amount else None,
            "contract_until": contract_until,
            "negated": False,
            "evidence": [span.model_dump(mode="json") for span in event.evidence_spans],
        }

    def _blocked_payload(self, story_id: str, source_event_id: int, value: FactualShadowInput, reasons, latency_ms: float) -> dict[str, object]:
        reason = reasons[0] if reasons else "FACTUAL_CORE_BLOCKED"
        return self._base_payload(story_id, source_event_id, value, "blocked", latency_ms) | {
            "block_reason": reason,
            "negated": any("NEGATED" in item for item in reasons),
        }

    def _error_payload(self, story_id: str, source_event_id: int, value: FactualShadowInput, error_code: str, latency_ms: float) -> dict[str, object]:
        return self._base_payload(story_id, source_event_id, value, "error", latency_ms) | {"error_code": error_code}

    @staticmethod
    def _base_payload(story_id: str, source_event_id: int, value: FactualShadowInput, decision: str, latency_ms: float) -> dict[str, object]:
        return {
            "story_id": story_id,
            "factual_core_version": FACTUAL_CORE_VERSION,
            "factual_core_freeze_commit": FACTUAL_CORE_FREEZE_COMMIT,
            "source_event_id": source_event_id,
            "processed_at": utc_iso(),
            "decision": decision,
            "event_family": None,
            "transfer_direction": None,
            "player_entity_id": None,
            "entities": [],
            "from_club_id": None,
            "to_club_ids": [],
            "amount_value": None,
            "currency": None,
            "contract_until": None,
            "negated": False,
            "evidence": [],
            "block_reason": None,
            "error_code": None,
            "latency_ms": max(0.0, latency_ms),
            "source_metadata": {
                "platform": value.platform,
                "source_id": value.source_id,
                "source_category": value.source_category,
                "source_language": value.source_language,
                "channel_id": value.channel_id,
                "sport": value.sport,
                "category": value.category,
                "source_url": value.source_url,
            },
        }

    def _save(self, payload: dict[str, object]) -> None:
        with self.database.transaction() as db:
            repository = StageCRepository(db)
            repository.save_factual_shadow_result(payload)
            decision = str(payload["decision"])
            stages = (
                ("D1_NORMALIZATION", "DISCOVERED", "NORMALIZED", "PASS"),
                ("D1_DEDUPE", "NORMALIZED", "DEDUPED", "PASS"),
                ("D1_FACTUAL_CORE", "DEDUPED", "FACTUAL_CORE_PARSED", decision.upper()),
                ("D1_SHADOW_ROUTING", "FACTUAL_CORE_PARSED", f"SHADOW_{decision.upper()}", decision.upper()),
            )
            for stage, previous, target, result in stages:
                repository.save_shadow_stage(
                    story_id=str(payload["story_id"]), story_version=1,
                    stage_name=stage, proposed_from_state=previous,
                    proposed_to_state=target, result=result,
                    blocking_codes=[str(payload["block_reason"])] if payload.get("block_reason") else [],
                    warning_codes=[], run_key=SHADOW_STAGE_RUN_KEY,
                )

    def _load(self, story_id: str) -> dict[str, object] | None:
        with self.database.transaction() as db:
            return StageCRepository(db).load_factual_shadow_result(story_id, FACTUAL_CORE_VERSION)

    @staticmethod
    def _result(payload: dict[str, object], *, deduplicated: bool = False) -> FactualShadowResult:
        return FactualShadowResult(
            story_id=str(payload["story_id"]), processed_at=str(payload["processed_at"]),
            factual_core_version=str(payload["factual_core_version"]),
            factual_core_freeze_commit=str(payload["factual_core_freeze_commit"]),
            decision=str(payload["decision"]), source_event_id=int(payload["source_event_id"]),
            event_family=payload.get("event_family"), transfer_direction=payload.get("transfer_direction"),
            player_entity_id=payload.get("player_entity_id"),
            entities=tuple(str(item) for item in payload.get("entities", [])),
            from_club_id=payload.get("from_club_id"),
            to_club_ids=tuple(str(item) for item in payload.get("to_club_ids", [])),
            amount_value=payload.get("amount_value"), currency=payload.get("currency"),
            contract_until=payload.get("contract_until"), negated=bool(payload.get("negated")),
            evidence=tuple(payload.get("evidence", [])), block_reason=payload.get("block_reason"),
            error_code=payload.get("error_code"), latency_ms=float(payload["latency_ms"]),
            deduplicated=deduplicated,
        )
