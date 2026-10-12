from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import V2Database
from .editorial_v2 import (
    CopyValidator,
    EditorialService,
    HeadlineValidator,
)
from .facts import HeadlineContract, RussianCopyContract, StructuredFacts
from .models import LlmTaskType
from .orchestration import (
    LlmOutputError,
    OllamaJsonClient,
    StructuredLlmOrchestrator,
)
from .pipeline import FactExtractionService, SourceEventContext, StageCPipeline
from .resolution import EntityResolver, SourceRecord, SourceRegistry
from .stage_c_repository import StageCRepository


MANIFEST_VERSION = "1.0"
SELECTION_RULES_VERSION = "stage-c1-real-selection-1.0"
COMPLETION_STAGE = "STAGE_C1_EVALUATION_COMPLETE"
PRODUCTION_DATABASE = Path("data/premwire.db")
DEFAULT_MODEL = "qwen3.5:4b"
DANGEROUS_ERROR_CODES = (
    "NEW_NUMBER_NOT_IN_SOURCE",
    "NEW_ENTITY_NOT_IN_SOURCE",
    "STATUS_STRENGTHENED",
    "NEGATION_LOST",
    "WRONG_FROM_TO_CLUB",
    "UNSUPPORTED_AMOUNT",
    "INVENTED_CONTRACT",
    "PREMWIRE_AS_FOREIGN_SOURCE",
    "UNRESOLVED_PRIMARY_SUBJECT",
    "COPY_FACT_MISMATCH",
)


class EvaluationManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: str
    source_event_id: int = Field(gt=0)
    legacy_item_id: int | None
    source_account: str
    registry_account: str
    source_role: str
    source_platform: str
    source_url: str
    source_text: str
    source_language: Literal["ru", "en", "mixed", "unknown"]
    published_at: str
    legacy_category: str | None
    legacy_state: str | None
    selection_bucket: Literal[
        "transfer_or_loan",
        "urgent_or_breaking",
        "rumour_or_tentative",
        "unsupported",
        "amount_or_contract",
        "complex_or_ambiguous",
    ]
    inclusion_reason: str


class EvaluationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: Literal["1.0"] = MANIFEST_VERSION
    source_kind: Literal["real_source_events"] = "real_source_events"
    selection_rules_version: str = SELECTION_RULES_VERSION
    created_at: str
    source_database_fingerprint: str
    items: list[EvaluationManifestItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_records(self) -> "EvaluationManifest":
        story_ids = [item.story_id for item in self.items]
        source_ids = [item.source_event_id for item in self.items]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("manifest contains duplicate story_id")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("manifest contains duplicate source_event_id")
        return self


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_language(text: str) -> Literal["ru", "en", "mixed", "unknown"]:
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cyrillic and latin:
        return "mixed" if min(cyrillic, latin) >= 10 else ("ru" if cyrillic > latin else "en")
    if cyrillic:
        return "ru"
    if latin:
        return "en"
    return "unknown"


def logical_database_fingerprint(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    details: dict[str, dict[str, Any]] = {}
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        tables = [
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            digest = hashlib.sha256()
            count = 0
            identifier = '"' + table.replace('"', '""') + '"'
            for row in db.execute(f'SELECT * FROM {identifier} ORDER BY rowid'):
                serialized = json.dumps(
                    dict(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                digest.update((serialized + "\n").encode("utf-8"))
                count += 1
            details[table] = {"count": count, "sha256": digest.hexdigest()}
    overall = hashlib.sha256(
        json.dumps(details, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return overall, details


class RealStorySelector:
    """Build a deterministic, real-source manifest from a Stage-B/Stage-C DB."""

    QUOTAS = {
        "transfer_or_loan": 8,
        "urgent_or_breaking": 7,
        "rumour_or_tentative": 5,
        "unsupported": 4,
        "amount_or_contract": 3,
        "complex_or_ambiguous": 3,
    }

    def __init__(self, *, project_root: Path) -> None:
        self.project_root = project_root
        source_payload = json.loads(
            (project_root / "config" / "sources.json").read_text(encoding="utf-8")
        )
        self.source_handles = {
            (str(item["platform"]).casefold(), str(item["id"]).casefold()): str(item["handle"])
            for item in source_payload["sources"]
            if item.get("enabled", False)
        }
        self.registry = SourceRegistry.load(
            project_root / "config" / "source_registry.json"
        )

    def build(self, database: Path) -> EvaluationManifest:
        overall, _ = logical_database_fingerprint(database)
        candidates = self._load_candidates(database)
        selected: list[EvaluationManifestItem] = []
        used: set[int] = set()
        # The order deliberately reserves difficult and rare examples first.
        for bucket in (
            "complex_or_ambiguous",
            "amount_or_contract",
            "unsupported",
            "rumour_or_tentative",
            "urgent_or_breaking",
            "transfer_or_loan",
        ):
            matches = [
                row
                for row in candidates
                if row["source_event_id"] not in used and self._matches(bucket, row)
            ]
            matches.sort(key=lambda row: self._rank(bucket, row), reverse=True)
            quota = self.QUOTAS[bucket]
            if bucket == "complex_or_ambiguous":
                matches = self._diverse_complex_examples(matches)
            if len(matches) < quota:
                raise RuntimeError(
                    f"not enough real candidates for {bucket}: {len(matches)}/{quota}"
                )
            for row in matches[:quota]:
                selected.append(self._to_item(row, bucket))
                used.add(int(row["source_event_id"]))
        if len(selected) != 30:
            raise AssertionError(f"selection must contain 30 items, got {len(selected)}")
        return EvaluationManifest(
            created_at=utc_now(),
            source_database_fingerprint=overall,
            items=selected,
        )

    @staticmethod
    def _diverse_complex_examples(
        matches: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        chosen: list[dict[str, Any]] = []
        predicates = (
            lambda text: any(
                marker in f" {text.casefold()} "
                for marker in (" not ", " no ", " не ", " нет ")
            ),
            lambda text: text.count("\n") >= 2,
            lambda text: len(text.strip()) < 90,
        )
        for predicate in predicates:
            candidate = next(
                (
                    row
                    for row in matches
                    if row not in chosen and predicate(str(row["original_text"]))
                ),
                None,
            )
            if candidate is not None:
                chosen.append(candidate)
        chosen.extend(row for row in matches if row not in chosen)
        return chosen

    def _load_candidates(self, database: Path) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(database)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT s.story_id, s.content_version, s.legacy_item_id,
                       se.id AS source_event_id, se.platform,
                       se.source_account, se.source_url, se.original_text,
                       se.published_at, i.card_category AS legacy_category,
                       i.state AS legacy_state
                FROM stories s
                JOIN story_sources ss
                  ON ss.story_id=s.story_id AND ss.is_primary=1
                JOIN source_events se ON se.id=ss.source_event_id
                LEFT JOIN items i ON i.id=s.legacy_item_id
                WHERE length(trim(se.original_text)) BETWEEN 35 AND 1800
                ORDER BY se.published_at DESC, se.id DESC
                """
            ).fetchall()
        candidates: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            platform = str(row["platform"]).casefold()
            source_id = str(row["source_account"]).casefold()
            registry_account = self.source_handles.get((platform, source_id))
            if registry_account is None:
                continue
            source_record = self.registry.resolve(platform, registry_account)
            if source_record is None:
                continue
            row["registry_account"] = registry_account
            row["source_role"] = source_record.source_role
            candidates.append(row)
        return candidates

    @staticmethod
    def _matches(bucket: str, row: dict[str, Any]) -> bool:
        text = str(row["original_text"]).casefold()
        category = str(row.get("legacy_category") or "").casefold()
        urgent = any(
            marker in text
            for marker in ("here we go", "breaking", "official", "официально", "срочно", "confirmed")
        )
        tentative = bool(
            re.search(
                r"\b(interested in|interest from|considering|could|may|"
                r"in talks|wants to|might|pursuit|может перейти|интересуется|"
                r"заинтересован|вед[её]т переговоры|рассматривает возможность|"
                r"хочет подписать)\b",
                text,
            )
        )
        transfer = bool(
            re.search(
                r"\b(joined|signed|signs|signing|transferred|moves|move to|"
                r"loan|loaned|free transfer|переш[её]л|перешла|переходит|"
                r"подписал|подписала|подписание|аренда|аренду|купил|купила|"
                r"согласовал сделку|согласовала сделку|договорились)\b",
                text,
            )
        )
        amount = any(symbol in text for symbol in ("€", "£", "$")) or any(
            marker in text
            for marker in (
                "million",
                " mln",
                "млн",
                "сумма трансфера",
                "contract until",
                "deal until",
                "контракт до",
                "до лета 20",
            )
        )
        negative = any(
            marker in f" {text} "
            for marker in (
                " not ",
                " no ",
                " won’t ",
                " won't ",
                " does not ",
                " do not ",
                " не ",
                " нет ",
                " не будет ",
            )
        )
        unsupported = category in {
            "quote",
            "stats",
            "statistics",
            "full_time",
            "lineup",
            "news",
        } or any(
            marker in text
            for marker in (
                " matches",
                " goals",
                " assists",
                "статист",
                "матч",
                "гол",
                "statement",
                "appointed",
                "назначен",
                "said:",
            )
        )
        multi_subject = text.count("\n") >= 2 and sum(
            text.count(marker)
            for marker in (" and ", " и ", " with ", " с ")
        ) >= 2
        if bucket == "complex_or_ambiguous":
            return negative or multi_subject or len(text.strip()) < 75
        if bucket == "amount_or_contract":
            return amount
        if bucket == "unsupported":
            return unsupported and not urgent
        if bucket == "rumour_or_tentative":
            return tentative and not urgent
        if bucket == "urgent_or_breaking":
            return urgent
        if bucket == "transfer_or_loan":
            extension = any(
                marker in text
                for marker in (
                    "signed a new ",
                    "new deal until",
                    "contract extension",
                    "продлил контракт",
                    "продлила контракт",
                )
            )
            return transfer and not tentative and not urgent and not extension
        return False

    @staticmethod
    def _rank(bucket: str, row: dict[str, Any]) -> tuple[int, int, int]:
        text = str(row["original_text"])
        folded = text.casefold()
        category = str(row.get("legacy_category") or "")
        published_bonus = int(str(row.get("legacy_state") or "") == "published")
        category_bonus = int(bool(category))
        if bucket == "complex_or_ambiguous":
            complexity = text.count("\n") + text.count(" and ") + text.count(" и ")
            return complexity, len(text), int(row["source_event_id"])
        if bucket == "transfer_or_loan":
            explicit_action = int(
                bool(
                    re.search(
                        r"\b(joined|signed|signs|loaned|переш[её]л|перешла|"
                        r"купил|купила|завершает подписание|contract signing)\b",
                        folded,
                    )
                )
            )
            return explicit_action, published_bonus, int(row["source_event_id"])
        return published_bonus, category_bonus, int(row["source_event_id"])

    def _to_item(
        self, row: dict[str, Any], bucket: str
    ) -> EvaluationManifestItem:
        return EvaluationManifestItem(
            story_id=str(row["story_id"]),
            source_event_id=int(row["source_event_id"]),
            legacy_item_id=(
                int(row["legacy_item_id"])
                if row["legacy_item_id"] is not None
                else None
            ),
            source_account=str(row["source_account"]),
            registry_account=str(row["registry_account"]),
            source_role=str(row["source_role"]),
            source_platform=str(row["platform"]),
            source_url=str(row["source_url"]),
            source_text=str(row["original_text"]),
            source_language=detect_language(str(row["original_text"])),
            published_at=str(row["published_at"]),
            legacy_category=(
                str(row["legacy_category"])
                if row["legacy_category"] is not None
                else None
            ),
            legacy_state=(
                str(row["legacy_state"])
                if row["legacy_state"] is not None
                else None
            ),
            selection_bucket=bucket,
            inclusion_reason=(
                f"Deterministic {bucket} candidate selected by "
                f"{SELECTION_RULES_VERSION}; legacy labels used only for discovery."
            ),
        )


def load_manifest(path: Path) -> EvaluationManifest:
    if not path.is_file():
        raise ValueError(f"manifest does not exist: {path}")
    manifest = EvaluationManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.source_kind != "real_source_events":
        raise ValueError("fixture manifests are forbidden")
    if len(manifest.items) > 50:
        raise ValueError("evaluation is limited to 50 stories")
    return manifest


def default_scheduler_probe() -> str:
    command = (
        "$t=Get-ScheduledTask -TaskName 'PremWire Football Monitor' "
        "-ErrorAction Stop; $t.State.ToString()"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("cannot verify PremWire Football Monitor state")
    return completed.stdout.strip()


def assert_safe_evaluation(
    *,
    project_root: Path,
    database: Path,
    manifest_path: Path,
    scheduler_probe: Callable[[], str] = default_scheduler_probe,
) -> EvaluationManifest:
    production = (project_root / PRODUCTION_DATABASE).resolve()
    selected_database = database.resolve()
    if selected_database == production:
        raise ValueError("stage-c-real-eval refuses data/premwire.db")
    if "backups" not in {part.casefold() for part in selected_database.parts}:
        raise ValueError("evaluation database must be an isolated backup")
    if scheduler_probe().casefold() != "disabled":
        raise ValueError("PremWire Football Monitor must be Disabled")
    manifest = load_manifest(manifest_path)
    with closing(sqlite3.connect(database)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("evaluation database quick_check failed")
    return manifest


def manifest_run_key(
    manifest: EvaluationManifest, *, model: str, seed: int
) -> str:
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "model": model,
        "seed": seed,
        "prompt_version": StructuredLlmOrchestrator.prompt_version,
        "schema_version": StructuredLlmOrchestrator.schema_version,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


class StageC1RealEvaluator:
    """Opt-in Stage C evaluation. Writes only to the supplied isolated DB."""

    def __init__(
        self,
        *,
        project_root: Path,
        database: Path,
        model: str = DEFAULT_MODEL,
        seed: int = 42,
        client: Any | None = None,
    ) -> None:
        self.project_root = project_root
        self.database = database
        self.model = model
        self.seed = seed
        settings = json.loads(
            (project_root / "config" / "settings.json").read_text(encoding="utf-8")
        )
        self.base_url = str(settings["editor"]["base_url"]).rstrip("/")
        self.timeout = int(settings["editor"]["timeout_seconds"])
        self.client = client or OllamaJsonClient(base_url=self.base_url, model=model)

    def run(self, manifest: EvaluationManifest) -> dict[str, Any]:
        started = time.perf_counter()
        run_key = manifest_run_key(manifest, model=self.model, seed=self.seed)
        V2Database(self.database).initialise()
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        repository = StageCRepository(db)
        orchestrator = StructuredLlmOrchestrator(
            client=self.client,
            repository=repository,
            model_name=self.model,
            temperature=0.0,
            seed=self.seed,
            timeout=self.timeout,
        )
        source_registry = SourceRegistry.load(
            self.project_root / "config" / "source_registry.json"
        )
        resolver = EntityResolver(project_root=self.project_root)
        extractor = FactExtractionService(
            orchestrator=orchestrator,
            source_registry=source_registry,
            entity_resolver=resolver,
        )
        pipeline = StageCPipeline(extractor=extractor, repository=repository)
        editorial = EditorialService(orchestrator)
        attempted = 0
        skipped = 0
        try:
            for item in manifest.items:
                if self._is_complete(db, item, run_key):
                    skipped += 1
                    continue
                attempted += 1
                story_started = time.perf_counter()
                blocking: list[str] = []
                warnings: list[str] = []
                result_status = "PASS"
                try:
                    context = SourceEventContext(
                        source_event_id=item.source_event_id,
                        story_id=item.story_id,
                        story_version=self._story_version(db, item.story_id),
                        platform=item.source_platform,
                        account=item.registry_account,
                        source_url=item.source_url,
                        published_at=item.published_at,
                        original_language=item.source_language,
                        original_text=item.source_text,
                    )
                    result = pipeline.process(context, run_key=run_key)
                    blocking.extend(result.fact_report.blocking_errors)
                    warnings.extend(result.fact_report.warnings)
                    blocking.extend(result.decision.blocking_reasons)
                    if result.fact_report.passed and result.decision.supported_category:
                        source_record = source_registry.resolve(
                            item.source_platform, item.registry_account
                        )
                        if source_record is None:
                            raise ValueError("UNKNOWN_SOURCE")
                        self._run_editorial(
                            db=db,
                            repository=repository,
                            editorial=editorial,
                            facts=result.facts,
                            fact_report=result.fact_report,
                            decision=result.decision,
                            item=item,
                            source_record=source_record,
                            story_version=context.story_version,
                        )
                    else:
                        result_status = "BLOCKED"
                except Exception as exc:  # one story must never stop the rest
                    result_status = "ERROR"
                    code = (
                        str(exc)
                        if isinstance(exc, (ValueError, LlmOutputError))
                        else type(exc).__name__
                    )
                    blocking.append(code)
                duration_ms = int((time.perf_counter() - story_started) * 1000)
                warnings.append(f"STORY_DURATION_MS:{duration_ms}")
                repository.save_shadow_stage(
                    story_id=item.story_id,
                    story_version=self._story_version(db, item.story_id),
                    stage_name=COMPLETION_STAGE,
                    proposed_from_state=None,
                    proposed_to_state="SHADOW_ONLY",
                    result=result_status,
                    blocking_codes=list(dict.fromkeys(blocking)),
                    warning_codes=list(dict.fromkeys(warnings)),
                    run_key=run_key,
                )
                db.commit()
            traces = self._build_traces(db, manifest, run_key)
        finally:
            db.close()
        metrics = calculate_metrics(
            traces,
            total_duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return {
            "run_key": run_key,
            "model": self.model,
            "seed": self.seed,
            "temperature": 0.0,
            "prompt_version": StructuredLlmOrchestrator.prompt_version,
            "schema_version": StructuredLlmOrchestrator.schema_version,
            "manifest_version": manifest.manifest_version,
            "manifest_items": len(manifest.items),
            "stories_attempted": attempted,
            "stories_skipped_as_complete": skipped,
            "metrics": metrics,
            "traces": traces,
        }

    @staticmethod
    def _story_version(db: sqlite3.Connection, story_id: str) -> int:
        row = db.execute(
            "SELECT content_version FROM stories WHERE story_id=?", (story_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"story not found: {story_id}")
        return int(row[0])

    @staticmethod
    def _is_complete(
        db: sqlite3.Connection,
        item: EvaluationManifestItem,
        run_key: str,
    ) -> bool:
        return (
            db.execute(
                """
                SELECT 1 FROM shadow_stage_results
                WHERE story_id=? AND stage_name=? AND run_key=?
                """,
                (item.story_id, COMPLETION_STAGE, run_key),
            ).fetchone()
            is not None
        )

    def _run_editorial(
        self,
        *,
        db: sqlite3.Connection,
        repository: StageCRepository,
        editorial: EditorialService,
        facts: StructuredFacts,
        fact_report: Any,
        decision: Any,
        item: EvaluationManifestItem,
        source_record: SourceRecord,
        story_version: int,
    ) -> None:
        before_id = int(
            db.execute("SELECT COALESCE(MAX(id),0) FROM llm_runs").fetchone()[0]
        )
        copy, headline, copy_report, run_ids = editorial.create(
            facts=facts,
            fact_report=fact_report,
            decision=decision,
            original_text=item.source_text,
            source_record=source_record,
            story_version=story_version,
        )
        headline_report = HeadlineValidator().validate(
            headline, facts=facts, source_record=source_record
        )
        llm_rows = db.execute(
            "SELECT * FROM llm_runs WHERE id>? ORDER BY id", (before_id,)
        ).fetchall()
        first_copy = self._first_contract(llm_rows, "EDITORIAL_COPY_RU", RussianCopyContract)
        first_copy_report = (
            CopyValidator().validate(
                first_copy.post_text,
                facts=facts,
                category=decision.template_intent or "",
            )
            if first_copy is not None
            else None
        )
        first_headline = self._first_contract(
            llm_rows, "CARD_HEADLINE_RU", HeadlineContract
        )
        first_headline_report = (
            HeadlineValidator().validate(
                first_headline, facts=facts, source_record=source_record
            )
            if first_headline is not None
            else None
        )
        validation_payload = {
            "copy_final": asdict(copy_report),
            "headline_final": asdict(headline_report),
            "copy_first_pass": (
                asdict(first_copy_report) if first_copy_report is not None else None
            ),
            "headline_first_pass": (
                asdict(first_headline_report)
                if first_headline_report is not None
                else None
            ),
            "headline_fallback_used": bool(
                first_headline_report is not None and not first_headline_report.passed
            ),
        }
        repository.save_editorial(
            story_id=facts.story_id,
            story_version=story_version,
            language="ru",
            post_text=copy.post_text,
            primary_title=headline.primary_title,
            secondary_title=headline.secondary_title,
            source_label=headline.source_label,
            validation_status=(
                "VALID" if copy_report.passed and headline_report.passed else "INVALID"
            ),
            validation_report=validation_payload,
            llm_run_ids=list(run_ids),
        )

    @staticmethod
    def _first_contract(
        rows: list[sqlite3.Row], task_type: str, contract: type[BaseModel]
    ) -> BaseModel | None:
        for row in rows:
            if row["task_type"] != task_type or row["validation_status"] != "VALID":
                continue
            try:
                return contract.model_validate_json(row["parsed_response_json"])
            except Exception:
                return None
        return None

    def _build_traces(
        self,
        db: sqlite3.Connection,
        manifest: EvaluationManifest,
        run_key: str,
    ) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        for item in manifest.items:
            version = self._story_version(db, item.story_id)
            llm_runs = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT id,task_type,model_name,prompt_version,schema_version,
                           temperature,seed,input_hash,raw_response,
                           parsed_response_json,validation_status,error_code,
                           duration_ms,attempt_number,created_at
                    FROM llm_runs WHERE story_id=? AND story_version=?
                    ORDER BY id
                    """,
                    (item.story_id, version),
                )
            ]
            fact_row = db.execute(
                """
                SELECT extracted_facts_json,extraction_status,llm_run_id
                FROM fact_extractions WHERE story_id=? AND story_version=?
                ORDER BY id DESC LIMIT 1
                """,
                (item.story_id, version),
            ).fetchone()
            validation_row = db.execute(
                """
                SELECT passed,blocking_errors_json,warnings_json,
                       field_results_json,validator_version
                FROM fact_validation_reports
                WHERE story_id=? AND story_version=?
                ORDER BY id DESC LIMIT 1
                """,
                (item.story_id, version),
            ).fetchone()
            classification_row = db.execute(
                """
                SELECT decision_json,classifier_version
                FROM classification_decisions
                WHERE story_id=? AND story_version=?
                ORDER BY id DESC LIMIT 1
                """,
                (item.story_id, version),
            ).fetchone()
            editorial_row = db.execute(
                """
                SELECT post_text,primary_title,secondary_title,source_label,
                       validation_status,validation_report_json,llm_run_ids
                FROM editorial_outputs
                WHERE story_id=? AND story_version=? AND language='ru'
                """,
                (item.story_id, version),
            ).fetchone()
            completion = db.execute(
                """
                SELECT result,blocking_codes,warning_codes
                FROM shadow_stage_results
                WHERE story_id=? AND story_version=? AND stage_name=? AND run_key=?
                """,
                (item.story_id, version, COMPLETION_STAGE, run_key),
            ).fetchone()
            completion_warnings = (
                self._json_or_none(completion["warning_codes"]) if completion else []
            ) or []
            story_duration_ms = next(
                (
                    int(code.split(":", 1)[1])
                    for code in completion_warnings
                    if isinstance(code, str)
                    and code.startswith("STORY_DURATION_MS:")
                ),
                None,
            )
            parsed_llm_runs = []
            for row in llm_runs:
                parsed_response_json = row.pop("parsed_response_json")
                parsed_llm_runs.append(
                    {
                        **row,
                        "parsed_response": self._json_or_none(
                            parsed_response_json
                        ),
                    }
                )
            traces.append(
                {
                    "manifest": item.model_dump(mode="json"),
                    "story_version": version,
                    "llm_runs": parsed_llm_runs,
                    "story_duration_ms": story_duration_ms,
                    "structured_facts": (
                        self._json_or_none(fact_row["extracted_facts_json"])
                        if fact_row
                        else None
                    ),
                    "fact_extraction_status": (
                        fact_row["extraction_status"] if fact_row else None
                    ),
                    "fact_validation": (
                        {
                            "passed": bool(validation_row["passed"]),
                            "blocking_errors": self._json_or_none(
                                validation_row["blocking_errors_json"]
                            ),
                            "warnings": self._json_or_none(
                                validation_row["warnings_json"]
                            ),
                            "field_results": self._json_or_none(
                                validation_row["field_results_json"]
                            ),
                            "validator_version": validation_row["validator_version"],
                        }
                        if validation_row
                        else None
                    ),
                    "classification": (
                        {
                            **(self._json_or_none(classification_row["decision_json"]) or {}),
                            "classifier_version": classification_row["classifier_version"],
                        }
                        if classification_row
                        else None
                    ),
                    "editorial": (
                        {
                            "post_text": editorial_row["post_text"],
                            "primary_title": editorial_row["primary_title"],
                            "secondary_title": editorial_row["secondary_title"],
                            "source_label": editorial_row["source_label"],
                            "validation_status": editorial_row["validation_status"],
                            "validation_report": self._json_or_none(
                                editorial_row["validation_report_json"]
                            ),
                            "llm_run_ids": self._json_or_none(
                                editorial_row["llm_run_ids"]
                            ),
                        }
                        if editorial_row
                        else None
                    ),
                    "completion": (
                        {
                            "result": completion["result"],
                            "blocking_codes": self._json_or_none(
                                completion["blocking_codes"]
                            ),
                            "warning_codes": completion_warnings,
                        }
                        if completion
                        else None
                    ),
                }
            )
        return traces

    @staticmethod
    def _json_or_none(value: str | None) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.9999)))
    return float(ordered[index])


def calculate_metrics(
    traces: list[dict[str, Any]], *, total_duration_ms: int
) -> dict[str, Any]:
    count = len(traces)
    fact_pass = evidence_pass = resolved = supported = 0
    route_counts = Counter()
    unsupported = ambiguous = 0
    copy_count = copy_first = copy_rewrite = copy_failure = 0
    headline_first = headline_fallback = headline_failure = 0
    valid_first = repaired = final_json_failure = 0
    block_counts: Counter[str] = Counter()
    dangerous: Counter[str] = Counter()
    task_latencies: dict[str, list[int]] = defaultdict(list)
    for trace in traces:
        runs = trace["llm_runs"]
        fact_attempts = [
            row
            for row in runs
            if row["task_type"] in {"FACT_EXTRACTION", "JSON_REPAIR"}
        ]
        first = next(
            (
                row
                for row in fact_attempts
                if row["task_type"] == "FACT_EXTRACTION"
                and row["attempt_number"] == 1
            ),
            None,
        )
        if first and first["validation_status"] == "VALID":
            valid_first += 1
        if any(row["task_type"] == "JSON_REPAIR" for row in fact_attempts):
            repaired += 1
        if trace["structured_facts"] is None:
            final_json_failure += 1
        for row in runs:
            task_latencies[row["task_type"]].append(int(row["duration_ms"]))
        validation = trace["fact_validation"]
        errors = list(validation["blocking_errors"] if validation else [])
        if validation and validation["passed"]:
            fact_pass += 1
        if validation and not any(
            code.startswith(("INVALID_EVIDENCE", "MISSING_EVIDENCE"))
            or code in {"AMOUNT_WITHOUT_EVIDENCE", "CONTRACT_WITHOUT_EVIDENCE"}
            for code in errors
        ):
            evidence_pass += 1
        facts = trace["structured_facts"]
        if facts and facts["subject"]["resolution_status"] == "resolved":
            resolved += 1
        classification = trace["classification"] or {}
        if classification.get("supported_category"):
            supported += 1
            route = classification.get("template_intent")
            route_counts[str(route)] += 1
        blocking = list(classification.get("blocking_reasons") or [])
        blocking.extend(errors)
        blocking.extend((trace.get("completion") or {}).get("blocking_codes") or [])
        for code in dict.fromkeys(blocking):
            block_counts[code] += 1
        if "BLOCKED_UNSUPPORTED_CATEGORY" in blocking:
            unsupported += 1
        if "BLOCKED_AMBIGUOUS_CATEGORY" in blocking:
            ambiguous += 1
        editorial = trace["editorial"]
        if editorial:
            copy_count += 1
            report = editorial.get("validation_report") or {}
            first_copy = report.get("copy_first_pass")
            if first_copy and first_copy.get("passed"):
                copy_first += 1
            copy_runs = [
                row for row in runs if row["task_type"] == "EDITORIAL_COPY_RU"
            ]
            if len(copy_runs) > 1:
                copy_rewrite += 1
            first_headline = report.get("headline_first_pass")
            if first_headline and first_headline.get("passed"):
                headline_first += 1
            if report.get("headline_fallback_used"):
                headline_fallback += 1
        elif classification.get("supported_category"):
            copy_failure += 1
            headline_failure += 1
        _count_dangerous_errors(dangerous, blocking, editorial)
    latency_metrics = {}
    for task, values in sorted(task_latencies.items()):
        latency_metrics[task] = {
            "count": len(values),
            "average_ms": round(sum(values) / len(values), 2),
            "median_ms": round(float(statistics.median(values)), 2),
            "p95_ms": round(_percentile(values, 0.95), 2),
        }
    material_durations = [
        trace.get("story_duration_ms") for trace in traces
    ]
    if material_durations and all(value is not None for value in material_durations):
        total_duration_ms = sum(int(value) for value in material_durations)
    return {
        "total_processed": count,
        "valid_json_first_attempt_rate": valid_first / count if count else 0.0,
        "json_repair_rate": repaired / count if count else 0.0,
        "json_final_failure_rate": final_json_failure / count if count else 0.0,
        "fact_validation_pass_rate": fact_pass / count if count else 0.0,
        "evidence_validation_pass_rate": evidence_pass / count if count else 0.0,
        "entity_resolution_rate": resolved / count if count else 0.0,
        "classification_supported_rate": supported / count if count else 0.0,
        "transfer_route_count": route_counts["transfer_v2"],
        "loan_route_count": route_counts["loan_v2"],
        "breaking_route_count": route_counts["breaking_v2"],
        "unsupported_count": unsupported,
        "ambiguous_count": ambiguous,
        "copy_generation_count": copy_count,
        "copy_first_pass_rate": copy_first / copy_count if copy_count else 0.0,
        "copy_rewrite_rate": copy_rewrite / copy_count if copy_count else 0.0,
        "copy_final_failure_rate": copy_failure / supported if supported else 0.0,
        "headline_first_pass_rate": headline_first / copy_count if copy_count else 0.0,
        "headline_fallback_rate": headline_fallback / copy_count if copy_count else 0.0,
        "headline_final_failure_rate": headline_failure / supported if supported else 0.0,
        "blocking_distribution": dict(sorted(block_counts.items())),
        "dangerous_errors": {
            code: dangerous.get(code, 0) for code in DANGEROUS_ERROR_CODES
        },
        "latency_by_task": latency_metrics,
        "total_duration_ms": total_duration_ms,
    }


def _count_dangerous_errors(
    counter: Counter[str],
    blocking: list[str],
    editorial: dict[str, Any] | None,
) -> None:
    block_set = set(blocking)
    if any(code in block_set for code in ("COPY_INVENTED_NUMBER",)):
        counter["NEW_NUMBER_NOT_IN_SOURCE"] += 1
    if any(code.startswith("UNRESOLVED_ENTITY") for code in block_set):
        counter["NEW_ENTITY_NOT_IN_SOURCE"] += 1
    if "STATUS_OVERSTATED" in block_set:
        counter["STATUS_STRENGTHENED"] += 1
    if "NEGATION_LOST" in block_set or "COPY_NEGATION_LOST" in block_set:
        counter["NEGATION_LOST"] += 1
    if "FROM_TO_CLUB_IDENTICAL" in block_set:
        counter["WRONG_FROM_TO_CLUB"] += 1
    if any("AMOUNT" in code for code in block_set):
        counter["UNSUPPORTED_AMOUNT"] += 1
    if "CONTRACT_WITHOUT_EVIDENCE" in block_set:
        counter["INVENTED_CONTRACT"] += 1
    if "PREMWIRE_NOT_PRIMARY_SOURCE" in block_set:
        counter["PREMWIRE_AS_FOREIGN_SOURCE"] += 1
    if "UNRESOLVED_PRIMARY_SUBJECT" in block_set:
        counter["UNRESOLVED_PRIMARY_SUBJECT"] += 1
    if editorial:
        report = editorial.get("validation_report") or {}
        copy_errors = set((report.get("copy_final") or {}).get("errors") or [])
        if copy_errors & {"COPY_INVENTED_NUMBER", "COPY_NEGATION_LOST"}:
            counter["COPY_FACT_MISMATCH"] += 1


def write_evaluation_reports(
    *,
    result: dict[str, Any],
    manifest: EvaluationManifest,
    reports_dir: Path,
    stamp: str,
    safety_state: dict[str, Any],
) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"stage-c1-real-ollama-{stamp}"
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    md_path = base.with_suffix(".md")
    manual_path = reports_dir / f"stage-c1-manual-review-{stamp}.csv"
    payload = {
        "safety_state": safety_state,
        "manifest": manifest.model_dump(mode="json"),
        **result,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_summary_csv(csv_path, result["traces"])
    _write_manual_csv(manual_path, result["traces"])
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    return {
        "markdown": md_path,
        "json": json_path,
        "csv": csv_path,
        "manual_review": manual_path,
    }


def _write_summary_csv(path: Path, traces: list[dict[str, Any]]) -> None:
    fields = (
        "story_id",
        "source_event_id",
        "selection_bucket",
        "fact_validation_passed",
        "route",
        "copy_generated",
        "headline_generated",
        "blocking_codes",
        "total_llm_duration_ms",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for trace in traces:
            manifest = trace["manifest"]
            validation = trace["fact_validation"] or {}
            classification = trace["classification"] or {}
            completion = trace["completion"] or {}
            writer.writerow(
                {
                    "story_id": manifest["story_id"],
                    "source_event_id": manifest["source_event_id"],
                    "selection_bucket": manifest["selection_bucket"],
                    "fact_validation_passed": validation.get("passed"),
                    "route": classification.get("template_intent"),
                    "copy_generated": bool(trace["editorial"]),
                    "headline_generated": bool(
                        trace["editorial"]
                        and trace["editorial"].get("primary_title")
                    ),
                    "blocking_codes": "|".join(
                        completion.get("blocking_codes") or []
                    ),
                    "total_llm_duration_ms": sum(
                        int(row["duration_ms"]) for row in trace["llm_runs"]
                    ),
                }
            )


def _write_manual_csv(path: Path, traces: list[dict[str, Any]]) -> None:
    fields = (
        "story_id",
        "source_text",
        "extracted_subject",
        "extracted_event_type",
        "extracted_status",
        "extracted_from_club",
        "extracted_to_club",
        "extracted_amount",
        "validation_result",
        "route",
        "generated_copy",
        "primary_title",
        "secondary_title",
        "reviewer_facts_correct",
        "reviewer_status_correct",
        "reviewer_entities_correct",
        "reviewer_negation_preserved",
        "reviewer_copy_natural",
        "reviewer_copy_no_hallucinations",
        "reviewer_headline_correct",
        "reviewer_notes",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for trace in traces:
            facts = trace["structured_facts"] or {}
            clubs = facts.get("clubs") or {}
            editorial = trace["editorial"] or {}
            validation = trace["fact_validation"] or {}
            classification = trace["classification"] or {}
            writer.writerow(
                {
                    "story_id": trace["manifest"]["story_id"],
                    "source_text": trace["manifest"]["source_text"],
                    "extracted_subject": (facts.get("subject") or {}).get(
                        "canonical_name"
                    ),
                    "extracted_event_type": (facts.get("event") or {}).get(
                        "event_type"
                    ),
                    "extracted_status": (facts.get("event") or {}).get("status"),
                    "extracted_from_club": (clubs.get("from_club") or {}).get(
                        "canonical_name"
                    ),
                    "extracted_to_club": (clubs.get("to_club") or {}).get(
                        "canonical_name"
                    ),
                    "extracted_amount": (facts.get("deal") or {}).get("amount_raw"),
                    "validation_result": (
                        "PASS" if validation.get("passed") else "BLOCKED"
                    ),
                    "route": classification.get("template_intent"),
                    "generated_copy": editorial.get("post_text"),
                    "primary_title": editorial.get("primary_title"),
                    "secondary_title": editorial.get("secondary_title"),
                    **{
                        field: ""
                        for field in fields
                        if field.startswith("reviewer_")
                    },
                }
            )


def _render_markdown(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    lines = [
        "# Stage C.1 real Ollama shadow baseline",
        "",
        "## 1. Safety state",
        "",
        "```json",
        json.dumps(payload["safety_state"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 2. Model and contracts",
        "",
        f"- model: `{payload['model']}`",
        f"- temperature: `{payload['temperature']}`",
        f"- seed: `{payload['seed']}`",
        f"- prompt version: `{payload['prompt_version']}`",
        f"- schema version: `{payload['schema_version']}`",
        "- TRANSLATION_EN: not invoked",
        "",
        "## 3. Selection manifest",
        "",
    ]
    bucket_counts = Counter(
        item["selection_bucket"] for item in payload["manifest"]["items"]
    )
    for bucket, count in sorted(bucket_counts.items()):
        lines.append(f"- {bucket}: {count}")
    lines.extend(
        [
            "",
            "## 4. Metrics",
            "",
            "```json",
            json.dumps(metrics, ensure_ascii=False, indent=2),
            "```",
            "",
            "## 5. Blocking distribution",
            "",
        ]
    )
    for code, count in metrics["blocking_distribution"].items():
        lines.append(f"- `{code}`: {count}")
    lines.extend(["", "## 6. Dangerous errors", ""])
    for code, count in metrics["dangerous_errors"].items():
        lines.append(f"- `{code}`: {count}")
    lines.extend(["", "## 7. Repairs and deterministic fallbacks", ""])
    repair_items = [
        trace
        for trace in payload["traces"]
        if any(row["task_type"] == "JSON_REPAIR" for row in trace["llm_runs"])
    ]
    fallback_items = [
        trace
        for trace in payload["traces"]
        if (trace.get("editorial") or {})
        .get("validation_report", {})
        .get("headline_fallback_used")
    ]
    lines.append(f"- JSON repair cases: {len(repair_items)}")
    lines.append(f"- headline deterministic fallback cases: {len(fallback_items)}")
    lines.extend(["", "## 8. All evaluated materials", ""])
    for index, trace in enumerate(payload["traces"], 1):
        item = trace["manifest"]
        facts = trace["structured_facts"]
        lines.extend(
            [
                f"### {index}. {item['story_id']}",
                "",
                f"- bucket: `{item['selection_bucket']}`",
                f"- source: `{item['source_platform']}:{item['source_account']}`",
                f"- source event: `{item['source_event_id']}`",
                f"- legacy item: `{item['legacy_item_id']}`",
                "",
                "**Source text**",
                "",
                item["source_text"],
                "",
                "**Extracted facts**",
                "",
                "```json",
                json.dumps(facts, ensure_ascii=False, indent=2),
                "```",
                "",
                "**Validation / route / editorial**",
                "",
                "```json",
                json.dumps(
                    {
                        "fact_validation": trace["fact_validation"],
                        "classification": trace["classification"],
                        "editorial": trace["editorial"],
                        "completion": trace["completion"],
                        "llm_runs": trace["llm_runs"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
    return "\n".join(lines)
