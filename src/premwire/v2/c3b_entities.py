from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from premwire.emblem_registry import normalize_entity_name


class EntityMatchMethod(str, Enum):
    EXACT_CANONICAL = "exact_canonical"
    EXACT_REVIEWED_ALIAS = "exact_reviewed_alias"
    DETERMINISTIC_NORMALIZED_ALIAS = "deterministic_normalized_alias"
    DETERMINISTIC_INFLECTED_VARIANT = "deterministic_inflected_variant"
    UNRESOLVED = "unresolved"


class EntityConfidenceClass(str, Enum):
    EXACT = "exact"
    REVIEWED = "reviewed"
    NORMALIZED = "normalized"
    INFLECTED = "inflected"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class CanonicalEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    display_name_ru: str | None
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalEntityMatch:
    source_mention: str
    normalized_surface: str
    start_offset: int | None
    end_offset: int | None
    entity_id: str | None
    entity_type: str
    canonical_name: str | None
    display_name_ru: str | None
    matched_alias: str | None
    match_method: EntityMatchMethod
    confidence_class: EntityConfidenceClass
    ambiguity_reason: str | None

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None


@dataclass(frozen=True, slots=True)
class _AliasCandidate:
    entity: CanonicalEntity
    alias: str
    reviewed: bool
    canonical: bool


def _literal_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _russian_token_variants(token: str) -> set[str]:
    """Return conservative case variants for one existing Russian token."""

    variants = {token}
    if not re.search(r"[А-Яа-яЁё]", token):
        return variants
    if re.fullmatch(r"[А-ЯЁ]{2,6}", token):
        return variants
    lower = token.casefold()
    if lower.endswith("а") and len(token) > 2:
        stem = token[:-1]
        genitive = "и" if stem[-1:].casefold() in "гкхжчшщц" else "ы"
        variants.update({stem + genitive, stem + "е", stem + "у", stem + "ой"})
    elif lower.endswith("я") and len(token) > 2:
        stem = token[:-1]
        variants.update({stem + "и", stem + "е", stem + "ю", stem + "ей"})
    elif (lower.endswith("й") or lower.endswith("ь")) and len(token) > 2:
        stem = token[:-1]
        variants.update({stem + "я", stem + "ю", stem + "ем", stem + "е"})
    elif re.search(r"[бвгджзклмнпрстфхцчшщ]$", lower) and len(token) > 3:
        variants.update({token + "а", token + "у", token + "ом", token + "е"})
    return variants


def deterministic_inflected_variants(alias: str) -> set[str]:
    """Inflect one token or one hyphen segment at a time, never the whole phrase."""

    variants = {alias}
    tokens = alias.split()
    for token_index, token in enumerate(tokens):
        pieces = token.split("-")
        for piece_index, piece in enumerate(pieces):
            for variant in _russian_token_variants(piece):
                changed_pieces = list(pieces)
                changed_pieces[piece_index] = variant
                changed_tokens = list(tokens)
                changed_tokens[token_index] = "-".join(changed_pieces)
                variants.add(" ".join(changed_tokens))
    return variants


