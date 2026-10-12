from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .c3a_claims import C3AParseResult, DeterministicTransferClaimParser
from .c3a_evaluation import STATUS_RANK, assert_safe_c3a_evaluation, source_role


DEVELOPMENT_GATE = {
    "route_precision": 1.0,
    "route_recall_minimum": 0.5,
    "unsupported_blocking": 1.0,
    "dangerous_errors": 0,
    "canonical_entity_accuracy_routed": 1.0,
    "relation_direction_accuracy_routed": 1.0,
    "negation_preservation": 1.0,
}


@dataclass(frozen=True, slots=True)
class ExpectedEntity:
    entity_id: str | None
    canonical_name: str | None
    source_mention: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class ExpectedSpan:
    source_mention: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class EvaluationStory:
    story_id: str
    original_text: str
    source_role: str
    support_class: str
    taxonomy_bucket: str
    expected_route: str | None
    subject: ExpectedEntity | None
    from_club: ExpectedEntity | None
    to_club: ExpectedEntity | None
    amounts: tuple[ExpectedSpan, ...]
    contracts: tuple[ExpectedSpan, ...]
    expected_status: str
    negation: bool

    @property
    def supported(self) -> bool:
        return self.support_class == "supported"


def _entity(payload: dict[str, Any] | None) -> ExpectedEntity | None:
    if payload is None:
        return None
    return ExpectedEntity(
        entity_id=(
            str(payload["entity_id"])
            if payload.get("entity_id") is not None
            else None
        ),
        canonical_name=(
            str(payload["canonical_name"])
            if payload.get("canonical_name") is not None
            else None
        ),
        source_mention=str(payload["source_mention"]),
        start_offset=int(payload["start_offset"]),
        end_offset=int(payload["end_offset"]),
    )


def _spans(payload: Iterable[dict[str, Any]]) -> tuple[ExpectedSpan, ...]:
    return tuple(
        ExpectedSpan(
            source_mention=str(item["source_mention"]),
            start_offset=int(item["start_offset"]),
            end_offset=int(item["end_offset"]),
        )
        for item in payload
    )


def load_development_stories(
    selection_path: Path,
    annotations_path: Path,
) -> list[EvaluationStory]:
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
    selected = {
        row["development_event_id"]: row for row in selection["records"]
    }
    stories: list[EvaluationStory] = []
    for row in annotations["records"]:
        source = selected[row["development_event_id"]]
        stories.append(
            EvaluationStory(
                story_id=str(row["development_event_id"]),
                original_text=str(source["source_text"]),
                source_role=source_role(str(source["source_account"])),
                support_class=str(row["expected_support_class"]),
                taxonomy_bucket=str(row["taxonomy_bucket"]),
                expected_route=row["expected_route_decision"],
                subject=_entity(row["subject"]),
                from_club=_entity(row["from_club"]),
                to_club=_entity(row["to_club"]),
                amounts=_spans(row["amount"]),
                contracts=_spans(row["contract"]),
                expected_status=str(row["status"]),
                negation=bool(row["negation"]),
            )
        )
    return stories


def load_gold_v2_stories(annotations_path: Path) -> list[EvaluationStory]:
    payload = json.loads(annotations_path.read_text(encoding="utf-8"))
    if payload.get("annotation_independent_from_parser") is not True:
        raise ValueError("gold v2 annotations are not independently certified")
    stories: list[EvaluationStory] = []
    for record in payload["records"]:
        original_text = str(record["original_text"])
        evidence = _spans(record.get("evidence_spans", []))
        for span in evidence:
            if original_text[span.start_offset : span.end_offset] != span.source_mention:
                raise ValueError(f"gold evidence mismatch: {record['gold_event_id']}")
        stories.append(
            EvaluationStory(
                story_id=str(record["gold_event_id"]),
                original_text=original_text,
                source_role=str(record["source_role"]),
                support_class=str(record["expected_support_class"]),
                taxonomy_bucket=str(record["taxonomy_bucket"]),
                expected_route=record.get("expected_route_decision"),
                subject=_entity(record.get("subject")),
                from_club=_entity(record.get("from_club")),
                to_club=_entity(record.get("to_club")),
                amounts=_spans(record.get("amount", [])),
                contracts=_spans(record.get("contract", [])),
                expected_status=str(record["status"]),
                negation=bool(record["negation"]),
            )
        )
    return stories


def _candidate_id(candidate: Any) -> str | None:
    return candidate.registry_entity_id if candidate is not None else None


