from __future__ import annotations

from dataclasses import dataclass, field

from .c2b_models import CandidateBundle, CandidateType, SemanticProposal, SourceMetadata
from .c2b_policies import calculate_status
from .facts import EntityType, ResolutionStatus, StructuredFacts


FACT_VALIDATOR_VERSION = "2.0"
SUPPORTED_CATEGORIES = {"transfer", "loan", "club_news"}
SUBJECT_REQUIRED_CATEGORIES = {"transfer", "loan"}


@dataclass(frozen=True, slots=True)
class C2BValidationReport:
    extraction_valid: bool
    publishable: bool
    blocking_errors: tuple[str, ...] = ()
    route_blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    field_results: dict[str, str] = field(default_factory=dict)
    validator_version: str = FACT_VALIDATOR_VERSION


class FactValidator2:
    def validate(
        self,
        *,
        facts: StructuredFacts,
        metadata: SourceMetadata,
        proposal: SemanticProposal,
        candidates: CandidateBundle,
    ) -> C2BValidationReport:
        errors: list[str] = []
        route_blocks: list[str] = []
        warnings: list[str] = []
        results: dict[str, str] = {}
        evidence_by_field: dict[str, list[object]] = {}

        if facts.story_id != metadata.story_id:
            errors.append("STORY_ID_MISMATCH")
        if (
            facts.source.platform != metadata.platform
            or facts.source.account != metadata.source_account
            or facts.source.url != metadata.url
            or facts.source.source_role != metadata.source_role
            or facts.source.trust_tier != metadata.trust_tier
            or facts.source.published_at != metadata.published_at
        ):
            errors.append("IMMUTABLE_SOURCE_METADATA_MISMATCH")

        for evidence in facts.evidence:
            valid = (
                evidence.source_event_id == metadata.source_event_id
                and evidence.end_offset <= len(metadata.original_text)
                and metadata.original_text[evidence.start_offset : evidence.end_offset]
                == evidence.source_text
            )
            if not valid:
                errors.append(f"INVALID_EVIDENCE:{evidence.field}")
                continue
            evidence_by_field.setdefault(evidence.field, []).append(evidence)
            results[evidence.field] = "verified"

        category = proposal.semantic_category
        if category in SUBJECT_REQUIRED_CATEGORIES:
            if not facts.subject.canonical_name:
                errors.append("PRIMARY_SUBJECT_REQUIRED")
            if facts.subject.resolution_status is ResolutionStatus.UNRESOLVED:
                errors.append("UNRESOLVED_PRIMARY_SUBJECT")
            if "subject.canonical_name" not in evidence_by_field:
                errors.append("MISSING_EVIDENCE:subject.canonical_name")
        elif not facts.subject.canonical_name:
            results["subject.canonical_name"] = "not_required"

        if "primary_claim" not in evidence_by_field:
            errors.append("MISSING_EVIDENCE:primary_claim")

        for field, club in (
            ("clubs.current", facts.clubs.current),
            ("clubs.from_club", facts.clubs.from_club),
            ("clubs.to_club", facts.clubs.to_club),
        ):
            if club is not None:
                if club.resolution_status is ResolutionStatus.UNRESOLVED:
                    errors.append(f"UNRESOLVED_ENTITY:{field}")
                if field not in evidence_by_field:
                    errors.append(f"MISSING_EVIDENCE:{field}")
        for club in facts.clubs.interested_clubs:
            if club.resolution_status is ResolutionStatus.UNRESOLVED:
                errors.append("UNRESOLVED_ENTITY:clubs.interested_clubs")
        if facts.clubs.interested_clubs and "clubs.interested_clubs" not in evidence_by_field:
            errors.append("MISSING_EVIDENCE:clubs.interested_clubs")

        from_club = facts.clubs.from_club
        to_club = facts.clubs.to_club
        if (
            from_club is not None
            and to_club is not None
            and from_club.entity_id is not None
            and to_club.entity_id is not None
            and from_club.entity_id == to_club.entity_id
        ):
            errors.append("FROM_TO_CLUB_IDENTICAL")

        if facts.deal.amount_raw is not None and "deal.amount" not in evidence_by_field:
            errors.append("AMOUNT_WITHOUT_EVIDENCE")
        if (
            facts.deal.contract_until is not None
            or facts.deal.contract_years is not None
        ) and "deal.contract" not in evidence_by_field:
            errors.append("CONTRACT_WITHOUT_EVIDENCE")

        expected_status = calculate_status(metadata, proposal, candidates)
        if facts.event.status != expected_status:
            errors.append("STATUS_STRONGER_THAN_POLICY")

        negations = [
            item for item in candidates.candidates
            if item.candidate_type is CandidateType.NEGATION
        ]
        if proposal.signals.negated and negations:
            claim = proposal.primary_claim_quote.casefold()
            if not any(item.surface.casefold() in claim for item in negations):
                errors.append("NEGATION_LOST")

        if category not in SUPPORTED_CATEGORIES:
            route_blocks.append("BLOCKED_UNSUPPORTED_CATEGORY")
        if category in {"quote", "statistics", "administrative", "other", "unknown"}:
            # These categories may legitimately lack a player subject.
            if facts.subject.entity_type is EntityType.PLAYER and not facts.subject.canonical_name:
                errors.append("EMPTY_PLAYER_SUBJECT")

        return C2BValidationReport(
            extraction_valid=not errors,
            publishable=not errors and not route_blocks,
            blocking_errors=tuple(dict.fromkeys(errors)),
            route_blocking_reasons=tuple(dict.fromkeys(route_blocks)),
            warnings=tuple(dict.fromkeys(warnings)),
            field_results=results,
        )
