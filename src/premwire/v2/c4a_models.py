from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class C4AStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class StrategyBEventType(str, Enum):
    COMPLETED_TRANSFER = "completed_transfer"
    COMPLETED_LOAN = "completed_loan"
    CONFIRMED_TRANSFER_REPORT = "confirmed_transfer_report"
    TRANSFER_RUMOUR = "transfer_rumour"
    CLUB_INTEREST = "club_interest"
    CONTRACT_EXTENSION = "contract_extension"
    NEW_CONTRACT = "new_contract_without_transfer"
    FREE_AGENT = "free_agent"
    PLAYER_RELEASE = "player_release"
    CONTRACT_TERMINATION = "contract_termination"
    RETURN_FROM_LOAN = "return_from_loan"


class FactualStatus(str, Enum):
    OFFICIAL = "official"
    AGREED = "agreed"
    CONFIRMED_REPORT = "confirmed_report"
    AGREEMENT_REPORTED = "agreement_reported"
    BID_REPORTED = "bid_reported"
    TALKS = "talks"
    INTEREST = "interest"
    RUMOUR = "rumour"
    DENIED = "denied"
    NEGATED = "negated"
    HISTORICAL = "historical"


class MovementType(str, Enum):
    PERMANENT = "permanent"
    LOAN = "loan"


class RouteDecision(str, Enum):
    ROUTED = "routed"
    BLOCKED = "blocked"


class SourceRole(str, Enum):
    OFFICIAL_CLUB = "official_club"
    JOURNALIST = "journalist"
    NEWSROOM = "newsroom"
    AGGREGATOR = "aggregator"
    UNKNOWN = "unknown"


class EvidenceSpan(C4AStrictModel):
    field: str
    source_text: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    source_event_id: str

    @model_validator(mode="after")
    def exact_span_is_ordered(self) -> "EvidenceSpan":
        if self.end_offset <= self.start_offset:
            raise ValueError("evidence end_offset must exceed start_offset")
        return self


class SourceIdentity(C4AStrictModel):
    source_id: str
    source_role: SourceRole
    published_at: str
    original_language: Literal["ru", "en", "mixed", "unknown"]
    account_is_attribution: bool = False
    attribution_evidence: EvidenceSpan | None = None

    @model_validator(mode="after")
    def attribution_is_explicit(self) -> "SourceIdentity":
        if not self.account_is_attribution and self.attribution_evidence is None:
            raise ValueError("non-account attribution requires exact text evidence")
        return self


class EntityRef(C4AStrictModel):
    entity_id: str
    entity_type: Literal["player", "club"]
    canonical_name: str
    source_mention: str
    evidence: EvidenceSpan


class MoneyValue(C4AStrictModel):
    raw: str
    value: int | None = Field(default=None, ge=0)
    currency: Literal["EUR", "GBP", "USD"] | None = None
    add_ons: int | None = Field(default=None, ge=0)
    evidence: EvidenceSpan


class ContractTerms(C4AStrictModel):
    raw: str
    old_expiry: str | None = None
    new_expiry: str | None = None
    duration_years: int | None = Field(default=None, ge=1)
    option: str | None = None
    salary_raw: str | None = None
    evidence: EvidenceSpan


class StrategyBEventBase(C4AStrictModel):
    story_id: str
    source: SourceIdentity
    status: FactualStatus
    temporal_qualifier: str | None = None
    evidence_spans: tuple[EvidenceSpan, ...]
    route_decision: Literal[RouteDecision.ROUTED] = RouteDecision.ROUTED
    blocking_reasons: tuple[()] = ()
    negated: Literal[False] = False
    historical: Literal[False] = False


class TransferEvent(StrategyBEventBase):
    schema_type: Literal["transfer"] = "transfer"
    event_type: Literal[
        StrategyBEventType.COMPLETED_TRANSFER,
        StrategyBEventType.COMPLETED_LOAN,
        StrategyBEventType.CONFIRMED_TRANSFER_REPORT,
    ]
    movement_type: MovementType
    subject: EntityRef
    from_club: EntityRef
    to_club: EntityRef
    amount: MoneyValue | None = None
    contract: ContractTerms | None = None

    @model_validator(mode="after")
    def transfer_relations_are_consistent(self) -> "TransferEvent":
        if self.subject.entity_type != "player":
            raise ValueError("transfer subject must be a player")
        if self.from_club.entity_type != "club" or self.to_club.entity_type != "club":
            raise ValueError("transfer endpoints must be clubs")
        if self.from_club.entity_id == self.to_club.entity_id:
            raise ValueError("transfer from/to clubs must differ")
        if self.event_type == StrategyBEventType.COMPLETED_LOAN and self.movement_type != MovementType.LOAN:
            raise ValueError("completed loan requires loan movement")
        if self.status not in {
            FactualStatus.OFFICIAL,
            FactualStatus.AGREED,
            FactualStatus.CONFIRMED_REPORT,
        }:
            raise ValueError("transfer event cannot carry a speculative status")
        return self