def _entity_metric(
    pairs: Iterable[tuple[str | None, str | None]],
) -> dict[str, int | float]:
    predicted = expected = correct = 0
    for actual, target in pairs:
        if actual is not None:
            predicted += 1
        if target is not None:
            expected += 1
        if actual is not None and actual == target:
            correct += 1
    return {
        "correct": correct,
        "predicted": predicted,
        "expected": expected,
        "precision": correct / predicted if predicted else 1.0,
        "recall": correct / expected if expected else 1.0,
    }


def _source_exact(text: str, candidate: Any) -> bool:
    return (
        0 <= candidate.start_offset < candidate.end_offset <= len(text)
        and text[candidate.start_offset : candidate.end_offset]
        == candidate.surface
    )


def _values_cover_expected(
    text: str,
    predicted: tuple[Any, ...],
    expected: tuple[ExpectedSpan, ...],
) -> bool:
    if not predicted and not expected:
        return True
    if not predicted or not expected:
        return False
    if not all(_source_exact(text, item) for item in predicted):
        return False
    for item in expected:
        if text[item.start_offset : item.end_offset] != item.source_mention:
            return False
        if not any(
            candidate.start_offset <= item.start_offset
            and item.end_offset <= candidate.end_offset
            for candidate in predicted
        ):
            return False
    return all(
        any(
            candidate.start_offset <= item.start_offset
            and item.end_offset <= candidate.end_offset
            for item in expected
        )
        for candidate in predicted
    )


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


