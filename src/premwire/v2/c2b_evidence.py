from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from premwire.emblem_registry import normalize_entity_name

from .c2b_models import Candidate
from .facts import Evidence, EvidenceType
from .resolution import ConfidenceType, EntityResolver, ResolvedEntity


EVIDENCE_BUILDER_VERSION = "1.0"


class EvidenceMatchError(ValueError):
    pass


def _quote_space_view(text: str) -> tuple[str, tuple[int, ...]]:
    quote_map = {"“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "’": "'", "‘": "'"}
    output: list[str] = []
    mapping: list[int] = []
    space = False
    for index, char in enumerate(text):
        value = quote_map.get(char, char)
        if value.isspace():
            if space:
                continue
            value = " "
            space = True
        else:
            space = False
        output.append(value)
        mapping.append(index)
    return "".join(output), tuple(mapping)


class EvidenceBuilder:
    def build_quote(
        self, *, field: str, quote: str, original_text: str, source_event_id: int
    ) -> Evidence:
        exact = [i for i in range(len(original_text)) if original_text.startswith(quote, i)]
        if len(exact) == 1:
            start = exact[0]
            end = start + len(quote)
        elif len(exact) > 1:
            raise EvidenceMatchError("AMBIGUOUS_EVIDENCE")
        else:
            view, mapping = _quote_space_view(original_text)
            needle, _ = _quote_space_view(quote)
            matches = [i for i in range(len(view)) if view.startswith(needle, i)]
            if len(matches) != 1:
                raise EvidenceMatchError(
                    "AMBIGUOUS_EVIDENCE" if len(matches) > 1 else "EVIDENCE_NOT_FOUND"
                )
            start = mapping[matches[0]]
            end = mapping[matches[0] + len(needle) - 1] + 1
        return Evidence(
            field=field,
            source_text=original_text[start:end],
            start_offset=start,
            end_offset=end,
            source_event_id=source_event_id,
            evidence_type=EvidenceType.EXPLICIT,
        )

    @staticmethod
    def from_candidate(
        *, field: str, candidate: Candidate, original_text: str, source_event_id: int
    ) -> Evidence:
        if original_text[candidate.start_offset : candidate.end_offset] != candidate.surface:
            raise EvidenceMatchError("CANDIDATE_OFFSET_MISMATCH")
        evidence_type = (
            EvidenceType.EXPLICIT_NEGATION
            if candidate.candidate_type.value == "negation"
            else EvidenceType.EXPLICIT
        )
        return Evidence(
            field=field,
            source_text=candidate.surface,
            start_offset=candidate.start_offset,
            end_offset=candidate.end_offset,
            source_event_id=source_event_id,
            evidence_type=evidence_type,
        )


@dataclass(frozen=True, slots=True)
class ReviewedAlias:
    entity_id: str
    entity_type: str
    canonical_name: str
    alias: str


class C2BEntityResolver:
    def __init__(self, *, project_root: Path) -> None:
        self.base = EntityResolver(project_root=project_root)
        payload = json.loads(
            (project_root / "config" / "entity_registry_stage_c2_reviewed.json")
            .read_text(encoding="utf-8")
        )
        self.reviewed: dict[str, ReviewedAlias] = {}
        for item in payload["aliases"]:
            if item.get("reviewed") is not True:
                continue
            alias = ReviewedAlias(
                entity_id=str(item["entity_id"]),
                entity_type=str(item["entity_type"]),
                canonical_name=str(item.get("canonical_name") or item["alias"]),
                alias=str(item["alias"]),
            )
            self.reviewed[normalize_entity_name(alias.alias)] = alias

    def resolve(self, candidate: Candidate) -> ResolvedEntity:
        requested_type = {
            "person": None,
            "club": "club",
            "organization": None,
        }.get(candidate.candidate_type.value)
        resolved = self.base.resolve(candidate.surface, entity_type=requested_type)
        if resolved.resolved:
            return resolved
        alias = self.reviewed.get(normalize_entity_name(candidate.surface))
        if alias is None:
            return resolved
        return ResolvedEntity(
            entity_id=alias.entity_id,
            entity_type=alias.entity_type,
            canonical_name=alias.canonical_name,
            display_name_ru=alias.alias if any("а" <= c.casefold() <= "я" for c in alias.alias) else None,
            surname_display=None,
            status="verified",
            provenance="stage_c2_reviewed",
            confidence_type=ConfidenceType.NORMALIZED_ALIAS,
            grammatical_forms={},
        )
