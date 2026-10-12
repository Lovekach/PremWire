from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from premwire.emblem_registry import normalize_entity_name
from .c3a_claims import C3AParseResult, DeterministicTransferClaimParser
from .stage_c1_evaluation import logical_database_fingerprint


PRODUCTION_FINGERPRINT = (
    "c8dc4e2224e885f307586aaf8704daf92175b92644e4311cd2aacbd40e44733e"
)
STATUS_RANK = {
    "unknown": 0,
    "rumour": 1,
    "reported": 2,
    "confirmed_report": 3,
    "official": 4,
}
JOURNALIST_SOURCES = {"x_fabrizio_romano", "x_ben_jacobs"}
OFFICIAL_PREFIXES = ("official_",)


def source_role(source_account: str) -> str:
    if source_account in JOURNALIST_SOURCES:
        return "journalist"
    if source_account.startswith(OFFICIAL_PREFIXES):
        return "official_club"
    return "aggregator"


def _norm(value: str | None) -> str | None:
    return normalize_entity_name(value) if value else None


def _surface(candidate) -> str | None:
    return candidate.surface if candidate is not None else None


def _norm_values(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(normalize_entity_name(value) for value in values))


def _value_is_source_supported(value: str, expected: tuple[str, ...]) -> bool:
    normalized = normalize_entity_name(value)
    expected_normalized = _norm_values(expected)
    return any(
        normalized in target or target in normalized
        for target in expected_normalized
    )


def _precision_recall(
    pairs: Iterable[tuple[str | None, str | None]]
) -> dict[str, float | int]:
    predicted = correct = expected = 0
    for actual, target in pairs:
        if target is not None:
            expected += 1
        if actual is not None:
            predicted += 1
            if actual == target:
                correct += 1
    return {
        "correct": correct,
        "predicted": predicted,
        "expected": expected,
        "precision": correct / predicted if predicted else 1.0,
        "recall": correct / expected if expected else 1.0,
    }


def assert_safe_c3a_evaluation(
    *,
    project_root: Path,
    database: Path,
    scheduler_state: str,
) -> dict[str, Any]:
    production = (project_root / "data" / "premwire.db").resolve()
    database = database.resolve()
    if database == production:
        raise ValueError("Stage C.3A evaluation refuses the production database")
    backup_root = (project_root / "data" / "backups").resolve()
    if backup_root not in database.parents:
        raise ValueError("evaluation database must be inside data/backups")
    if scheduler_state != "Disabled":
        raise ValueError("Scheduler must remain Disabled")
    settings = json.loads(
        (project_root / "config" / "settings.json").read_text(encoding="utf-8")
    )
    if not settings["publishing"]["dry_run"]:
        raise ValueError("publishing.dry_run must remain true")
    if settings["publishing"]["auto_approve"]:
        raise ValueError("publishing.auto_approve must remain false")
    if settings["x_publishing"]["enabled"]:
        raise ValueError("X publishing must remain disabled")
    facts = settings["facts"]
    if facts["pipeline_version"] != "c1":
        raise ValueError("production facts.pipeline_version must remain c1")
    if facts["compact_extraction_enabled"]:
        raise ValueError("compact extraction must remain disabled")
    with sqlite3.connect(database) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise ValueError(f"evaluation database quick_check failed: {quick_check}")
    fingerprint, _ = logical_database_fingerprint(production)
    if fingerprint != PRODUCTION_FINGERPRINT:
        raise ValueError("production database fingerprint changed")
    return {
        "database": str(database),
        "quick_check": quick_check,
        "scheduler_state": scheduler_state,
        "production_fingerprint": fingerprint,
    }


@dataclass(frozen=True, slots=True)
class EvaluationStory:
    story_id: str
    original_text: str
    source_role: str
    event_category: str
    subject: str | None
    from_club: str | None
    to_club: str | None
    current_club: str | None
    interested_clubs: tuple[str, ...]
    deal_type: str
    amounts: tuple[str, ...]
    contracts: tuple[str, ...]
    negation: bool
    tentative: bool
    expected_status: str
    expected_route: str | None


