from __future__ import annotations

from dataclasses import dataclass

from .c2b_evidence import C2BEntityResolver, EvidenceBuilder
from .c2b_models import (
    Candidate,
    CandidateBundle,
    CandidateType,
    FieldProvenance,
    ProvenanceType,
    SemanticProposal,
    SourceMetadata,
)
from .facts import (
    ClubRef,
    ClubsFacts,
    DealFacts,
    DealType,
    EntityType,
    EventFacts,
    EventType,
    Priority,
    ReportStatus,
    ResolutionStatus,
    SourceFacts,
    StructuredFacts,
    SubjectFacts,
    VisualSubjectType,
)


STATUS_POLICY_VERSION = "2.0"
AMOUNT_PARSER_VERSION = "2.0"
CONTRACT_PARSER_VERSION = "2.0"
RELATION_RESOLVER_VERSION = "2.0"


def _has(bundle: CandidateBundle, kind: CandidateType) -> bool:
    return any(item.candidate_type is kind for item in bundle.candidates)


def calculate_status(
    metadata: SourceMetadata, proposal: SemanticProposal, bundle: CandidateBundle
) -> ReportStatus:
    negated = proposal.signals.negated and _has(bundle, CandidateType.NEGATION)
    tentative = proposal.signals.tentative and _has(bundle, CandidateType.TENTATIVE)
    completed = (
        proposal.signals.completed_move_explicit
        and _has(bundle, CandidateType.COMPLETED_MOVE)
    )
    official = (
        proposal.signals.official_announcement_signal
        and _has(bundle, CandidateType.OFFICIAL)
    )
    journalist = (
        proposal.signals.confirmed_journalist_signal
        and _has(bundle, CandidateType.JOURNALIST_CONFIRMATION)
    )
    if negated:
        return ReportStatus.REPORTED
    official_roles = {
        "official_club",
        "official_league",
        "official_federation",
        "official_player",
    }
    if metadata.source_role in official_roles and (official or completed):
        return ReportStatus.OFFICIAL
    if completed and (journalist or metadata.source_role in {"journalist", "aggregator", "media"}):
        return ReportStatus.CONFIRMED_REPORT
    if tentative:
        interest_words = {"interested", "interest", "considering", "заинтерес", "интересуется"}
        surfaces = " ".join(
            item.surface.casefold()
            for item in bundle.candidates
            if item.candidate_type is CandidateType.TENTATIVE
        )
        return (
            ReportStatus.RUMOUR
            if any(word in surfaces for word in interest_words)
            else ReportStatus.REPORTED
        )
    return ReportStatus.REPORTED if completed else ReportStatus.UNKNOWN


def assemble_deal(
    proposal: SemanticProposal, bundle: CandidateBundle
) -> tuple[DealFacts, list[Candidate]]:
    by_id = bundle.by_id()
    money = [by_id[item] for item in proposal.money_candidate_ids]
    contracts = [by_id[item] for item in proposal.contract_candidate_ids]
    values: dict[str, object] = {}
    if len(money) == 1:
        candidate = money[0]
        values.update(candidate.parsed)
        values["amount_raw"] = candidate.surface
    elif len(money) > 1:
        # Multiple unrelated figures remain intentionally unpublished.
        money = []
    for candidate in contracts:
        for key, value in candidate.parsed.items():
            if key in {"contract_until", "contract_years"} and value is not None:
                values.setdefault(key, value)
    allowed = set(DealFacts.model_fields)
    return DealFacts(**{key: value for key, value in values.items() if key in allowed}), money + contracts


def _club_ref(resolved: object) -> ClubRef:
    entity = resolved
    return ClubRef(
        entity_id=entity.entity_id,
        canonical_name=entity.canonical_name,
        display_name_ru=entity.display_name_ru,
        resolution_status=(
            ResolutionStatus.RESOLVED if entity.resolved else ResolutionStatus.UNRESOLVED
        ),
    )


def resolve_relations(
    proposal: SemanticProposal,
    bundle: CandidateBundle,
    resolver: C2BEntityResolver,
) -> tuple[ClubsFacts, dict[str, Candidate]]:
    by_id = bundle.by_id()
    selections = proposal.relation_candidates
    used: dict[str, Candidate] = {}

    def one(field: str, candidate_id: str | None) -> ClubRef | None:
        if candidate_id is None:
            return None
        candidate = by_id[candidate_id]
        if candidate.candidate_type is not CandidateType.CLUB:
            raise ValueError(f"RELATION_NOT_CLUB:{field}")
        resolved = resolver.resolve(candidate)
        if resolved.entity_type != "club":
            raise ValueError(f"RELATION_NOT_CLUB:{field}")
        used[field] = candidate
        return _club_ref(resolved)

    interested = []
    for index, candidate_id in enumerate(selections.interested_clubs):
        value = one(f"clubs.interested_clubs[{index}]", candidate_id)
        if value is not None:
            interested.append(value)
    facts = ClubsFacts(
        current=one("clubs.current", selections.current_club),
        from_club=one("clubs.from_club", selections.from_club),
        to_club=one("clubs.to_club", selections.to_club),
        interested_clubs=interested,
    )
    if (
        facts.from_club
        and facts.to_club
        and facts.from_club.entity_id is not None
        and facts.to_club.entity_id is not None
        and facts.from_club.entity_id == facts.to_club.entity_id
    ):
        raise ValueError("FROM_TO_CLUB_IDENTICAL")
    return facts, used


