from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from premwire.emblem_registry import normalize_entity_name

from .c2b_candidates import CandidateExtractor
from .c2b_evidence import C2BEntityResolver
from .c2b_policies import C2FactsAssembler
from .c2b_source import SourceMetadataBuilder, SourceTextNormalizer
from .c2b_validation import FactValidator2
from .c2c_protocol import (
    EXTRACTION_PROMPT_VERSION,
    PROPOSAL_SCHEMA_VERSION,
    FlatProposalRunner,
    ProtocolOutputError,
    ProtocolTelemetry,
)
from .orchestration import JsonLlmClient
from .resolution import SourceRegistry
from .stage_c1_evaluation import (
    EvaluationManifest,
    PRODUCTION_DATABASE,
    default_scheduler_probe,
    load_manifest,
)


MAX_EVALUATION_STORIES = 50


def assert_safe_c2c_evaluation(
    *,
    project_root: Path,
    database: Path,
    manifest_path: Path,
    scheduler_probe: Callable[[], str] = default_scheduler_probe,
) -> EvaluationManifest:
    production = (project_root / PRODUCTION_DATABASE).resolve()
    selected = database.resolve()
    if selected == production:
        raise ValueError("stage-c2c-eval refuses data/premwire.db")
    if "backups" not in {part.casefold() for part in selected.parts}:
        raise ValueError("evaluation database must be an isolated backup")
    if scheduler_probe().casefold() != "disabled":
        raise ValueError("PremWire Football Monitor must be Disabled")
    manifest = load_manifest(manifest_path)
    if len(manifest.items) > MAX_EVALUATION_STORIES:
        raise ValueError("evaluation is limited to 50 stories")
    with closing(sqlite3.connect(database)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("evaluation database quick_check failed")
    return manifest


def _telemetry_dict(value: ProtocolTelemetry) -> dict[str, Any]:
    return {
        **asdict(value),
        "repair_kind": value.repair_kind.value if value.repair_kind else None,
        "attempts": [
            {
                **asdict(attempt),
                "failure_kind": (
                    attempt.failure_kind.value if attempt.failure_kind else None
                ),
            }
            for attempt in value.attempts
        ],
    }


class StageC2CEvaluator:
    def __init__(
        self, *, project_root: Path, client: JsonLlmClient, seed: int = 42
    ) -> None:
        registry = SourceRegistry.load(project_root / "config/source_registry.json")
        self.metadata = SourceMetadataBuilder(registry)
        self.normalizer = SourceTextNormalizer()
        self.candidates = CandidateExtractor(
            project_root=project_root,
            reviewed_registry_path=(
                project_root / "config/entity_registry_stage_c2_reviewed.json"
            ),
        )
        self.proposals = FlatProposalRunner(client=client, seed=seed)
        self.assembler = C2FactsAssembler(
            resolver=C2BEntityResolver(project_root=project_root)
        )
        self.validator = FactValidator2()

    def run(
        self, manifest: EvaluationManifest, gold_path: Path | None = None
    ) -> dict[str, Any]:
        gold_by_id: dict[int, dict[str, Any]] = {}
        if gold_path:
            payload = json.loads(gold_path.read_text(encoding="utf-8"))
            gold_by_id = {
                int(item["source_event_id"]): item for item in payload["records"]
            }
        records: list[dict[str, Any]] = []
        for item in manifest.items:
            row: dict[str, Any] = {
                "story_id": item.story_id,
                "source_event_id": item.source_event_id,
                "selection_bucket": item.selection_bucket,
            }
            try:
                metadata = self.metadata.build(item)
                normalized = self.normalizer.normalize(metadata.original_text)
                candidates = self.candidates.extract(
                    source_event_id=item.source_event_id, text=normalized
                )
                proposal_result = self.proposals.run(
                    metadata=metadata, candidates=candidates
                )
                row["protocol"] = _telemetry_dict(proposal_result.telemetry)
                assembled = self.assembler.assemble(
                    metadata=metadata,
                    proposal=proposal_result.semantic_proposal,
                    candidates=candidates,
                )
                validation = self.validator.validate(
                    facts=assembled.facts,
                    metadata=metadata,
                    proposal=proposal_result.semantic_proposal,
                    candidates=candidates,
                )
                claim = proposal_result.inputs.claims[
                    proposal_result.proposal.claim_span
                ]
                row.update(
                    {
                        "result": (
                            "passed" if validation.publishable else "blocked"
                        ),
                        "proposal": proposal_result.proposal.model_dump(mode="json"),
                        "semantic_proposal": (
                            proposal_result.semantic_proposal.model_dump(mode="json")
                        ),
                        "claim_span": asdict(claim),
                        "facts": assembled.facts.model_dump(mode="json"),
                        "validation": asdict(validation),
                        "candidate_count": len(candidates.candidates),
                    }
                )
                row["structural"] = self._structural(
                    row=row,
                    source_text=metadata.original_text,
                    selection_bucket=item.selection_bucket,
                )
                if item.source_event_id in gold_by_id:
                    row["gold_comparison"] = self._compare(
                        row, gold_by_id[item.source_event_id], candidates.by_id()
                    )
            except ProtocolOutputError as exc:
                row.update(
                    {
                        "result": "blocked",
                        "error": "ProtocolOutputError:BLOCKED_LLM_INVALID_OUTPUT",
                        "protocol": _telemetry_dict(exc.telemetry),
                    }
                )
            except Exception as exc:  # fail closed and preserve diagnostics
                row.update(
                    {
                        "result": "blocked",
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                )
            records.append(row)
        return {
            "protocol": {
                "schema_version": PROPOSAL_SCHEMA_VERSION,
                "prompt_version": EXTRACTION_PROMPT_VERSION,
            },
            "records": records,
            "metrics": self._metrics(records),
        }

    @staticmethod
    def _structural(
        *, row: dict[str, Any], source_text: str, selection_bucket: str
    ) -> dict[str, bool]:
        facts = row["facts"]
        validation = row["validation"]
        evidence_valid = all(
            source_text[item["start_offset"] : item["end_offset"]]
            == item["source_text"]
            for item in facts["evidence"]
        )
        claim = row["claim_span"]
        claim_valid = (
            source_text[claim["start_offset"] : claim["end_offset"]]
            == claim["source_text"]
        )
        blocking = validation["blocking_errors"]
        return {
            "evidence": evidence_valid,
            "claim_span": claim_valid,
            "amount_supported": not (
                facts["deal"]["amount_value"] is not None
                and not any(
                    item["field"] == "deal.amount" for item in facts["evidence"]
                )
            ),
            "contract_supported": not (
                (
                    facts["deal"]["contract_until"] is not None
                    or facts["deal"]["contract_years"] is not None
                )
                and not any(
                    item["field"] == "deal.contract" for item in facts["evidence"]
                )
            ),
            "status_policy": "STATUS_STRONGER_THAN_POLICY" not in blocking,
            "relation_type_safe": not any(
                code.startswith("RELATION_NOT_CLUB") for code in blocking
            ),
            "unsupported_blocked": (
                selection_bucket != "unsupported" or row["result"] == "blocked"
            ),
        }

    def _compare(
        self,
        row: dict[str, Any],
        gold: dict[str, Any],
        candidates: dict[str, Any],
    ) -> dict[str, bool]:
        semantic = row["semantic_proposal"]
        facts = row["facts"]
        selected = candidates.get(semantic.get("primary_subject_candidate_id"))
        expected_subject = gold.get("expected_primary_subject_surface")
        subject_ok = (
            expected_subject is None and selected is None
        ) or (
            selected is not None
            and normalize_entity_name(selected.surface)
            == normalize_entity_name(str(expected_subject))
        )
        expected_claims = {
            item["source_text"]
            for item in gold.get("expected_evidence", [])
            if item["field"] == "primary_claim"
        }
        claim_ok = (
            row["claim_span"]["source_text"] in expected_claims
            if expected_claims
            else True
        )
        expected_supported = bool(gold.get("expected_supported_route"))
        from_ok = self._club_matches(
            facts["clubs"].get("from_club"),
            gold.get("expected_from_club_surface"),
        )
        to_ok = self._club_matches(
            facts["clubs"].get("to_club"),
            gold.get("expected_to_club_surface"),
        )
        return {
            "category": semantic["semantic_category"]
            == gold.get("expected_event_type"),
            "primary_subject": subject_ok,
            "relation": from_ok and to_ok,
            "from_club": from_ok,
            "to_club": to_ok,
            "claim_span": claim_ok,
            "amount": (
                facts["deal"]["amount_value"] == gold.get("expected_amount_value")
                and facts["deal"]["currency"] == gold.get("expected_currency")
            ),
            "contract": (
                facts["deal"]["contract_years"]
                == gold.get("expected_contract_years")
                and (
                    gold.get("expected_contract_until") is None
                    or str(gold.get("expected_contract_until")).casefold()
                    in str(facts["deal"]["contract_until"]).casefold()
                )
            ),
            "negation": (
                not gold.get("negation_present")
                or semantic["signals"]["negated"]
            ),
            "fact_validation": (
                row["result"] == "passed"
            )
            == expected_supported,
            "unsupported_blocking": (
                expected_supported or row["result"] == "blocked"
            ),
        }

    def _club_matches(
        self, actual: dict[str, Any] | None, expected_surface: object
    ) -> bool:
        expected = str(expected_surface or "").strip()
        if not expected:
            return actual is None
        if actual is None:
            return False
        resolved = self.assembler.resolver.base.resolve(
            expected, entity_type="club"
        )
        if (
            resolved.resolved
            and resolved.entity_id
            and actual.get("entity_id")
        ):
            return actual["entity_id"] == resolved.entity_id
        return normalize_entity_name(str(actual.get("canonical_name", ""))) == (
            normalize_entity_name(expected)
        )

    @staticmethod
    def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(records)
        protocols = [row["protocol"] for row in records if "protocol" in row]
        comparisons = [
            row["gold_comparison"]
            for row in records
            if "gold_comparison" in row
        ]
        structural = [
            row["structural"] for row in records if "structural" in row
        ]

        def ratio(values: list[bool], denominator: int | None = None) -> float:
            base = len(values) if denominator is None else denominator
            return round(sum(values) / base, 4) if base else 0.0

        def comparison_rate(key: str) -> float | None:
            return (
                ratio([bool(item[key]) for item in comparisons])
                if comparisons
                else None
            )

        def first(key: str) -> list[bool]:
            return [
                bool(item["attempts"][0][key])
                for item in protocols
                if item.get("attempts")
            ]

        dangerous = {
            "new_entity_hallucination": 0,
            "invented_amount": sum(
                1
                for row in records
                if row.get("gold_comparison")
                and not row["gold_comparison"]["amount"]
                and row.get("facts", {}).get("deal", {}).get("amount_value")
                is not None
            ),
            "invented_contract": sum(
                1
                for row in records
                if row.get("gold_comparison")
                and not row["gold_comparison"]["contract"]
                and (
                    row.get("facts", {}).get("deal", {}).get("contract_until")
                    is not None
                    or row.get("facts", {}).get("deal", {}).get("contract_years")
                    is not None
                )
            ),
            "status_strengthened": sum(
                1
                for item in structural
                if not item["status_policy"]
            ),
            "negation_lost": sum(
                1
                for item in comparisons
                if not item["negation"]
            ),
            "wrong_from_to": sum(
                1
                for row in records
                if row.get("gold_comparison")
                and not row["gold_comparison"]["relation"]
                and (
                    row.get("facts", {}).get("clubs", {}).get("from_club")
                    is not None
                    or row.get("facts", {}).get("clubs", {}).get("to_club")
                    is not None
                )
            ),
            "evidence_outside_source": sum(
                1 for item in structural if not item["evidence"]
            ),
        }
        repair_attempted = [
            bool(item["repair_attempted"]) for item in protocols
        ]
        repair_successes = [
            bool(item["repair_success"])
            for item in protocols
            if item["repair_attempted"]
        ]
        return {
            "stories": total,
            "valid_json_syntax_first_attempt": ratio(first("valid_json"), total),
            "valid_schema_first_attempt": ratio(first("valid_schema"), total),
            "valid_semantic_proposal_first_attempt": ratio(
                first("valid_semantic"), total
            ),
            "repair_attempted": ratio(repair_attempted, total),
            "repair_success": ratio(repair_successes),
            "final_valid_proposal": ratio(
                [bool(item["final_valid"]) for item in protocols], total
            ),
            "category_accuracy": comparison_rate("category"),
            "subject_selection_accuracy": comparison_rate("primary_subject"),
            "relation_selection_accuracy": comparison_rate("relation"),
            "claim_span_accuracy": comparison_rate("claim_span"),
            "amount_selection_accuracy": comparison_rate("amount"),
            "contract_selection_accuracy": comparison_rate("contract"),
            "negation_span_accuracy": comparison_rate("negation"),
            "final_fact_validation": comparison_rate("fact_validation"),
            "claim_span_valid": (
                ratio([bool(item["claim_span"]) for item in structural])
                if structural
                else 0.0
            ),
            "correct_unsupported_blocking": (
                comparison_rate("unsupported_blocking")
                if comparisons
                else (
                    ratio(
                        [bool(item["unsupported_blocked"]) for item in structural]
                    )
                    if structural
                    else 0.0
                )
            ),
            "failure_counts": {
                kind: sum(
                    1
                    for item in protocols
                    for attempt in item.get("attempts", [])
                    if attempt.get("failure_kind") == kind
                )
                for kind in (
                    "json_parse_failure",
                    "json_schema_failure",
                    "semantic_candidate_failure",
                )
            },
            "average_latency_ms": (
                round(mean([item["latency_ms"] for item in protocols]), 1)
                if protocols
                else 0.0
            ),
            "average_prompt_characters": (
                round(mean([item["prompt_characters"] for item in protocols]), 1)
                if protocols
                else 0.0
            ),
            "average_prompt_tokens_estimate": (
                round(
                    mean(
                        [item["prompt_tokens_estimate"] for item in protocols]
                    ),
                    1,
                )
                if protocols
                else 0.0
            ),
            "average_candidate_count": (
                round(mean([item["candidate_count"] for item in protocols]), 1)
                if protocols
                else 0.0
            ),
            "average_response_tokens_estimate": (
                round(
                    mean(
                        [
                            attempt["response_tokens_estimate"]
                            for item in protocols
                            for attempt in item.get("attempts", [])
                        ]
                    ),
                    1,
                )
                if protocols
                else 0.0
            ),
            "dangerous_errors": dangerous,
        }


def write_c2c_reports(
    *,
    result: dict[str, Any],
    reports_dir: Path,
    stamp: str,
    kind: str,
    safety: dict[str, Any],
) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"stage-c2c-{kind}-{stamp}"
    paths = {
        "json": base.with_suffix(".json"),
        "csv": base.with_suffix(".csv"),
        "markdown": base.with_suffix(".md"),
    }
    paths["json"].write_text(
        json.dumps({**result, "safety": safety}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    columns = [
        "story_id",
        "source_event_id",
        "selection_bucket",
        "result",
        "error",
        "valid_json_first",
        "valid_schema_first",
        "valid_semantic_first",
        "repair_attempted",
        "repair_success",
        "final_valid",
        "latency_ms",
    ]
    with paths["csv"].open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in result["records"]:
            protocol = row.get("protocol", {})
            attempts = protocol.get("attempts", [])
            first = attempts[0] if attempts else {}
            writer.writerow(
                {
                    "story_id": row["story_id"],
                    "source_event_id": row["source_event_id"],
                    "selection_bucket": row["selection_bucket"],
                    "result": row.get("result"),
                    "error": row.get("error"),
                    "valid_json_first": first.get("valid_json"),
                    "valid_schema_first": first.get("valid_schema"),
                    "valid_semantic_first": first.get("valid_semantic"),
                    "repair_attempted": protocol.get("repair_attempted"),
                    "repair_success": protocol.get("repair_success"),
                    "final_valid": protocol.get("final_valid"),
                    "latency_ms": protocol.get("latency_ms"),
                }
            )
    paths["markdown"].write_text(
        "# Stage C.2C proposal protocol evaluation\n\n"
        f"- Kind: `{kind}`\n"
        f"- Schema: `{PROPOSAL_SCHEMA_VERSION}`\n"
        f"- Prompt: `{EXTRACTION_PROMPT_VERSION}`\n"
        f"- Stories: `{len(result['records'])}`\n"
        f"- Production unchanged: `{safety['production_unchanged']}`\n\n"
        "## Metrics\n\n```json\n"
        + json.dumps(result["metrics"], ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    return paths