def _development_stories(
    selection: dict[str, Any], annotations: dict[str, Any]
) -> list[EvaluationStory]:
    selected = {
        row["development_event_id"]: row for row in selection["records"]
    }
    stories: list[EvaluationStory] = []
    for row in annotations["records"]:
        source = selected[row["development_event_id"]]
        role = source_role(source["source_account"])
        if row["negation"]:
            status = "unknown"
        elif row["tentative_wording"]:
            status = "rumour"
        elif any(
            normalize_entity_name(signal)
            in {"here we go", "breaking", "срочно"}
            for signal in row["status_signals"]
        ):
            status = "confirmed_report"
        elif row["expected_route"] is not None:
            status = "confirmed_report" if role == "journalist" else "reported"
        else:
            status = "unknown"
        stories.append(
            EvaluationStory(
                story_id=row["development_event_id"],
                original_text=source["source_text"],
                source_role=role,
                event_category=row["event_category"],
                subject=row["primary_subject"],
                from_club=row["from_club"],
                to_club=row["to_club"],
                current_club=row["current_club"],
                interested_clubs=tuple(row["interested_clubs"]),
                deal_type=row["deal_type"],
                amounts=tuple(row["amount"]),
                contracts=tuple(row["contract"]),
                negation=bool(row["negation"]),
                tentative=bool(row["tentative_wording"]),
                expected_status=status,
                expected_route=row["expected_route"],
            )
        )
    return stories


def _gold_source_roles(database: Path) -> dict[int, str]:
    with sqlite3.connect(database) as connection:
        return {
            int(event_id): source_role(account)
            for event_id, account in connection.execute(
                "SELECT id, source_account FROM source_events"
            )
        }


def _gold_stories(
    annotations: dict[str, Any], database: Path
) -> list[EvaluationStory]:
    roles = _gold_source_roles(database)
    stories: list[EvaluationStory] = []
    for row in annotations["records"]:
        route: str | None = None
        if row["expected_supported_route"]:
            if row["expected_priority"] == "breaking":
                route = "breaking_v2"
            elif str(row["expected_deal_type"]).startswith("loan"):
                route = "loan_v2"
            else:
                route = "transfer_v2"
        amounts = (
            (str(row["expected_amount_raw"]),)
            if row["expected_amount_raw"]
            else ()
        )
        contracts: tuple[str, ...] = ()
        if row["expected_contract_until"]:
            contracts = (str(row["expected_contract_until"]),)
        elif row["expected_contract_years"]:
            contracts = (str(row["expected_contract_years"]),)
        stories.append(
            EvaluationStory(
                story_id=row["story_id"],
                original_text=row["original_text"],
                source_role=roles.get(
                    int(row["source_event_id"]), "aggregator"
                ),
                event_category=row["expected_event_type"],
                subject=row["expected_primary_subject_surface"],
                from_club=row["expected_from_club_surface"],
                to_club=row["expected_to_club_surface"],
                current_club=row["expected_current_club_surface"],
                interested_clubs=tuple(
                    row["expected_interested_club_surfaces"]
                ),
                deal_type=row["expected_deal_type"],
                amounts=amounts,
                contracts=contracts,
                negation=bool(row["negation_present"]),
                tentative=bool(row["tentative_language_present"]),
                expected_status=row["expected_status"],
                expected_route=route,
            )
        )
    return stories


