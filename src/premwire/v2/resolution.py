from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from premwire.emblem_registry import EmblemRegistry, normalize_entity_name


OFFICIAL_SOURCE_ROLES = {
    "official_club",
    "official_league",
    "official_federation",
    "official_player",
}
ALLOWED_SOURCE_ROLES = OFFICIAL_SOURCE_ROLES | {
    "journalist",
    "media",
    "aggregator",
    "unknown",
}


@dataclass(frozen=True, slots=True)
class SourceRecord:
    platform: str
    account: str
    canonical_name: str
    source_role: str
    trust_tier: int
    official_entity_id: str | None
    allowed_languages: tuple[str, ...]
    enabled: bool
    trusted_for_editorial: bool = False

    @property
    def is_official(self) -> bool:
        return self.source_role in OFFICIAL_SOURCE_ROLES


class SourceRegistry:
    def __init__(self, records: list[SourceRecord], version: int) -> None:
        self.version = version
        self.records = tuple(records)
        self._by_key: dict[tuple[str, str], SourceRecord] = {}
        for record in records:
            if record.source_role not in ALLOWED_SOURCE_ROLES:
                raise ValueError(f"unsupported source role: {record.source_role}")
            if record.trust_tier < 1:
                raise ValueError("trust_tier must be positive")
            key = (record.platform.casefold(), record.account.casefold().lstrip("@"))
            if key in self._by_key:
                raise ValueError(f"duplicate source: {key}")
            self._by_key[key] = record

    @classmethod
    def load(cls, path: Path) -> "SourceRegistry":
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = [
            SourceRecord(
                platform=str(item["platform"]),
                account=str(item["account"]),
                canonical_name=str(item["canonical_name"]),
                source_role=str(item["source_role"]),
                trust_tier=int(item["trust_tier"]),
                official_entity_id=item.get("official_entity_id"),
                allowed_languages=tuple(str(value) for value in item["allowed_languages"]),
                enabled=bool(item["enabled"]),
                trusted_for_editorial=bool(item.get("trusted_for_editorial", False)),
            )
            for item in payload["sources"]
        ]
        return cls(records, int(payload["version"]))

    def resolve(self, platform: str, account: str) -> SourceRecord | None:
        record = self._by_key.get(
            (platform.casefold(), account.casefold().lstrip("@"))
        )
        return record if record is not None and record.enabled else None


class ConfidenceType(str, Enum):
    EXACT_CANONICAL = "EXACT_CANONICAL"
    EXACT_ALIAS = "EXACT_ALIAS"
    NORMALIZED_ALIAS = "NORMALIZED_ALIAS"
    SOURCE_EXPLICIT = "SOURCE_EXPLICIT"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    entity_id: str | None
    entity_type: str
    canonical_name: str
    display_name_ru: str | None
    surname_display: str | None
    status: str
    provenance: str
    confidence_type: ConfidenceType
    grammatical_forms: dict[str, Any]

    @property
    def resolved(self) -> bool:
        return self.confidence_type is not ConfidenceType.UNRESOLVED


class EntityResolver:
    def __init__(
        self,
        *,
        project_root: Path,
        registry_path: Path | None = None,
    ) -> None:
        self.project_root = project_root
        path = registry_path or project_root / "config" / "entity_registry.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.version = int(payload["version"])
        self._canonical: dict[str, ResolvedEntity] = {}
        self._aliases: dict[str, tuple[ResolvedEntity, str]] = {}
        ambiguous: set[str] = set()
        for raw in payload["entities"]:
            entity = ResolvedEntity(
                entity_id=str(raw["entity_id"]),
                entity_type=str(raw["entity_type"]),
                canonical_name=str(raw["canonical_name"]),
                display_name_ru=raw.get("display_name_ru"),
                surname_display=raw.get("surname_display"),
                status=str(raw["status"]),
                provenance=str(raw["provenance"]),
                confidence_type=ConfidenceType.EXACT_CANONICAL,
                grammatical_forms=dict(raw.get("grammatical_forms", {})),
            )
            canonical = normalize_entity_name(entity.canonical_name)
            self._canonical[canonical] = entity
            for alias in raw.get("aliases", []):
                normalized = normalize_entity_name(str(alias["alias"]))
                if normalized in ambiguous:
                    continue
                existing = self._aliases.get(normalized)
                if existing and existing[0].entity_id != entity.entity_id:
                    self._aliases.pop(normalized, None)
                    ambiguous.add(normalized)
                    continue
                self._aliases[normalized] = (entity, str(alias["alias"]))
        self._emblems = EmblemRegistry.load(project_root)

    def resolve(self, surface_form: str, *, entity_type: str | None = None) -> ResolvedEntity:
        normalized = normalize_entity_name(surface_form)
        canonical = self._canonical.get(normalized)
        if canonical is not None and (
            entity_type is None or canonical.entity_type == entity_type
        ):
            return canonical
        alias_match = self._aliases.get(normalized)
        if alias_match is not None:
            entity, original_alias = alias_match
            if entity_type is None or entity.entity_type == entity_type:
                confidence = (
                    ConfidenceType.EXACT_ALIAS
                    if surface_form.casefold() == original_alias.casefold()
                    else ConfidenceType.NORMALIZED_ALIAS
                )
                return replace(entity, confidence_type=confidence)
        emblem = self._emblems.resolve(surface_form)
        if emblem is not None and (entity_type is None or entity_type == "club"):
            return ResolvedEntity(
                entity_id=f"club-{emblem.id}",
                entity_type="club",
                canonical_name=emblem.name,
                display_name_ru=emblem.name,
                surname_display=None,
                status="verified",
                provenance="emblem_registry",
                confidence_type=ConfidenceType.EXACT_ALIAS,
                grammatical_forms={},
            )
        return ResolvedEntity(
            entity_id=None,
            entity_type=entity_type or "other",
            canonical_name=surface_form,
            display_name_ru=None,
            surname_display=None,
            status="unresolved",
            provenance="unresolved",
            confidence_type=ConfidenceType.UNRESOLVED,
            grammatical_forms={},
        )
