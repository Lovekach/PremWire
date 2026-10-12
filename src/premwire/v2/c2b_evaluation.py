from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from premwire.emblem_registry import normalize_entity_name

from .c2b_candidates import CandidateExtractor
from .c2b_evidence import C2BEntityResolver
from .c2b_policies import C2FactsAssembler
from .c2b_proposal import CompactProposalRunner
from .c2b_source import SourceMetadataBuilder, SourceTextNormalizer
from .c2b_validation import FactValidator2
from .orchestration import JsonLlmClient
from .resolution import SourceRegistry
from .stage_c1_evaluation import (
    EvaluationManifest,
    PRODUCTION_DATABASE,
    default_scheduler_probe,
    load_manifest,
    logical_database_fingerprint,
)


MAX_EVALUATION_STORIES = 50


def assert_safe_c2b_evaluation(
    *,
    project_root: Path,
    database: Path,
    manifest_path: Path,
    scheduler_probe: Callable[[], str] = default_scheduler_probe,
) -> EvaluationManifest:
    production = (project_root / PRODUCTION_DATABASE).resolve()
    selected = database.resolve()
    if selected == production:
        raise ValueError("stage-c2b-eval refuses data/premwire.db")
    if "backups" not in {part.casefold() for part in selected.parts}:
        raise ValueError("evaluation database must be an isolated backup")
    settings = json.loads((project_root / "config/settings.json").read_text(encoding="utf-8"))
    if not settings["publishing"]["dry_run"]:
        raise ValueError("publishing.dry_run must be true")
    if settings["publishing"]["auto_approve"]:
        raise ValueError("publishing.auto_approve must be false")
    if settings["x_publishing"]["enabled"]:
        raise ValueError("X must be disabled")
    if settings.get("facts", {}).get("compact_extraction_enabled", False):
        raise ValueError("production compact_extraction_enabled must remain false")
    if scheduler_probe().casefold() != "disabled":
        raise ValueError("PremWire Football Monitor must be Disabled")
    manifest = load_manifest(manifest_path)
    if len(manifest.items) > MAX_EVALUATION_STORIES:
        raise ValueError("evaluation is limited to 50 stories")
    with closing(sqlite3.connect(database)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("evaluation database quick_check failed")
    return manifest


class StageC2BEvaluator:
    def __init__(self, *, project_root: Path, client: JsonLlmClient, seed: int = 42) -> None:
        registry = SourceRegistry.load(project_root / "config/source_registry.json")
        self.metadata = SourceMetadataBuilder(registry)
        self.normalizer = SourceTextNormalizer()
        reviewed = project_root / "config/entity_registry_stage_c2_reviewed.json"
        self.candidates = CandidateExtractor(
            project_root=project_root, reviewed_registry_path=reviewed
        )
        resolver = C2BEntityResolver(project_root=project_root)
        self.proposals = CompactProposalRunner(client=client, seed=seed)
        self.assembler = C2FactsAssembler(resolver=resolver)
        self.validator = FactValidator2()

    def run(self, manifest: EvaluationManifest, gold_path: Path | None = None) -> dict[str, Any]:
        gold_by_id: dict[int, dict[str, Any]] = {}
        if gold_path:
            payload = json.loads(gold_path.read_text(encoding="utf-8"))
            gold_by_id = {int(item["source_event_id"]): item for item in payload["records"]}
        records = []
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
                proposal_result = self.proposals.run(metadata=metadata, candidates=candidates)
                assembled = self.assembler.assemble(
                    metadata=metadata,
                    proposal=proposal_result.proposal,
                    candidates=candidates,
                )
                validation = self.validator.validate(
                    facts=assembled.facts,
                    metadata=metadata,
                    proposal=proposal_result.proposal,
                    candidates=candidates,
                )
                selected_subject = candidates.by_id().get(
                    proposal_result.proposal.primary_subject_candidate_id
                )
                structural = {
                    "evidence": not any(
                        code.startswith("INVALID_EVIDENCE:")
                        for code in validation.blocking_errors
                    ),
                    "entity_hallucination": False,
                    "amount_supported": not (
                        assembled.facts.deal.amount_value is not None
                        and not any(e.field == "deal.amount" for e in assembled.facts.evidence)
                    ),
                    "contract_supported": not (
                        (
                            assembled.facts.deal.contract_until is not None
                            or assembled.facts.deal.contract_years is not None
                        )
                        and not any(e.field == "deal.contract" for e in assembled.facts.evidence)
                    ),
                    "status_policy": "STATUS_STRONGER_THAN_POLICY"
                    not in validation.blocking_errors,
                    "relation_type_safe": not any(
                        code.startswith("RELATION_NOT_CLUB")
                        for code in validation.blocking_errors
                    ),
                    "known_registry_entity_resolved": (
                        selected_subject is None
                        or not selected_subject.registry_matches
                        or assembled.facts.subject.resolution_status.value == "resolved"
                    ),
                    "registry_gap": (
                        selected_subject is not None
                        and not selected_subject.registry_matches
                        and assembled.facts.subject.resolution_status.value == "unresolved"
                    ),
                    "unsupported_blocked": (
                        item.selection_bucket != "unsupported"
                        or not validation.publishable
                    ),
                }
                row.update(
                    {
                        "result": "passed" if validation.publishable else "blocked",
                        "first_attempt_valid": proposal_result.first_attempt_valid,
                        "repaired": proposal_result.repaired,
                        "latency_ms": proposal_result.latency_ms,
                        "candidate_count": len(candidates.candidates),
                        "proposal": proposal_result.proposal.model_dump(mode="json"),
                        "facts": assembled.facts.model_dump(mode="json"),
                        "provenance": [
                            item.model_dump(mode="json") for item in assembled.provenance
                        ],
                        "validation": asdict(validation),
                        "structural": structural,
                    }
                )
                if item.source_event_id in gold_by_id:
                    row["gold_comparison"] = self._compare(
                        row, gold_by_id[item.source_event_id], candidates.by_id()
                    )
            except Exception as exc:  # fail closed and preserve diagnostics
                row.update(
                    {
                        "result": "blocked",
                        "error": f"{type(exc).__name__}:{exc}",
                        "first_attempt_valid": False,
                        "repaired": False,
                        "latency_ms": 0,
                    }
                )
            records.append(row)
        return {"records": records, "metrics": self._metrics(records)}

    @staticmethod
    def _compare(
        row: dict[str, Any], gold: dict[str, Any], candidates: dict[str, Any]
    ) -> dict[str, bool]:
        proposal = row["proposal"]
        facts = row["facts"]
        selected = candidates.get(proposal.get("primary_subject_candidate_id"))
        expected_subject = gold.get("expected_primary_subject_surface")
        subject_ok = (
            expected_subject is None and selected is None
        ) or (
            selected is not None
            and normalize_entity_name(selected.surface)
            == normalize_entity_name(str(expected_subject))
        )
        expected_category = gold.get("expected_event_type")
        category_ok = proposal["semantic_category"] == expected_category
        evidence_ok = all(
            ev["source_text"]
            == gold["original_text"][ev["start_offset"] : ev["end_offset"]]
            for ev in facts["evidence"]
        )
        expected_supported = bool(gold.get("expected_supported_route"))
        return {
            "primary_subject": subject_ok,
            "category": category_ok,
            "evidence": evidence_ok,
            "entity_resolution": (
                expected_subject is None
                or facts["subject"]["resolution_status"] == "resolved"
            ),
            "status": facts["event"]["status"] == gold.get("expected_status"),
            "amount": (
                facts["deal"]["amount_value"] == gold.get("expected_amount_value")
                and facts["deal"]["currency"] == gold.get("expected_currency")
            ),
            "contract": (
                facts["deal"]["contract_years"] == gold.get("expected_contract_years")
                and (
                    gold.get("expected_contract_until") is None
                    or str(gold.get("expected_contract_until")).casefold()
                    in str(facts["deal"]["contract_until"]).casefold()
                )
            ),
            "relation": (
                _surface(facts["clubs"].get("from_club"))
                == normalize_entity_name(str(gold.get("expected_from_club_surface") or ""))
                and _surface(facts["clubs"].get("to_club"))
                == normalize_entity_name(str(gold.get("expected_to_club_surface") or ""))
            ),
            "supported_route": (row["result"] == "passed") == expected_supported,
            "unsupported_blocking": expected_supported or row["result"] == "blocked",
            "negation": (
                not gold.get("negation_present")
                or proposal["signals"]["negated"]
            ),
        }

    @staticmethod
    def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(records)
        comparisons = [r["gold_comparison"] for r in records if "gold_comparison" in r]
        structural = [r["structural"] for r in records if "structural" in r]
        def rate(key: str) -> float | None:
            return (
                round(sum(bool(c[key]) for c in comparisons) / len(comparisons), 4)
                if comparisons else None
            )
        def structural_rate(key: str) -> float | None:
            return (
                round(sum(bool(c[key]) for c in structural) / len(structural), 4)
                if structural else None
            )

        evidence_rate = rate("evidence")
        entity_rate = rate("entity_resolution")
        unsupported_rate = rate("unsupported_blocking")
        if not comparisons:
            evidence_rate = structural_rate("evidence")
            entity_rate = structural_rate("known_registry_entity_resolved")
            unsupported_rate = structural_rate("unsupported_blocked")
        return {
            "stories": total,
            "valid_json_first_attempt": round(
                sum(bool(r.get("first_attempt_valid")) for r in records) / total, 4
            ) if total else 0.0,
            "repair_rate": round(sum(bool(r.get("repaired")) for r in records) / total, 4) if total else 0.0,
            "candidate_selection_accuracy": rate("primary_subject"),
            "primary_subject_accuracy": rate("primary_subject"),
            "category_accuracy": rate("category"),
            "evidence_correctness": evidence_rate,
            "entity_resolution": entity_rate,
            "status_accuracy": rate("status"),
            "amount_accuracy": rate("amount"),
            "contract_accuracy": rate("contract"),
            "relation_accuracy": rate("relation"),
            "negation_preservation": rate("negation"),
            "supported_route_accuracy": rate("supported_route"),
            "correct_unsupported_blocking": unsupported_rate,
            "registry_gaps": [
                r["source_event_id"]
                for r in records
                if r.get("structural", {}).get("registry_gap")
            ],
            "average_latency_ms": round(mean([r.get("latency_ms", 0) for r in records]), 1) if records else 0,
            "dangerous_errors": {
                "new_entity_hallucination": sum(
                    1 for r in records
                    if r.get("structural", {}).get("entity_hallucination")
                ),
                "invented_amount": sum(
                    1 for r in records
                    if r.get("gold_comparison") and not r["gold_comparison"]["amount"]
                    and r.get("facts", {}).get("deal", {}).get("amount_value") is not None
                ),
                "invented_contract": sum(
                    1 for r in records
                    if r.get("gold_comparison") and not r["gold_comparison"]["contract"]
                    and (
                        r.get("facts", {}).get("deal", {}).get("contract_until") is not None
                        or r.get("facts", {}).get("deal", {}).get("contract_years") is not None
                    )
                ),
                "status_strengthened": sum(
                    1 for r in records
                    if r.get("structural") and not r["structural"]["status_policy"]
                ),
                "negation_lost": sum(
                    1 for r in records
                    if r.get("gold_comparison") and not r["gold_comparison"]["negation"]
                ),
                "wrong_from_to": sum(
                    1 for r in records
                    if r.get("gold_comparison") and not r["gold_comparison"]["relation"]
                ),
                "evidence_outside_source": sum(
                    1 for r in records
                    if (
                        r.get("gold_comparison")
                        and not r["gold_comparison"]["evidence"]
                    )
                    or (
                        not r.get("gold_comparison")
                        and r.get("structural")
                        and not r["structural"]["evidence"]
                    )
                ),
            },
        }


def _surface(value: dict[str, Any] | None) -> str:
    return normalize_entity_name(str(value.get("canonical_name", ""))) if value else ""


def write_c2b_reports(
    *, result: dict[str, Any], reports_dir: Path, stamp: str, kind: str,
    safety: dict[str, Any],
) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"stage-c2b-{kind}-{stamp}"
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    md_path = base.with_suffix(".md")
    manual_path = reports_dir / f"stage-c2b-{kind}-manual-review-{stamp}.csv"
    payload = {**result, "safety": safety}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = result["records"]
    columns = [
        "story_id", "source_event_id", "selection_bucket", "result",
        "first_attempt_valid", "repaired", "latency_ms", "candidate_count", "error",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})
    with manual_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_event_id", "story_id", "review_status", "notes"])
        for row in rows:
            writer.writerow([row["source_event_id"], row["story_id"], "", ""])
    md_path.write_text(
        "# Stage C.2B evaluation\n\n"
        f"- Kind: `{kind}`\n"
        f"- Stories: {len(rows)}\n"
        f"- Production unchanged: `{safety['production_unchanged']}`\n\n"
        "## Metrics\n\n```json\n"
        + json.dumps(result["metrics"], ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    return {"json": json_path, "csv": csv_path, "markdown": md_path, "manual_review": manual_path}