class StageC3AEvaluator:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.parser = DeterministicTransferClaimParser(project_root)

    @staticmethod
    def _record(
        story: EvaluationStory,
        result: C3AParseResult,
        latency_ms: float,
    ) -> dict[str, Any]:
        graph = result.graph
        predicted_subject = _surface(graph.subject_candidate) if graph else None
        predicted_from = _surface(graph.from_club_candidate) if graph else None
        predicted_to = _surface(graph.to_club_candidate) if graph else None
        predicted_current = (
            _surface(graph.current_club_candidate) if graph else None
        )
        predicted_interested = (
            tuple(
                candidate.surface
                for candidate in graph.interested_club_candidates
            )
            if graph
            else ()
        )
        predicted_amounts = (
            tuple(candidate.surface for candidate in graph.amount_candidates)
            if graph
            else ()
        )
        predicted_contracts = (
            tuple(candidate.surface for candidate in graph.contract_candidates)
            if graph
            else ()
        )
        predicted_category = (
            "loan"
            if graph
            and graph.deal_type
            in {"loan", "loan_with_option", "loan_with_obligation"}
            else "transfer"
            if graph
            else "unknown"
        )
        dangerous: list[str] = []
        if graph:
            source = story.original_text
            candidates = [
                graph.subject_candidate,
                graph.current_club_candidate,
                graph.from_club_candidate,
                graph.to_club_candidate,
                *graph.interested_club_candidates,
                *graph.amount_candidates,
                *graph.contract_candidates,
            ]
            for candidate in (item for item in candidates if item is not None):
                if (
                    candidate.start_offset < 0
                    or candidate.end_offset > len(source)
                    or source[candidate.start_offset : candidate.end_offset]
                    != candidate.surface
                ):
                    dangerous.append("evidence_outside_source")
                    break
            if (
                predicted_from is not None
                and story.from_club is not None
                and _norm(predicted_from) != _norm(story.from_club)
            ) or (
                predicted_to is not None
                and story.to_club is not None
                and _norm(predicted_to) != _norm(story.to_club)
            ):
                dangerous.append("wrong_from_to")
            if story.negation and graph.negation_signal is None:
                dangerous.append("negation_lost")
            if any(
                not _value_is_source_supported(value, story.amounts)
                for value in predicted_amounts
            ):
                dangerous.append("invented_amount")
            if any(
                not _value_is_source_supported(value, story.contracts)
                for value in predicted_contracts
            ):
                dangerous.append("invented_contract")
        if STATUS_RANK[result.status] > STATUS_RANK[story.expected_status]:
            dangerous.append("status_strengthened")
        if result.route is not None and result.route != story.expected_route:
            dangerous.append("unsupported_or_wrong_route")
        graph_exact = all(
            (
                _norm(predicted_subject) == _norm(story.subject),
                _norm(predicted_from) == _norm(story.from_club),
                _norm(predicted_to) == _norm(story.to_club),
                _norm(predicted_current) == _norm(story.current_club),
                _norm_values(predicted_interested)
                == _norm_values(story.interested_clubs),
                (
                    graph.deal_type == story.deal_type
                    if graph is not None
                    else story.deal_type in {"unknown", "not_applicable"}
                ),
                _norm_values(predicted_amounts) == _norm_values(story.amounts),
                _norm_values(predicted_contracts)
                == _norm_values(story.contracts),
                (graph.negation_signal is not None if graph else False)
                == story.negation,
                (graph.tentative_signal is not None if graph else False)
                == story.tentative,
            )
        )
        return {
            "story_id": story.story_id,
            "source_role": story.source_role,
            "expected": {
                "category": story.event_category,
                "subject": story.subject,
                "from_club": story.from_club,
                "to_club": story.to_club,
                "route": story.expected_route,
                "status": story.expected_status,
                "amounts": story.amounts,
                "contracts": story.contracts,
            },
            "predicted": {
                "category": predicted_category,
                "subject": predicted_subject,
                "from_club": predicted_from,
                "to_club": predicted_to,
                "route": result.route,
                "status": result.status,
                "amounts": predicted_amounts,
                "contracts": predicted_contracts,
                "pattern": graph.parser_pattern_id if graph else None,
                "blocking_reasons": result.blocking_reasons,
            },
            "graph_exact_match": graph_exact,
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
            latency = (time.perf_counter() - started) * 1000
            records.append(self._record(story, result, latency))
        subject = _precision_recall(
            (
                _norm(row["predicted"]["subject"]),
                _norm(row["expected"]["subject"]),
            )
            for row in records
        )
        from_club = _precision_recall(
            (
                _norm(row["predicted"]["from_club"]),
                _norm(row["expected"]["from_club"]),
            )
            for row in records
        )
        to_club = _precision_recall(
            (
                _norm(row["predicted"]["to_club"]),
                _norm(row["expected"]["to_club"]),
            )
            for row in records
        )
        routes = _precision_recall(
            (
                row["predicted"]["route"],
                row["expected"]["route"],
            )
            for row in records
        )
        dangerous = [
            {"story_id": row["story_id"], "errors": row["dangerous_errors"]}
            for row in records
            if row["dangerous_errors"]
        ]
        unsupported = [
            row for row in records if row["expected"]["route"] is None
        ]
        metrics = {
            "stories": len(records),
            "category_accuracy": sum(
                row["predicted"]["category"] == row["expected"]["category"]
                for row in records
            )
            / len(records),
            "subject": subject,
            "from_club": from_club,
            "to_club": to_club,
            "relation_graph_exact_match": sum(
                row["graph_exact_match"] for row in records
            )
            / len(records),
            "amount_binding_accuracy": sum(
                _norm_values(row["predicted"]["amounts"])
                == _norm_values(row["expected"]["amounts"])
                for row in records
            )
            / len(records),
            "contract_binding_accuracy": sum(
                _norm_values(row["predicted"]["contracts"])
                == _norm_values(row["expected"]["contracts"])
                for row in records
            )
            / len(records),
            "negation_preservation": sum(
                "negation_lost" not in row["dangerous_errors"]
                for row in records
            )
            / len(records),
            "status_accuracy": sum(
                row["predicted"]["status"] == row["expected"]["status"]
                for row in records
            )
            / len(records),
            "supported_route": routes,
            "unsupported_blocking": (
                sum(row["predicted"]["route"] is None for row in unsupported)
                / len(unsupported)
                if unsupported
                else 1.0
            ),
            "dangerous_error_count": sum(
                len(row["dangerous_errors"]) for row in records
            ),
            "dangerous_story_count": len(dangerous),
            "average_latency_ms": sum(
                row["latency_ms"] for row in records
            )
            / len(records),
        }
        return {"metrics": metrics, "dangerous_errors": dangerous, "records": records}

    def evaluate_development(
        self, selection_path: Path, annotations_path: Path
    ) -> dict[str, Any]:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
        return self.evaluate(_development_stories(selection, annotations))

    def evaluate_gold(
        self, annotations_path: Path, database: Path
    ) -> dict[str, Any]:
        annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
        return self.evaluate(_gold_stories(annotations, database))


def render_markdown(title: str, payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    route = metrics["supported_route"]
    return "\n".join(
        [
            f"# {title}",
            "",
            f"- Stories: {metrics['stories']}",
            f"- Supported route precision: {route['precision']:.3f}",
            f"- Supported route recall: {route['recall']:.3f}",
            f"- Unsupported blocking: {metrics['unsupported_blocking']:.3f}",
            f"- Dangerous errors: {metrics['dangerous_error_count']}",
            f"- Category accuracy: {metrics['category_accuracy']:.3f}",
            f"- Graph exact match: {metrics['relation_graph_exact_match']:.3f}",
            f"- Subject P/R: {metrics['subject']['precision']:.3f} / "
            f"{metrics['subject']['recall']:.3f}",
            f"- From-club P/R: {metrics['from_club']['precision']:.3f} / "
            f"{metrics['from_club']['recall']:.3f}",
            f"- To-club P/R: {metrics['to_club']['precision']:.3f} / "
            f"{metrics['to_club']['recall']:.3f}",
            f"- Amount binding accuracy: "
            f"{metrics['amount_binding_accuracy']:.3f}",
            f"- Contract binding accuracy: "
            f"{metrics['contract_binding_accuracy']:.3f}",
            f"- Negation preservation: "
            f"{metrics['negation_preservation']:.3f}",
            f"- Status accuracy: {metrics['status_accuracy']:.3f}",
            f"- Average latency: {metrics['average_latency_ms']:.3f} ms",
            "",
        ]
    )