class TransferInterestEvent(StrategyBEventBase):
    schema_type: Literal["transfer_interest"] = "transfer_interest"
    event_type: Literal[
        StrategyBEventType.TRANSFER_RUMOUR,
        StrategyBEventType.CLUB_INTEREST,
    ]
    subject: EntityRef
    current_club: EntityRef | None = None
    interested_clubs: tuple[EntityRef, ...]
    amount: MoneyValue | None = None

    @model_validator(mode="after")
    def interest_is_set_valued_and_speculative(self) -> "TransferInterestEvent":
        if self.subject.entity_type != "player":
            raise ValueError("interest subject must be a player")
        ids = [club.entity_id for club in self.interested_clubs]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("interested clubs must be a non-empty canonical set")
        if any(club.entity_type != "club" for club in self.interested_clubs):
            raise ValueError("interested entities must be clubs")
        if self.status not in {
            FactualStatus.AGREEMENT_REPORTED,
            FactualStatus.BID_REPORTED,
            FactualStatus.TALKS,
            FactualStatus.INTEREST,
            FactualStatus.RUMOUR,
        }:
            raise ValueError("interest event cannot carry a completed status")
        return self


class ContractEvent(StrategyBEventBase):
    schema_type: Literal["contract"] = "contract"
    event_type: Literal[
        StrategyBEventType.CONTRACT_EXTENSION,
        StrategyBEventType.NEW_CONTRACT,
    ]
    subject: EntityRef
    club: EntityRef
    terms: ContractTerms

    @model_validator(mode="after")
    def contract_has_no_transfer_relation(self) -> "ContractEvent":
        if self.subject.entity_type != "player" or self.club.entity_type != "club":
            raise ValueError("contract requires one player and one club")
        return self


class PlayerLifecycleEvent(StrategyBEventBase):
    schema_type: Literal["player_lifecycle"] = "player_lifecycle"
    event_type: Literal[
        StrategyBEventType.FREE_AGENT,
        StrategyBEventType.PLAYER_RELEASE,
        StrategyBEventType.CONTRACT_TERMINATION,
        StrategyBEventType.RETURN_FROM_LOAN,
    ]
    subject: EntityRef
    club: EntityRef | None = None
    previous_loan_club: EntityRef | None = None
    parent_club: EntityRef | None = None
    effective_date: str | None = None

    @model_validator(mode="after")
    def lifecycle_relations_match_event(self) -> "PlayerLifecycleEvent":
        if self.subject.entity_type != "player":
            raise ValueError("lifecycle subject must be a player")
        if self.event_type == StrategyBEventType.RETURN_FROM_LOAN:
            if self.previous_loan_club is None or self.parent_club is None:
                raise ValueError("return from loan requires explicit previous and parent clubs")
            if self.previous_loan_club.entity_id == self.parent_club.entity_id:
                raise ValueError("loan clubs must differ")
        elif self.previous_loan_club is not None or self.parent_club is not None:
            raise ValueError("loan relations are only valid for return from loan")
        if self.event_type in {
            StrategyBEventType.PLAYER_RELEASE,
            StrategyBEventType.CONTRACT_TERMINATION,
        } and self.club is None:
            raise ValueError("release or termination requires an explicit club")
        return self


ValidatedStrategyBEvent = Annotated[
    Union[TransferEvent, TransferInterestEvent, ContractEvent, PlayerLifecycleEvent],
    Field(discriminator="schema_type"),
]


class StrategyBParseResult(C4AStrictModel):
    source_event_id: str
    route_decision: RouteDecision
    event: ValidatedStrategyBEvent | None = None
    blocking_reasons: tuple[str, ...] = ()
    grammar_family: str | None = None
    latency_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def route_and_event_are_atomic(self) -> "StrategyBParseResult":
        if self.route_decision == RouteDecision.ROUTED:
            if self.event is None or self.blocking_reasons:
                raise ValueError("routed result requires exactly one event and no blockers")
        elif self.event is not None or not self.blocking_reasons:
            raise ValueError("blocked result requires blockers and no event")
        return self