class CanonicalEntityResolverV2:
    """Resolve existing entities only through deterministic, unique whole aliases."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        policy_path = project_root / "config" / "entity_canonicalization_stage_c3b.json"
        self.policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.version = str(self.policy["policy_version"])
        self._minimum_length = int(self.policy["minimum_normalized_alias_length"])

        candidates: list[_AliasCandidate] = []
        candidates.extend(self._base_registry_candidates())
        candidates.extend(self._reviewed_registry_candidates())
        candidates.extend(self._c3b_reviewed_registry_candidates())
        candidates.extend(self._club_registry_candidates())
        self._exact_canonical = self._unique_index(
            item for item in candidates if item.canonical
        )
        self._exact_reviewed = self._unique_index(
            item for item in candidates if item.reviewed and not item.canonical
        )
        self._normalized = self._unique_normalized_index(candidates)
        self._inflected = self._unique_inflected_index(candidates)

    @staticmethod
    def _entity(
        *, entity_id: str, entity_type: str, canonical_name: str,
        display_name_ru: str | None, provenance: str,
    ) -> CanonicalEntity:
        return CanonicalEntity(
            entity_id=entity_id,
            entity_type=entity_type,
            canonical_name=canonical_name,
            display_name_ru=display_name_ru,
            provenance=(provenance,),
        )

    def _base_registry_candidates(self) -> list[_AliasCandidate]:
        payload = json.loads(
            (self.project_root / "config" / "entity_registry.json").read_text(
                encoding="utf-8"
            )
        )
        result: list[_AliasCandidate] = []
        for raw in payload["entities"]:
            entity = self._entity(
                entity_id=str(raw["entity_id"]),
                entity_type=str(raw["entity_type"]),
                canonical_name=str(raw["canonical_name"]),
                display_name_ru=raw.get("display_name_ru"),
                provenance="entity_registry",
            )
            result.append(_AliasCandidate(entity, entity.canonical_name, True, True))
            for alias in raw.get("aliases", []):
                surface = str(alias["alias"])
                result.append(
                    _AliasCandidate(
                        entity, surface, True,
                        _literal_key(surface) == _literal_key(entity.canonical_name),
                    )
                )
        return result

    def _reviewed_registry_candidates(self) -> list[_AliasCandidate]:
        payload = json.loads(
            (
                self.project_root
                / "config"
                / "entity_registry_stage_c2_reviewed.json"
            ).read_text(encoding="utf-8")
        )
        result: list[_AliasCandidate] = []
        for raw in payload["aliases"]:
            if raw.get("reviewed") is not True:
                continue
            entity = self._entity(
                entity_id=str(raw["entity_id"]),
                entity_type=str(raw["entity_type"]),
                canonical_name=str(raw.get("canonical_name") or raw["alias"]),
                display_name_ru=(
                    str(raw["alias"])
                    if re.search(r"[А-Яа-яЁё]", str(raw["alias"]))
                    else None
                ),
                provenance="stage_c2_reviewed",
            )
            result.append(_AliasCandidate(entity, str(raw["alias"]), True, False))
            result.append(
                _AliasCandidate(entity, entity.canonical_name, True, True)
            )
        return result

    def _club_registry_candidates(self) -> list[_AliasCandidate]:
        payload = json.loads(
            (self.project_root / "config" / "club-emblems.json").read_text(
                encoding="utf-8"
            )
        )
        result: list[_AliasCandidate] = []
        for raw in payload["clubs"]:
            canonical_name = str(raw.get("name_en") or raw.get("name") or raw["id"])
            entity = self._entity(
                entity_id=f"club-{raw['id']}",
                entity_type="club",
                canonical_name=canonical_name,
                display_name_ru=str(raw.get("name") or canonical_name),
                provenance="club_emblems",
            )
            names = {
                canonical_name,
                str(raw.get("name") or ""),
                *(str(item) for item in raw.get("aliases_ru", [])),
                *(str(item) for item in raw.get("aliases_en", [])),
            }
            for name in (item for item in names if item):
                result.append(
                    _AliasCandidate(
                        entity,
                        name,
                        True,
                        _literal_key(name) == _literal_key(canonical_name),
                    )
                )
        return result

    def _c3b_reviewed_registry_candidates(self) -> list[_AliasCandidate]:
        payload = json.loads(
            (
                self.project_root
                / "config"
                / "entity_registry_stage_c3b_reviewed.json"
            ).read_text(encoding="utf-8")
        )
        result: list[_AliasCandidate] = []
        for raw in payload["aliases"]:
            if raw.get("reviewed") is not True:
                continue
            alias = str(raw["alias"])
            entity = self._entity(
                entity_id=str(raw["entity_id"]),
                entity_type=str(raw["entity_type"]),
                canonical_name=str(raw["canonical_name"]),
                display_name_ru=(
                    alias if re.search(r"[А-Яа-яЁё]", alias) else None
                ),
                provenance="stage_c3b_reviewed",
            )
            result.append(_AliasCandidate(entity, alias, True, False))
            result.append(_AliasCandidate(entity, entity.canonical_name, True, True))
        return result

    def _unique_index(
        self, candidates: Iterable[_AliasCandidate]
    ) -> dict[str, _AliasCandidate | tuple[str, ...]]:
        grouped: dict[str, list[_AliasCandidate]] = {}
        for candidate in candidates:
            if len(normalize_entity_name(candidate.alias)) < self._minimum_length:
                continue
            grouped.setdefault(_literal_key(candidate.alias), []).append(candidate)
        return self._collapse(grouped)

    def _unique_normalized_index(
        self, candidates: Iterable[_AliasCandidate]
    ) -> dict[str, _AliasCandidate | tuple[str, ...]]:
        grouped: dict[str, list[_AliasCandidate]] = {}
        for candidate in candidates:
            key = normalize_entity_name(candidate.alias)
            if len(key) >= self._minimum_length:
                grouped.setdefault(key, []).append(candidate)
        return self._collapse(grouped)

    def _unique_inflected_index(
        self, candidates: Iterable[_AliasCandidate]
    ) -> dict[str, _AliasCandidate | tuple[str, ...]]:
        grouped: dict[str, list[_AliasCandidate]] = {}
        for candidate in candidates:
            if not re.search(r"[А-Яа-яЁё]", candidate.alias):
                continue
            original = normalize_entity_name(candidate.alias)
            for variant in deterministic_inflected_variants(candidate.alias):
                key = normalize_entity_name(variant)
                if key == original or len(key) < self._minimum_length:
                    continue
                grouped.setdefault(key, []).append(candidate)
        return self._collapse(grouped)

    @staticmethod
    def _collapse(
        grouped: dict[str, list[_AliasCandidate]]
    ) -> dict[str, _AliasCandidate | tuple[str, ...]]:
        result: dict[str, _AliasCandidate | tuple[str, ...]] = {}
        for key, values in grouped.items():
            entity_ids = tuple(sorted({item.entity.entity_id for item in values}))
            if len(entity_ids) == 1:
                # Prefer reviewed/canonical metadata for the same entity.
                result[key] = sorted(
                    values,
                    key=lambda item: (
                        not item.reviewed,
                        not item.canonical,
                        len(item.alias),
                    ),
                )[0]
            else:
                result[key] = entity_ids
        return result

    def resolve(
        self,
        source_mention: str,
        *,
        entity_type: str | None = None,
        start_offset: int | None = None,
        end_offset: int | None = None,
    ) -> CanonicalEntityMatch:
        normalized = normalize_entity_name(source_mention)
        attempts = (
            (
                self._exact_canonical,
                _literal_key(source_mention),
                EntityMatchMethod.EXACT_CANONICAL,
                EntityConfidenceClass.EXACT,
            ),
            (
                self._exact_reviewed,
                _literal_key(source_mention),
                EntityMatchMethod.EXACT_REVIEWED_ALIAS,
                EntityConfidenceClass.REVIEWED,
            ),
            (
                self._normalized,
                normalized,
                EntityMatchMethod.DETERMINISTIC_NORMALIZED_ALIAS,
                EntityConfidenceClass.NORMALIZED,
            ),
            (
                self._inflected,
                normalized,
                EntityMatchMethod.DETERMINISTIC_INFLECTED_VARIANT,
                EntityConfidenceClass.INFLECTED,
            ),
        )
        ambiguity: tuple[str, ...] | None = None
        for index, key, method, confidence in attempts:
            matched = index.get(key)
            if matched is None:
                continue
            if isinstance(matched, tuple):
                ambiguity = matched
                break
            if entity_type is not None and matched.entity.entity_type != entity_type:
                continue
            entity = matched.entity
            return CanonicalEntityMatch(
                source_mention=source_mention,
                normalized_surface=normalized,
                start_offset=start_offset,
                end_offset=end_offset,
                entity_id=entity.entity_id,
                entity_type=entity.entity_type,
                canonical_name=entity.canonical_name,
                display_name_ru=entity.display_name_ru,
                matched_alias=matched.alias,
                match_method=method,
                confidence_class=confidence,
                ambiguity_reason=None,
            )
        return CanonicalEntityMatch(
            source_mention=source_mention,
            normalized_surface=normalized,
            start_offset=start_offset,
            end_offset=end_offset,
            entity_id=None,
            entity_type=entity_type or "unknown",
            canonical_name=None,
            display_name_ru=None,
            matched_alias=None,
            match_method=EntityMatchMethod.UNRESOLVED,
            confidence_class=EntityConfidenceClass.UNRESOLVED,
            ambiguity_reason=(
                f"AMBIGUOUS_ALIAS:{','.join(ambiguity)}"
                if ambiguity
                else "UNKNOWN_ENTITY"
            ),
        )
