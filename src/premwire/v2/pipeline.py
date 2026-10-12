from __future__ import annotations

import json
from dataclasses import dataclass, replace

from .facts import ClubRef, ResolutionStatus, SourceFacts, StructuredFacts, SubjectFacts
from .models import LlmTaskType
from .orchestration import StructuredLlmOrchestrator
from .resolution import EntityResolver, SourceRecord, SourceRegistry
from .routing import ClassificationRouter
from .stage_c_repository import StageCRepository
from .validation import FactValidationReport, FactValidator


@dataclass(frozen=True, slots=True)
class SourceEventContext:
    source_event_id: int
    story_id: str
    story_version: int
    platform: str
    account: str
    source_url: str
    published_at: str
    original_language: str
    original_text: str


@dataclass(frozen=True, slots=True)
class StageCProcessResult:
    facts: StructuredFacts
    fact_report: FactValidationReport
    decision: object
    llm_run_ids: tuple[int, ...]


def build_fact_extraction_prompt(context: SourceEventContext) -> str:
    return (
        "Return JSON only matching the schema. Extract only facts explicitly "
        "present in ORIGINAL_TEXT. Never use football knowledge or memory. "
        "Unknown values must be null. Every significant fact requires exact "
        "source_text and offsets. Do not infer. Do not convert currencies. "
        "HERE WE GO is confirmed_report, never official. BREAKING changes only "
        "priority. Entity IDs and trust fields are placeholders and will be "
        "resolved deterministically.\n"
        f"STORY_ID={context.story_id}\n"
        f"SOURCE_EVENT_ID={context.source_event_id}\n"
        f"ORIGINAL_TEXT={json.dumps(context.original_text, ensure_ascii=False)}"
    )


class FactExtractionService:
    def __init__(
        self,
        *,
        orchestrator: StructuredLlmOrchestrator,
        source_registry: SourceRegistry,
        entity_resolver: EntityResolver,
    ) -> None:
        self.orchestrator = orchestrator
        self.source_registry = source_registry
        self.entity_resolver = entity_resolver

    def extract(self, context: SourceEventContext) -> tuple[StructuredFacts, SourceRecord, tuple[int, ...]]:
        source_record = self.source_registry.resolve(context.platform, context.account)
        if source_record is None:
            raise ValueError("UNKNOWN_SOURCE")
        result = self.orchestrator.run_json(
            task_type=LlmTaskType.FACT_EXTRACTION,
            story_id=context.story_id,
            story_version=context.story_version,
            prompt=build_fact_extraction_prompt(context),
            contract=StructuredFacts,
        )
        candidate = result.value
        subject = self.entity_resolver.resolve(
            candidate.subject.canonical_name,
            entity_type=candidate.subject.entity_type.value,
        )

        payload = candidate.model_dump(mode="json")
        payload["story_id"] = context.story_id
        payload["source"] = SourceFacts(
            platform=context.platform,
            account=source_record.account,
            source_role=source_record.source_role,
            trust_tier=source_record.trust_tier,
            url=context.source_url,
            published_at=context.published_at,
            original_language=context.original_language,
        ).model_dump(mode="json")
        payload["subject"] = SubjectFacts(
            entity_id=subject.entity_id,
            canonical_name=subject.canonical_name,
            display_name_ru=subject.display_name_ru,
            surname_display=subject.surname_display,
            entity_type=candidate.subject.entity_type,
            position=candidate.subject.position,
            resolution_status=(
                ResolutionStatus.RESOLVED if subject.resolved
                else ResolutionStatus.UNRESOLVED
            ),
        ).model_dump(mode="json")
        for key in ("current", "from_club", "to_club"):
            club_payload = payload["clubs"].get(key)
            if club_payload is not None:
                payload["clubs"][key] = self._resolve_club(club_payload)
        payload["clubs"]["interested_clubs"] = [
            self._resolve_club(club)
            for club in payload["clubs"].get("interested_clubs", [])
        ]
        return StructuredFacts(**payload), source_record, result.run_ids

    def _resolve_club(self, club_payload: dict) -> dict:
        resolved = self.entity_resolver.resolve(
            str(club_payload["canonical_name"]), entity_type="club"
        )
        return ClubRef(
            entity_id=resolved.entity_id,
            canonical_name=resolved.canonical_name,
            display_name_ru=resolved.display_name_ru,
            resolution_status=(
                ResolutionStatus.RESOLVED if resolved.resolved
                else ResolutionStatus.UNRESOLVED
            ),
        ).model_dump(mode="json")


class StageCPipeline:
    """Shadow-only Stage C coordinator. It never changes stories.current_state."""

    def __init__(
        self,
        *,
        extractor: FactExtractionService,
        repository: StageCRepository,
    ) -> None:
        self.extractor = extractor
        self.repository = repository

    def process(self, context: SourceEventContext, *, run_key: str) -> StageCProcessResult:
        facts, source_record, run_ids = self.extractor.extract(context)
        self.repository.save_fact_extraction(
            story_id=context.story_id,
            story_version=context.story_version,
            payload=facts.model_dump(mode="json"),
            status="VALID_JSON",
            llm_run_id=run_ids[-1] if run_ids else None,
        )
        report = FactValidator().validate(
            facts,
            original_text=context.original_text,
            source_event_id=context.source_event_id,
            source_record=source_record,
            expected_story_id=context.story_id,
        )
        self.repository.save_validation_report(
            story_id=context.story_id,
            story_version=context.story_version,
            passed=report.passed,
            blocking_errors=report.blocking_errors,
            warnings=report.warnings,
            field_results=report.field_results,
            validator_version=report.validator_version,
        )
        decision = ClassificationRouter().classify(facts, report)
        self.repository.save_classification(
            story_id=context.story_id,
            story_version=context.story_version,
            decision=decision.model_dump(mode="json"),
            classifier_version=ClassificationRouter.version,
        )
        stages = [
            ("NORMALIZE", "DISCOVERED", "NORMALIZED"),
            ("DEDUPE", "NORMALIZED", "DEDUPED"),
            ("FACT_EXTRACTION", "DEDUPED", "FACTS_EXTRACTED"),
            (
                "FACT_VALIDATION",
                "FACTS_EXTRACTED",
                "FACTS_VALIDATED" if report.passed else "BLOCKED_FACTS_INVALID",
            ),
        ]
        for name, from_state, to_state in stages:
            self.repository.save_shadow_stage(
                story_id=context.story_id,
                story_version=context.story_version,
                stage_name=name,
                proposed_from_state=from_state,
                proposed_to_state=to_state,
                result="PASS" if report.passed or name != "FACT_VALIDATION" else "BLOCKED",
                blocking_codes=list(report.blocking_errors) if name == "FACT_VALIDATION" else [],
                warning_codes=list(report.warnings) if name == "FACT_VALIDATION" else [],
                run_key=run_key,
            )
        return StageCProcessResult(facts, report, decision, run_ids)
