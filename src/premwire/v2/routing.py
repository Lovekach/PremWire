from __future__ import annotations

from .facts import (
    ClassificationDecision,
    DealType,
    EventType,
    Priority,
    ReportStatus,
    ResolutionStatus,
    StructuredFacts,
)
from .validation import FactValidationReport


SUPPORTED_STATUSES = {ReportStatus.OFFICIAL, ReportStatus.CONFIRMED_REPORT}


class ClassificationRouter:
    version = "1.0"

    def classify(
        self,
        facts: StructuredFacts,
        validation: FactValidationReport,
    ) -> ClassificationDecision:
        rejected: dict[str, list[str]] = {}
        blocking = list(validation.blocking_errors)
        matched: list[str] = []

        if not validation.passed:
            return self._decision(facts, False, None, matched, rejected, blocking)
        if facts.event.status is ReportStatus.RUMOUR:
            blocking.append("BLOCKED_AMBIGUOUS_CATEGORY")
            return self._decision(facts, False, None, matched, rejected, blocking)

        transfer_errors = self._transfer_errors(facts)
        if not transfer_errors:
            matched.extend(["EVENT_TRANSFER", "COMPLETED_STATUS", "RESOLVED_TO_CLUB"])
            return self._decision(
                facts, True, "transfer_v2", matched, rejected, blocking
            )
        rejected["transfer_v2"] = transfer_errors

        loan_errors = self._loan_errors(facts)
        if not loan_errors:
            matched.extend(["EVENT_LOAN", "COMPLETED_STATUS", "RESOLVED_TO_CLUB"])
            return self._decision(facts, True, "loan_v2", matched, rejected, blocking)
        rejected["loan_v2"] = loan_errors

        breaking_errors = self._breaking_errors(facts)
        if not breaking_errors:
            matched.extend(["URGENT_PRIORITY", "VERIFIED_PRIMARY_CLAIM"])
            return self._decision(
                facts, True, "breaking_v2", matched, rejected, blocking
            )
        rejected["breaking_v2"] = breaking_errors

        if facts.event.event_type in {EventType.TRANSFER, EventType.LOAN}:
            blocking.append("BLOCKED_AMBIGUOUS_CATEGORY")
        else:
            blocking.append("BLOCKED_UNSUPPORTED_CATEGORY")
        return self._decision(facts, False, None, matched, rejected, blocking)

    @staticmethod
    def _transfer_errors(facts: StructuredFacts) -> list[str]:
        errors: list[str] = []
        if facts.event.event_type is not EventType.TRANSFER:
            errors.append("EVENT_TYPE_NOT_TRANSFER")
        if facts.event.deal_type in {
            DealType.LOAN,
            DealType.LOAN_WITH_OPTION,
            DealType.LOAN_WITH_OBLIGATION,
        }:
            errors.append("DEAL_TYPE_IS_LOAN")
        if facts.event.status not in SUPPORTED_STATUSES:
            errors.append("STATUS_NOT_COMPLETED_REPORT")
        if facts.subject.resolution_status is not ResolutionStatus.RESOLVED:
            errors.append("SUBJECT_UNRESOLVED")
        if (
            facts.clubs.to_club is None
            or facts.clubs.to_club.resolution_status is not ResolutionStatus.RESOLVED
        ):
            errors.append("TO_CLUB_UNRESOLVED")
        return errors

    @staticmethod
    def _loan_errors(facts: StructuredFacts) -> list[str]:
        errors: list[str] = []
        is_loan = facts.event.event_type is EventType.LOAN or facts.event.deal_type in {
            DealType.LOAN,
            DealType.LOAN_WITH_OPTION,
            DealType.LOAN_WITH_OBLIGATION,
        }
        if not is_loan:
            errors.append("EVENT_TYPE_NOT_LOAN")
        if facts.event.status not in SUPPORTED_STATUSES:
            errors.append("STATUS_NOT_COMPLETED_REPORT")
        if facts.subject.resolution_status is not ResolutionStatus.RESOLVED:
            errors.append("SUBJECT_UNRESOLVED")
        if (
            facts.clubs.to_club is None
            or facts.clubs.to_club.resolution_status is not ResolutionStatus.RESOLVED
        ):
            errors.append("TO_CLUB_UNRESOLVED")
        return errors

    @staticmethod
    def _breaking_errors(facts: StructuredFacts) -> list[str]:
        errors: list[str] = []
        if facts.event.priority is not Priority.URGENT:
            errors.append("PRIORITY_NOT_URGENT")
        if not facts.primary_claim:
            errors.append("PRIMARY_CLAIM_EMPTY")
        subject_resolved = (
            facts.subject.resolution_status is ResolutionStatus.RESOLVED
        )
        club_resolved = any(
            club is not None and club.resolution_status is ResolutionStatus.RESOLVED
            for club in (
                facts.clubs.current,
                facts.clubs.from_club,
                facts.clubs.to_club,
            )
        )
        if not subject_resolved and not club_resolved:
            errors.append("NO_RESOLVED_VISUAL_SUBJECT")
        if facts.event.event_type in {
            EventType.STATISTICS,
            EventType.QUOTE,
            EventType.ADMINISTRATIVE,
            EventType.LINEUP,
            EventType.MATCH,
        }:
            errors.append("UNSUPPORTED_BREAKING_EVENT_TYPE")
        return errors

    @staticmethod
    def _decision(
        facts: StructuredFacts,
        supported: bool,
        intent: str | None,
        matched: list[str],
        rejected: dict[str, list[str]],
        blocking: list[str],
    ) -> ClassificationDecision:
        return ClassificationDecision(
            event_type=facts.event.event_type,
            status=facts.event.status,
            priority=facts.event.priority,
            supported_category=supported,
            template_intent=intent,
            matched_rules=matched,
            rejected_routes=rejected,
            blocking_reasons=list(dict.fromkeys(blocking)),
        )