class StageC3BEvaluator:
    def __init__(self, project_root: Path) -> None:
        self.parser = DeterministicTransferClaimParser(project_root)

    @staticmethod
    def _record(
        story: EvaluationStory,
        result: C3AParseResult,
        latency_ms: float,
    ) -> dict[str, Any]:
        graph = result.graph
        subject_id = _candidate_id(graph.subject_candidate) if graph else None
        from_id = _candidate_id(graph.from_club_candidate) if graph else None
        to_id = _candidate_id(graph.to_club_candidate) if graph else None
        expected_subject = story.subject.entity_id if story.subject else None
        expected_from = story.from_club.entity_id if story.from_club else None
        expected_to = story.to_club.entity_id if story.to_club else None
        predicted_amounts = graph.amount_candidates if graph else ()
        predicted_contracts = graph.contract_candidates if graph else ()
        amount_correct = _values_cover_expected(
            story.original_text, predicted_amounts, story.amounts
        )
        contract_correct = _values_cover_expected(
            story.original_text, predicted_contracts, story.contracts
        )
        evidence_correct = True
        if graph is not None:
            candidates = (
                graph.subject_candidate,
                graph.current_club_candidate,
                graph.from_club_candidate,
                graph.to_club_candidate,
                *graph.interested_club_candidates,
                *graph.amount_candidates,
                *graph.contract_candidates,
            )
            evidence_correct = all(
                _source_exact(story.original_text, item)
                for item in candidates
                if item is not None
            ) and all(
                0 <= start < end <= len(story.original_text)
                for start, end in graph.evidence_spans
            )

        route_correct = result.route == story.expected_route
        entity_correct = (
            subject_id == expected_subject
            and from_id == expected_from
            and to_id == expected_to
        )
        direction_correct = from_id == expected_from and to_id == expected_to
        negation_preserved = (
            not story.negation
            or result.route is None
            or (graph is not None and graph.negation_signal is not None)
        )
        status_correct = result.status == story.expected_status
        graph_exact = all(
            (
                route_correct,
                entity_correct,
                amount_correct,
                contract_correct,
                negation_preserved,
                status_correct,
                evidence_correct,
            )
        )

        dangerous: list[str] = []
        if not evidence_correct:
            dangerous.append("evidence_outside_source")
        if result.route is not None and not story.supported:
            dangerous.append("unsupported_routed")
            if story.negation:
                dangerous.append("lost_negation")
            if story.taxonomy_bucket == "historical_transfer":
                dangerous.append("historical_claim_treated_as_current")
            if story.taxonomy_bucket == "tentative_interest":
                dangerous.append("tentative_claim_treated_as_completed")
        if result.route is not None:
            if expected_subject is not None and subject_id != expected_subject:
                dangerous.append("wrong_subject")
            if expected_from is not None and from_id != expected_from:
                dangerous.append("wrong_from")
            if expected_to is not None and to_id != expected_to:
                dangerous.append("wrong_to")
            if from_id == expected_to and to_id == expected_from and from_id != to_id:
                dangerous.append("swapped_from_to")
            predicted_ids = {item for item in (subject_id, from_id, to_id) if item}
            expected_ids = {
                item for item in (expected_subject, expected_from, expected_to) if item
            }
            if not predicted_ids.issubset(expected_ids):
                dangerous.append("invented_entity")
        if any(
            not _source_exact(story.original_text, item)
            for item in predicted_amounts
        ):
            dangerous.append("invented_amount")
        if any(
            not _source_exact(story.original_text, item)
            for item in predicted_contracts
        ):
            dangerous.append("invented_contract")
        if (
            result.route is not None
            and STATUS_RANK[result.status] > STATUS_RANK[story.expected_status]
        ):
            dangerous.append("strengthened_status")

        return {
            "story_id": story.story_id,
            "taxonomy_bucket": story.taxonomy_bucket,
            "support_class": story.support_class,
            "expected": {
                "route": story.expected_route,
                "subject_id": expected_subject,
                "from_club_id": expected_from,
                "to_club_id": expected_to,
                "status": story.expected_status,
                "negation": story.negation,
                "amount_spans": [
                    {
                        "source_mention": item.source_mention,
                        "start_offset": item.start_offset,
                        "end_offset": item.end_offset,
                    }
                    for item in story.amounts
                ],
                "contract_spans": [
                    {
                        "source_mention": item.source_mention,
                        "start_offset": item.start_offset,
                        "end_offset": item.end_offset,
                    }
                    for item in story.contracts
                ],
            },
            "predicted": {
                "route": result.route,
                "subject_id": subject_id,
                "from_club_id": from_id,
                "to_club_id": to_id,
                "status": result.status,
                "negation": bool(graph and graph.negation_signal),
                "amount_spans": [
                    [item.start_offset, item.end_offset]
                    for item in predicted_amounts
                ],
                "contract_spans": [
                    [item.start_offset, item.end_offset]
                    for item in predicted_contracts
                ],
                "pattern": graph.parser_pattern_id if graph else None,
                "blocking_reasons": list(result.blocking_reasons),
            },
            "checks": {
                "route": route_correct,
                "entity": entity_correct,
                "direction": direction_correct,
                "amount": amount_correct,
                "contract": contract_correct,
                "negation": negation_preserved,
                "status": status_correct,
                "evidence": evidence_correct,
                "exact_graph": graph_exact,
            },
            "dangerous_errors": sorted(set(dangerous)),
            "latency_ms": latency_ms,
        }

    def evaluate(self, stories: list[EvaluationStory]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for story in stories:
            started = time.perf_counter()
            result = self.parser.parse(
                source_event_id=story.story_id,
                original_text=story.original_text,
                source_role=story.source_role,
            )
            records.append(
                self._record(
                    story,
                    result,
                    (time.perf_counter() - started) * 1000,
                )
            )

        supported = [row for row in records if row["support_class"] == "supported"]
        unsupported = [row for row in records if row["support_class"] != "supported"]
        routed = [row for row in records if row["predicted"]["route"] is not None]
        routed_supported = [
            row for row in supported if row["predicted"]["route"] is not None
        ]
        routed_unsupported = [
            row for row in unsupported if row["predicted"]["route"] is not None
        ]
        route_correct = sum(row["checks"]["route"] for row in routed)
        subject = _entity_metric(
            (row["predicted"]["subject_id"], row["expected"]["subject_id"])
            for row in routed_supported
        )
        from_club = _entity_metric(
            (row["predicted"]["from_club_id"], row["expected"]["from_club_id"])
            for row in routed_supported
        )
        to_club = _entity_metric(
            (row["predicted"]["to_club_id"], row["expected"]["to_club_id"])
            for row in routed_supported
        )
        errors = Counter(
            error for row in records for error in row["dangerous_errors"]
        )
        latencies = [float(row["latency_ms"]) for row in records]
        metrics = {
            "total_stories": len(records),
            "supported_stories": len(supported),
            "unsupported_stories": len(unsupported),
            "routed_supported": len(routed_supported),
            "blocked_supported": len(supported) - len(routed_supported),
            "routed_unsupported": len(routed_unsupported),
            "route_precision": route_correct / len(routed) if routed else 1.0,
            "route_recall": len(routed_supported) / len(supported) if supported else 1.0,
            "unsupported_blocking": (
                (len(unsupported) - len(routed_unsupported)) / len(unsupported)
                if unsupported
                else 1.0
            ),
            "canonical_subject": subject,
            "canonical_from_club": from_club,
            "canonical_to_club": to_club,
            "canonical_entity_accuracy_routed": (
                sum(row["checks"]["entity"] for row in routed_supported)
                / len(routed_supported)
                if routed_supported
                else 1.0
            ),
            "relation_direction_accuracy_routed": (
                sum(row["checks"]["direction"] for row in routed_supported)
                / len(routed_supported)
                if routed_supported
                else 1.0
            ),
            "graph_exact_match": sum(
                row["checks"]["exact_graph"] for row in records
            ) / len(records),
            "graph_exact_match_routed_supported": (
                sum(row["checks"]["exact_graph"] for row in routed_supported)
                / len(routed_supported)
                if routed_supported
                else 1.0
            ),
            "amount_accuracy": sum(row["checks"]["amount"] for row in records)
            / len(records),
            "contract_accuracy": sum(
                row["checks"]["contract"] for row in records
            ) / len(records),
            "negation_preservation": sum(
                row["checks"]["negation"] for row in records
            ) / len(records),
            "status_accuracy": sum(row["checks"]["status"] for row in records)
            / len(records),
            "evidence_span_accuracy": sum(
                row["checks"]["evidence"] for row in records
            ) / len(records),
            "average_latency_ms": sum(latencies) / len(latencies),
            "p95_latency_ms": _p95(latencies),
            "dangerous_error_count": sum(errors.values()),
            "dangerous_story_count": sum(bool(row["dangerous_errors"]) for row in records),
            "dangerous_errors_by_category": dict(sorted(errors.items())),
        }
        gate_checks = {
            "route_precision": metrics["route_precision"] == 1.0,
            "route_recall": metrics["route_recall"] >= 0.5,
            "unsupported_blocking": metrics["unsupported_blocking"] == 1.0,
            "dangerous_errors": metrics["dangerous_error_count"] == 0,
            "canonical_entity_accuracy_routed": metrics[
                "canonical_entity_accuracy_routed"
            ] == 1.0,
            "relation_direction_accuracy_routed": metrics[
                "relation_direction_accuracy_routed"
            ] == 1.0,
            "negation_preservation": metrics["negation_preservation"] == 1.0,
        }
        return {
            "evaluation_version": "stage-c3b-development-1.0",
            "metrics": metrics,
            "development_gate": {
                "decision": "PASSED" if all(gate_checks.values()) else "FAILED",
                "checks": gate_checks,
                "thresholds": DEVELOPMENT_GATE,
            },
            "dangerous_errors": [
                {
                    "story_id": row["story_id"],
                    "errors": row["dangerous_errors"],
                }
                for row in records
                if row["dangerous_errors"]
            ],
            "records": records,
        }

    def evaluate_development(
        self,
        selection_path: Path,
        annotations_path: Path,
    ) -> dict[str, Any]:
        return self.evaluate(
            load_development_stories(selection_path, annotations_path)
        )


def render_markdown(title: str, payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    gate = payload["development_gate"]
    return "\n".join(
        [
            f"# {title}",
            "",
            f"- Decision: **{gate['decision']}**",
            f"- Stories: {metrics['total_stories']}",
            f"- Supported / unsupported: {metrics['supported_stories']} / {metrics['unsupported_stories']}",
            f"- Routed supported / blocked supported: {metrics['routed_supported']} / {metrics['blocked_supported']}",
            f"- Routed unsupported: {metrics['routed_unsupported']}",
            f"- Route precision: {metrics['route_precision']:.6f}",
            f"- Route recall: {metrics['route_recall']:.6f}",
            f"- Unsupported blocking: {metrics['unsupported_blocking']:.6f}",
            f"- Canonical entity accuracy (routed): {metrics['canonical_entity_accuracy_routed']:.6f}",
            f"- Direction accuracy (routed): {metrics['relation_direction_accuracy_routed']:.6f}",
            f"- Graph exact match (all): {metrics['graph_exact_match']:.6f}",
            f"- Graph exact match (routed supported): {metrics['graph_exact_match_routed_supported']:.6f}",
            f"- Amount accuracy: {metrics['amount_accuracy']:.6f}",
            f"- Contract accuracy: {metrics['contract_accuracy']:.6f}",
            f"- Negation preservation: {metrics['negation_preservation']:.6f}",
            f"- Status accuracy: {metrics['status_accuracy']:.6f}",
            f"- Evidence span accuracy: {metrics['evidence_span_accuracy']:.6f}",
            f"- Dangerous errors: {metrics['dangerous_error_count']}",
            f"- Average latency: {metrics['average_latency_ms']:.3f} ms",
            f"- P95 latency: {metrics['p95_latency_ms']:.3f} ms",
            "",
            "## Gate checks",
            "",
            *[
                f"- {name}: {'PASS' if passed else 'FAIL'}"
                for name, passed in gate["checks"].items()
            ],
            "",
        ]
    )


__all__ = [
    "load_gold_v2_stories",
    "DEVELOPMENT_GATE",
    "EvaluationStory",
    "ExpectedEntity",
    "ExpectedSpan",
    "StageC3BEvaluator",
    "assert_safe_c3a_evaluation",
    "load_development_stories",
    "render_markdown",
]