@dataclass(frozen=True, slots=True)
class AssembledFacts:
    facts: StructuredFacts
    provenance: tuple[FieldProvenance, ...]


class C2FactsAssembler:
    def __init__(self, *, resolver: C2BEntityResolver) -> None:
        self.resolver = resolver
        self.evidence = EvidenceBuilder()

    def assemble(
        self,
        *,
        metadata: SourceMetadata,
        proposal: SemanticProposal,
        candidates: CandidateBundle,
    ) -> AssembledFacts:
        by_id = candidates.by_id()
        evidence = [
            self.evidence.build_quote(
                field="primary_claim",
                quote=proposal.primary_claim_quote,
                original_text=metadata.original_text,
                source_event_id=metadata.source_event_id,
            )
        ]
        subject_candidate = (
            by_id[proposal.primary_subject_candidate_id]
            if proposal.primary_subject_candidate_id
            else None
        )
        if subject_candidate:
            resolved = self.resolver.resolve(subject_candidate)
            subject = SubjectFacts(
                entity_id=resolved.entity_id,
                canonical_name=resolved.canonical_name,
                display_name_ru=resolved.display_name_ru,
                surname_display=resolved.surname_display,
                entity_type=(
                    EntityType(resolved.entity_type)
                    if resolved.entity_type in {item.value for item in EntityType}
                    else EntityType.OTHER
                ),
                resolution_status=(
                    ResolutionStatus.RESOLVED
                    if resolved.resolved
                    else ResolutionStatus.UNRESOLVED
                ),
            )
            evidence.append(
                self.evidence.from_candidate(
                    field="subject.canonical_name",
                    candidate=subject_candidate,
                    original_text=metadata.original_text,
                    source_event_id=metadata.source_event_id,
                )
            )
        else:
            subject = SubjectFacts(
                canonical_name="",
                entity_type=EntityType.OTHER,
                resolution_status=ResolutionStatus.UNRESOLVED,
            )
        clubs, relation_candidates = resolve_relations(
            proposal, candidates, self.resolver
        )
        for field, candidate in relation_candidates.items():
            evidence.append(
                self.evidence.from_candidate(
                    field=field.split("[", 1)[0],
                    candidate=candidate,
                    original_text=metadata.original_text,
                    source_event_id=metadata.source_event_id,
                )
            )
        deal, deal_candidates = assemble_deal(proposal, candidates)
        for candidate in deal_candidates:
            field = (
                "deal.amount"
                if candidate.candidate_type is CandidateType.MONEY
                else "deal.contract"
            )
            evidence.append(
                self.evidence.from_candidate(
                    field=field,
                    candidate=candidate,
                    original_text=metadata.original_text,
                    source_event_id=metadata.source_event_id,
                )
            )
        category = proposal.semantic_category
        event_type = EventType(category if category != "unknown" else "other")
        status = calculate_status(metadata, proposal, candidates)
        urgent = _has(candidates, CandidateType.BREAKING)
        facts = StructuredFacts(
            story_id=metadata.story_id,
            source=SourceFacts(
                platform=metadata.platform,
                account=metadata.source_account,
                source_role=metadata.source_role,
                trust_tier=metadata.trust_tier,
                url=metadata.url,
                published_at=metadata.published_at,
                original_language=metadata.original_language,
            ),
            event=EventFacts(
                event_type=event_type,
                deal_type=(
                    DealType.LOAN
                    if category == "loan"
                    else DealType.PERMANENT
                    if category == "transfer"
                    else DealType.NOT_APPLICABLE
                ),
                status=status,
                priority=Priority.URGENT if urgent else Priority.NORMAL,
                visual_subject_type=(
                    VisualSubjectType.PLAYER if subject.entity_type is EntityType.PLAYER else
                    VisualSubjectType.COACH if subject.entity_type is EntityType.COACH else
                    VisualSubjectType.CLUB if subject.entity_type is EntityType.CLUB else
                    VisualSubjectType.DOCUMENT if category in {"quote", "administrative"} else
                    VisualSubjectType.UNKNOWN
                ),
            ),
            subject=subject,
            clubs=clubs,
            deal=deal,
            primary_claim=proposal.primary_claim_quote,
            evidence=evidence,
        )
        provenance = (
            FieldProvenance(field="source", sources=[ProvenanceType.SOURCE_METADATA]),
            FieldProvenance(
                field="subject",
                sources=[ProvenanceType.LLM_SELECTED_CANDIDATE, ProvenanceType.REGISTRY_RESOLUTION],
                candidate_ids=[subject_candidate.candidate_id] if subject_candidate else [],
            ),
            FieldProvenance(field="event.status", sources=[ProvenanceType.DETERMINISTIC_POLICY]),
            FieldProvenance(field="primary_claim", sources=[ProvenanceType.EVIDENCE_MATCH]),
            FieldProvenance(
                field="deal",
                sources=[ProvenanceType.DETERMINISTIC_CANDIDATE],
                candidate_ids=[item.candidate_id for item in deal_candidates],
            ),
        )
        return AssembledFacts(facts=facts, provenance=provenance)
