from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .facts import (
    ClubRef,
    ClubsFacts,
    DealFacts,
    EntityType,
    EventFacts,
    Evidence,
    EvidenceType,
    SourceFacts,
    StructuredFacts,
    SubjectFacts,
    VisualSubjectType,
)
from .resolution import EntityResolver, SourceRegistry
from .routing import ClassificationRouter
from .stage_c_repository import StageCRepository
from .validation import FactValidator, parse_amount


@dataclass(frozen=True, slots=True)
class StageCShadowReport:
    run_key: str
    stories_processed: int
    valid_json_rate: float
    repair_rate: float
    final_llm_failure_rate: float
    fact_validation_pass_rate: float
    entity_resolution_rate: float
    supported_route_rate: float
    blocked_reasons: dict[str, int]
    amount_extraction_count: int
    amount_rejection_count: int
    negation_failures: int
    copy_validation_pass_rate: float
    headline_validation_pass_rate: float
    average_task_ms: dict[str, float]
    successful_examples: list[dict[str, Any]]
    blocked_examples: list[dict[str, Any]]


class FixtureFactsBuilder:
    def __init__(self, *, project_root: Path) -> None:
        self.entities = EntityResolver(project_root=project_root)
        self.sources = SourceRegistry.load(project_root / "config" / "source_registry.json")

    def build(self, case: dict[str, Any], story_id: str, source_event_id: int) -> tuple[StructuredFacts, Any]:
        text = str(case["text"])
        subject_surface = str(case.get("subject", "Example Player"))
        club_surface = str(case.get("club", "Manchester United"))
        subject = self.entities.resolve(subject_surface, entity_type="player")
        club = self.entities.resolve(club_surface, entity_type="club")
        source_account = str(case.get("source", "FabrizioRomano"))
        source = self.sources.resolve("x", source_account)
        if source is None:
            source = self.sources.resolve("x", "FabrizioRomano")
        assert source is not None

        evidence: list[Evidence] = []
        for field_name, surface in (
            ("subject.canonical_name", subject_surface),
            ("clubs.to_club", club_surface),
        ):
            start = text.find(surface)
            if start >= 0:
                evidence.append(Evidence(
                    field=field_name, source_text=surface, start_offset=start,
                    end_offset=start + len(surface), source_event_id=source_event_id,
                    evidence_type=EvidenceType.EXPLICIT,
                ))
        evidence.append(Evidence(
            field="primary_claim", source_text=text, start_offset=0,
            end_offset=len(text), source_event_id=source_event_id,
            evidence_type=(
                EvidenceType.EXPLICIT_NEGATION
                if " not " in f" {text.casefold()} " or " не " in f" {text.casefold()} "
                else EvidenceType.EXPLICIT
            ),
        ))

        deal = DealFacts(contract_until=case.get("contract_until"))
        amount_raw = case.get("amount") or case.get("amount_claim")
        if amount_raw:
            parsed = parse_amount(str(amount_raw))
            if parsed is not None:
                if case.get("amount"):
                    start = text.index(str(amount_raw))
                    evidence.append(Evidence(
                        field="deal.amount", source_text=str(amount_raw),
                        start_offset=start, end_offset=start + len(str(amount_raw)),
                        source_event_id=source_event_id,
                        evidence_type=EvidenceType.EXPLICIT,
                    ))
                deal = DealFacts(
                    amount_raw=str(amount_raw), amount_value=parsed.value,
                    currency=parsed.currency, amount_type="fixed",
                    contract_until=case.get("contract_until"),
                )
        primary_claim = text
        if case.get("lose_negation"):
            primary_claim = text.replace("do not ", "")
        facts = StructuredFacts(
            story_id=story_id,
            source=SourceFacts(
                platform="x", account=source.account, source_role=source.source_role,
                trust_tier=source.trust_tier, url=f"https://x.com/{source.account}/status/{source_event_id}",
                published_at="2026-07-29T00:00:00Z",
                original_language="ru" if any("а" <= c.casefold() <= "я" for c in text) else "en",
            ),
            event=EventFacts(
                event_type=case["event_type"], deal_type=case["deal_type"],
                status=case["status"], priority=case["priority"],
                visual_subject_type=VisualSubjectType.PLAYER,
            ),
            subject=SubjectFacts(
                entity_id=subject.entity_id, canonical_name=subject_surface,
                display_name_ru=subject.display_name_ru,
                surname_display=subject.surname_display,
                entity_type=EntityType.PLAYER,
                resolution_status="resolved" if subject.resolved else "unresolved",
            ),
            clubs=ClubsFacts(to_club=ClubRef(
                entity_id=club.entity_id, canonical_name=club_surface,
                display_name_ru=club.display_name_ru,
                resolution_status="resolved" if club.resolved else "unresolved",
            )),
            deal=deal,
            primary_claim=primary_claim,
            evidence=evidence,
        )
        return facts, source


