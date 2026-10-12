from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PREVALENCE_TAXONOMY = (
    "completed_transfer",
    "completed_loan",
    "confirmed_transfer_report",
    "transfer_rumour",
    "club_interest",
    "contract_extension",
    "new_contract_without_transfer",
    "free_agent",
    "player_release",
    "return_from_loan",
    "injury",
    "suspension",
    "match_preview",
    "lineup",
    "match_result",
    "goal_or_statistic",
    "tournament_news",
    "coach_or_manager_news",
    "player_quote_or_interview",
    "club_statement",
    "financial_or_business_news",
    "unrelated_sports_news",
    "malformed_duplicate_or_footer_only",
    "ambiguous",
)

TRANSFER_RELATED_CLASSES = frozenset(
    {
        "completed_transfer",
        "completed_loan",
        "confirmed_transfer_report",
        "transfer_rumour",
        "club_interest",
        "contract_extension",
        "new_contract_without_transfer",
        "free_agent",
        "player_release",
        "return_from_loan",
    }
)


def portable_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_digest(values: Iterable[str]) -> str:
    encoded = "\n".join(sorted({str(value) for value in values if value not in (None, "")})).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def extract_identity_index(records: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    index = {
        "story_id": set(),
        "source_event_id": set(),
        "legacy_item_id": set(),
        "content_hash": set(),
        "text_sha256": set(),
        "source_url_hash": set(),
    }
    for record in records:
        for key in ("story_id", "story_uuid", "development_event_id", "gold_event_id"):
            if record.get(key) not in (None, ""):
                index["story_id"].add(str(record[key]))
        if record.get("source_event_id") not in (None, ""):
            index["source_event_id"].add(str(record["source_event_id"]))
        for key in ("legacy_item_id", "legacy_event_id"):
            if record.get(key) not in (None, ""):
                index["legacy_item_id"].add(str(record[key]))
        if record.get("content_hash") not in (None, ""):
            index["content_hash"].add(str(record["content_hash"]))
        for key in ("text_sha256", "raw_text_sha256", "normalized_source_hash"):
            if record.get(key) not in (None, ""):
                index["text_sha256"].add(str(record[key]))
        if record.get("source_url_hash") not in (None, ""):
            index["source_url_hash"].add(str(record["source_url_hash"]))
    return index


def identity_summary(index: Mapping[str, set[str]]) -> dict[str, Any]:
    return {
        key: {"count": len(values), "sha256": stable_digest(values)}
        for key, values in sorted(index.items())
    }


def overlap_counts(left: Mapping[str, set[str]], right: Mapping[str, set[str]]) -> dict[str, int]:
    return {key: len(left.get(key, set()) & right.get(key, set())) for key in sorted(set(left) | set(right))}


def read_only_counts(database: Path) -> dict[str, int]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("items", "stories", "source_events")
            if table in tables
        }
    finally:
        connection.close()


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    radius = z * math.sqrt((proportion * (1.0 - proportion) + z * z / (4.0 * total)) / total) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - radius)
    high = 1.0 if successes == total else min(1.0, centre + radius)
    return low, high


def prevalence_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(records)
    taxonomy = Counter(str(record["primary_taxonomy_class"]) for record in records)
    sources = Counter(str(record["source"]) for record in records)
    languages = Counter(str(record["language"]) for record in records)
    periods = Counter(str(record["publication_period"]) for record in records)
    supported = sum(record["current_c3b_scope"] == "supported" for record in records)
    publishable = sum(bool(record["potentially_publishable"]) for record in records)
    transfer_related = sum(record["primary_taxonomy_class"] in TRANSFER_RELATED_CLASSES for record in records)
    malformed = taxonomy["malformed_duplicate_or_footer_only"]

    def share(value: int) -> dict[str, Any]:
        low, high = wilson_interval(value, total)
        return {"count": value, "share": value / total if total else 0.0, "wilson_95": [low, high]}

    return {
        "records_total": total,
        "taxonomy_distribution": {key: share(taxonomy[key]) for key in PREVALENCE_TAXONOMY},
        "transfer_related": share(transfer_related),
        "completed_transfers": share(taxonomy["completed_transfer"]),
        "completed_loans": share(taxonomy["completed_loan"]),
        "rumours_and_interest": share(taxonomy["transfer_rumour"] + taxonomy["club_interest"]),
        "current_c3b_supported_scope": share(supported),
        "potentially_publishable": share(publishable),
        "duplicates_or_malformed": share(malformed),
        "source_distribution": dict(sorted(sources.items())),
        "language_distribution": dict(sorted(languages.items())),
        "publication_period_distribution": dict(sorted(periods.items())),
        "is_parser_gate": False,
        "gate_decision": None,
    }


@dataclass(frozen=True)
class CapabilityGuardInput:
    supported_denominator: int
    frozen: bool
    checksum_matches: bool
    run_count: int
    working_tree_clean: bool
    production_safe: bool
    scheduler_disabled: bool
    overlap_count: int
    c2c_holdout_opened: bool
    uses_production_database: bool


def validate_capability_gate_ready(candidate: CapabilityGuardInput) -> None:
    failures: list[str] = []
    if candidate.supported_denominator < 30:
        failures.append("supported denominator below 30")
    if not candidate.frozen:
        failures.append("corpus is not frozen")
    if not candidate.checksum_matches:
        failures.append("checksum mismatch")
    if candidate.run_count != 0:
        failures.append("run count is not zero")
    if not candidate.working_tree_clean:
        failures.append("working tree is dirty")
    if not candidate.production_safe:
        failures.append("production flags are unsafe")
    if not candidate.scheduler_disabled:
        failures.append("scheduler is enabled")
    if candidate.overlap_count:
        failures.append("corpus overlaps an existing dataset")
    if candidate.c2c_holdout_opened:
        failures.append("C.2C holdout is open")
    if candidate.uses_production_database:
        failures.append("production database is forbidden")
    if failures:
        raise RuntimeError("; ".join(failures))


__all__ = [
    "CapabilityGuardInput",
    "PREVALENCE_TAXONOMY",
    "TRANSFER_RELATED_CLASSES",
    "extract_identity_index",
    "file_sha256",
    "identity_summary",
    "overlap_counts",
    "portable_text_sha256",
    "prevalence_metrics",
    "read_only_counts",
    "stable_digest",
    "validate_capability_gate_ready",
    "wilson_interval",
]
