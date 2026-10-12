from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EventType(str, Enum):
    TRANSFER = "transfer"
    LOAN = "loan"
    CLUB_NEWS = "club_news"
    INJURY = "injury"
    STATISTICS = "statistics"
    QUOTE = "quote"
    MATCH = "match"
    LINEUP = "lineup"
    ADMINISTRATIVE = "administrative"
    OTHER = "other"


class DealType(str, Enum):
    PERMANENT = "permanent"
    LOAN = "loan"
    LOAN_WITH_OPTION = "loan_with_option"
    LOAN_WITH_OBLIGATION = "loan_with_obligation"
    FREE_TRANSFER = "free_transfer"
    CONTRACT_EXTENSION = "contract_extension"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ReportStatus(str, Enum):
    OFFICIAL = "official"
    CONFIRMED_REPORT = "confirmed_report"
    REPORTED = "reported"
    RUMOUR = "rumour"
    UNKNOWN = "unknown"


class Priority(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"


class VisualSubjectType(str, Enum):
    PLAYER = "player"
    COACH = "coach"
    CLUB = "club"
    STADIUM = "stadium"
    KIT = "kit"
    DOCUMENT = "document"
    NONE = "none"
    UNKNOWN = "unknown"


class EntityType(str, Enum):
    PLAYER = "player"
    COACH = "coach"
    CLUB = "club"
    LEAGUE = "league"
    FEDERATION = "federation"
    OTHER = "other"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class EvidenceType(str, Enum):
    EXPLICIT = "explicit"
    EXPLICIT_NEGATION = "explicit_negation"
    EXPLICIT_RANGE = "explicit_range"
    DETERMINISTIC_DATE_NORMALIZATION = "deterministic_date_normalization"


class SourceFacts(StrictModel):
    platform: str
    account: str
    source_role: str
    trust_tier: int = Field(ge=1)
    url: str
    published_at: str
    original_language: str


class EventFacts(StrictModel):
    event_type: EventType
    deal_type: DealType
    status: ReportStatus
    priority: Priority
    visual_subject_type: VisualSubjectType


class SubjectFacts(StrictModel):
    entity_id: str | None = None
    canonical_name: str
    display_name_ru: str | None = None
    surname_display: str | None = None
    entity_type: EntityType
    position: str | None = None
    resolution_status: ResolutionStatus


class ClubRef(StrictModel):
    entity_id: str | None = None
    canonical_name: str
    display_name_ru: str | None = None
    resolution_status: ResolutionStatus


class ClubsFacts(StrictModel):
    current: ClubRef | None = None
    from_club: ClubRef | None = None
    to_club: ClubRef | None = None
    interested_clubs: list[ClubRef] = Field(default_factory=list)


class DealFacts(StrictModel):
    amount_raw: str | None = None
    amount_value: int | None = Field(default=None, ge=0)
    currency: Literal["EUR", "GBP", "USD"] | None = None
    amount_type: Literal["fixed", "range", "unknown"] | None = None
    amount_min: int | None = Field(default=None, ge=0)
    amount_max: int | None = Field(default=None, ge=0)
    includes_add_ons: bool | None = None
    fixed_amount: int | None = Field(default=None, ge=0)
    add_ons_amount: int | None = Field(default=None, ge=0)
    total_possible_amount: int | None = Field(default=None, ge=0)
    contract_years: int | None = Field(default=None, ge=0)
    contract_until: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "DealFacts":
        if self.amount_type == "range":
            if self.amount_min is None or self.amount_max is None:
                raise ValueError("range requires amount_min and amount_max")
            if self.amount_min > self.amount_max:
                raise ValueError("amount_min cannot exceed amount_max")
        return self


class Evidence(StrictModel):
    field: str
    source_text: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    source_event_id: int = Field(gt=0)
    evidence_type: EvidenceType

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "Evidence":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class StructuredFacts(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    story_id: str
    source: SourceFacts
    event: EventFacts
    subject: SubjectFacts
    clubs: ClubsFacts
    deal: DealFacts
    primary_claim: str
    evidence: list[Evidence] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    forbidden_to_publish_fields: list[str] = Field(default_factory=list)


class ClassificationDecision(StrictModel):
    event_type: EventType
    status: ReportStatus
    priority: Priority
    supported_category: bool
    template_intent: Literal["transfer_v2", "loan_v2", "breaking_v2"] | None = None
    matched_rules: list[str] = Field(default_factory=list)
    rejected_routes: dict[str, list[str]] = Field(default_factory=dict)
    blocking_reasons: list[str] = Field(default_factory=list)


class EditorialOutputContract(StrictModel):
    post_text: str
    primary_title: str
    secondary_title: str
    source_label: str


class RussianCopyContract(StrictModel):
    post_text: str


class HeadlineContract(StrictModel):
    primary_title: str
    secondary_title: str
    source_label: str


class TranslationOutputContract(StrictModel):
    english_text: str
    entity_map: dict[str, str] = Field(default_factory=dict)
    validation_report: dict[str, list[str]] = Field(default_factory=dict)