class StageCShadowEvaluator:
    def __init__(self, *, database: Path, project_root: Path) -> None:
        self.database = database
        self.project_root = project_root

    def run_fixtures(
        self,
        *,
        fixture_path: Path,
        limit: int | None = None,
        seed: int = 42,
        only_category: str | None = None,
        story_id: str | None = None,
    ) -> StageCShadowReport:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        cases = list(payload["cases"])
        if story_id:
            cases = [case for case in cases if case["id"] == story_id]
        if only_category:
            cases = [case for case in cases if case["event_type"] == only_category]
        cases = sorted(cases, key=lambda item: hashlib.sha256(
            f"{seed}:{item['id']}".encode()
        ).hexdigest())
        if limit is not None:
            cases = cases[:limit]
        run_key = hashlib.sha256(
            json.dumps(
                {"fixture_version": payload["version"], "seed": seed,
                 "ids": [case["id"] for case in cases]},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:20]
        builder = FixtureFactsBuilder(project_root=self.project_root)
        successful: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        blocked_reasons: dict[str, int] = {}
        resolved = validated = routed = amount_count = amount_rejected = negations = 0

        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        repository = StageCRepository(db)
        try:
            for index, case in enumerate(cases, 1):
                fixture_story_id = f"fixture-stage-c-{case['id']}"
                db.execute(
                    """
                    INSERT OR IGNORE INTO stories(
                        story_id,current_state,content_version,dedupe_algorithm_version,
                        created_at,updated_at
                    ) VALUES (?, 'DISCOVERED', 1, 1, ?, ?)
                    """,
                    (fixture_story_id, self._now(), self._now()),
                )
                source_event_id = 1_000_000 + index
                facts, source = builder.build(case, fixture_story_id, source_event_id)
                if facts.subject.resolution_status.value == "resolved":
                    resolved += 1
                report = FactValidator().validate(
                    facts, original_text=case["text"],
                    source_event_id=source_event_id, source_record=source,
                    expected_story_id=fixture_story_id,
                )
                if report.passed:
                    validated += 1
                decision = ClassificationRouter().classify(facts, report)
                if decision.supported_category:
                    routed += 1
                if facts.deal.amount_value is not None:
                    amount_count += 1
                    if any("AMOUNT" in code for code in report.blocking_errors):
                        amount_rejected += 1
                if "NEGATION_LOST" in report.blocking_errors:
                    negations += 1
                reasons = list(decision.blocking_reasons)
                for reason in reasons:
                    blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
                example = {
                    "fixture_id": case["id"],
                    "source_text": case["text"],
                    "extracted_facts": facts.model_dump(mode="json"),
                    "validation_result": asdict(report),
                    "classification": decision.model_dump(mode="json"),
                    "generated_copy": None,
                    "headline": None,
                }
                (successful if decision.supported_category else blocked).append(example)
                repository.save_fact_extraction(
                    story_id=fixture_story_id, story_version=1,
                    payload=facts.model_dump(mode="json"), status="FIXTURE",
                    llm_run_id=None,
                )
                repository.save_validation_report(
                    story_id=fixture_story_id, story_version=1,
                    passed=report.passed, blocking_errors=report.blocking_errors,
                    warnings=report.warnings, field_results=report.field_results,
                    validator_version=report.validator_version,
                )
                repository.save_classification(
                    story_id=fixture_story_id, story_version=1,
                    decision=decision.model_dump(mode="json"),
                    classifier_version=ClassificationRouter.version,
                )
                repository.save_shadow_stage(
                    story_id=fixture_story_id, story_version=1,
                    stage_name="STAGE_C_CLASSIFICATION",
                    proposed_from_state="DISCOVERED",
                    proposed_to_state=(
                        "FACTS_VALIDATED" if decision.supported_category
                        else decision.blocking_reasons[0]
                    ),
                    result="PASS" if decision.supported_category else "BLOCKED",
                    blocking_codes=reasons, warning_codes=list(report.warnings),
                    run_key=run_key,
                )
            db.commit()
        finally:
            db.close()
        count = len(cases)
        return StageCShadowReport(
            run_key=run_key,
            stories_processed=count,
            valid_json_rate=1.0 if count else 0.0,
            repair_rate=0.0,
            final_llm_failure_rate=0.0,
            fact_validation_pass_rate=validated / count if count else 0.0,
            entity_resolution_rate=resolved / count if count else 0.0,
            supported_route_rate=routed / count if count else 0.0,
            blocked_reasons=dict(sorted(blocked_reasons.items())),
            amount_extraction_count=amount_count,
            amount_rejection_count=amount_rejected,
            negation_failures=negations,
            copy_validation_pass_rate=0.0,
            headline_validation_pass_rate=0.0,
            average_task_ms={},
            successful_examples=successful[:20],
            blocked_examples=blocked[:20],
        )

    def write_report(self, report: StageCShadowReport, reports_dir: Path) -> tuple[Path, Path]:
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_path = reports_dir / f"stage-c-shadow-{stamp}.json"
        md_path = reports_dir / f"stage-c-shadow-{stamp}.md"
        payload = asdict(report)
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(
            "# Stage C shadow evaluation\n\n"
            f"- run_key: `{report.run_key}`\n"
            f"- stories processed: {report.stories_processed}\n"
            f"- valid JSON rate: {report.valid_json_rate:.1%}\n"
            f"- fact validation pass rate: {report.fact_validation_pass_rate:.1%}\n"
            f"- entity resolution rate: {report.entity_resolution_rate:.1%}\n"
            f"- supported route rate: {report.supported_route_rate:.1%}\n"
            f"- blocked reasons: `{json.dumps(report.blocked_reasons, ensure_ascii=False)}`\n\n"
            "Fixture mode intentionally does not call LLM, renderer, Telegram or X.\n",
            encoding="utf-8",
        )
        return md_path, json_path

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
