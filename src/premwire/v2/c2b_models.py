from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class C2BStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceMetadata(C2BStrictModel):
    story_id: str
    source_event_id: int = Field(gt=0)
    platform: str
    source_account: str
    canonical_source_name: str
    source_role: str
    trust_tier: int = Field(ge=1)
    url: str
    published_at: str
    original_language: str
    original_text: str


class TextSpan(C2BStrictModel):
    span_type: str
    source_text: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "TextSpan":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must exceed start_offset")
        return self


class CandidateType(str, Enum):
    PERSON = "person"
    CLUB = "club"
    ORGANIZATION = "organization"
    MONEY = "money"
    CONTRACT = "contract"
    DATE = "date"
    NEGATION = "negation"
    TENTATIVE = "tentative"
    COMPLETED_MOVE = "completed_move"
    OFFICIAL = "official"
    BREAKING = "breaking"
    JOURNALIST_CONFIRMATION = "journalist_confirmation"


class Candidate(C2BStrictModel):
    candidate_id: str
    candidate_type: CandidateType
    surface: str
    normalized_surface: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    detector: str
    registry_matches: list[str] = Field(default_factory=list)
    type_hints: list[str] = Field(default_factory=list)
    parsed: dict[str, str | int | bool | Decimal | None] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def ordered(self) -> "Candidate":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must exceed start_offset")
        return self


class CandidateBundle(C2BStrictModel):
    detector_version: Literal["1.0"] = "1.0"
    source_event_id: int = Field(gt=0)
    candidates: list[Candidate] = Field(default_factory=list)

    def by_id(self) -> dict[str, Candidate]:
        return {candidate.candidate_id: candidate for candidate in self.candidates}


class SemanticSignals(C2BStrictModel):
    completed_move_explicit: bool
    tentative: bool
    negated: bool
    official_announcement_signal: bool
    confirmed_journalist_signal: bool


class RelationCandidateSelection(C2BStrictModel):
    current_club: str | None = None
    from_club: str | None = None
    to_club: str | None = None
    interested_clubs: list[str] = Field(default_factory=list)


class SemanticProposal(C2BStrictModel):
    schema_version: Literal["c2-semantic-proposal-1.0"] = (
        "c2-semantic-proposal-1.0"
    )
    semantic_category: Literal[
        "transfer",
        "loan",
        "club_news",
        "quote",
        "statistics",
        "administrative",
        "other",
        "unknown",
    ]
    primary_subject_candidate_id: str | None = None
    relation_candidates: RelationCandidateSelection
    money_candidate_ids: list[str] = Field(default_factory=list)
    contract_candidate_ids: list[str] = Field(default_factory=list)
    primary_claim_quote: str
    signals: SemanticSignals
    supporting_quotes: list[str] = Field(default_factory=list)


class ProvenanceType(str, Enum):
    SOURCE_METADATA = "SOURCE_METADATA"
    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    LLM_SELECTED_CANDIDATE = "LLM_SELECTED_CANDIDATE"
    DETERMINISTIC_POLICY = "DETERMINISTIC_POLICY"
    REGISTRY_RESOLUTION = "REGISTRY_RESOLUTION"
    EVIDENCE_MATCH = "EVIDENCE_MATCH"


class FieldProvenance(C2BStrictModel):
    field: str
    sources: list[ProvenanceType]
    candidate_ids: list[str] = Field(default_factory=list)
    evidence_fields: list[str] = Field(default_factory=list)
