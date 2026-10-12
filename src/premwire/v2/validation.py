from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import (
    Evidence,
    EventType,
    ReportStatus,
    ResolutionStatus,
    StructuredFacts,
)
from .resolution import OFFICIAL_SOURCE_ROLES, SourceRecord


FACT_VALIDATOR_VERSION = "1.0"
MONEY_RE = re.compile(
    r"(?P<symbol>[€£$])\s*(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>m|mn|million|млн|k|тыс)?",
    re.IGNORECASE,
)
SYMBOL_CURRENCY = {"€": "EUR", "£": "GBP", "$": "USD"}
UNIT_MULTIPLIER = {
    "m": 1_000_000,
    "mn": 1_000_000,
    "million": 1_000_000,
    "млн": 1_000_000,
    "k": 1_000,
    "тыс": 1_000,
    None: 1,
}
NEGATION_RE = re.compile(
    r"\b(not|no longer|won['’]t|will not|does not|do not|не|нет|не будет|"
    r"не планирует|не собирается)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedAmount:
    raw: str
    value: int
    currency: str


def parse_amount(value: str) -> ParsedAmount | None:
    match = MONEY_RE.fullmatch(value.strip())
    if match is None:
        return None
    number = float(match.group("number").replace(",", "."))
    multiplier = UNIT_MULTIPLIER[match.group("unit").casefold() if match.group("unit") else None]
    return ParsedAmount(
        raw=value,
        value=int(number * multiplier),
        currency=SYMBOL_CURRENCY[match.group("symbol")],
    )


@dataclass(frozen=True, slots=True)
class FactValidationReport:
    passed: bool
    blocking_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    field_results: dict[str, str] = field(default_factory=dict)
    validator_version: str = FACT_VALIDATOR_VERSION


class FactValidator:
    def validate(
        self,
        facts: StructuredFacts,
        *,
        original_text: str,
        source_event_id: int,
        source_record: SourceRecord | None,
        expected_story_id: str | None = None,
    ) -> FactValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        results: dict[str, str] = {}
        evidence_by_field: dict[str, list[Evidence]] = {}

        if expected_story_id is not None and facts.story_id != expected_story_id:
            errors.append("STORY_ID_MISMATCH")
        if source_event_id <= 0:
            errors.append("SOURCE_EVENT_NOT_FOUND")

        for evidence in facts.evidence:
            valid = (
                evidence.source_event_id == source_event_id
                and evidence.end_offset <= len(original_text)
                and original_text[evidence.start_offset : evidence.end_offset]
                == evidence.source_text
            )
            if not valid:
                errors.append(f"INVALID_EVIDENCE:{evidence.field}")
                results[evidence.field] = "invalid_evidence"
                continue
            evidence_by_field.setdefault(evidence.field, []).append(evidence)
            results[evidence.field] = "verified"

        if source_record is None:
            errors.append("UNKNOWN_SOURCE")
        else:
            if source_record.source_role != facts.source.source_role:
                errors.append("SOURCE_ROLE_MISMATCH")
            if source_record.trust_tier != facts.source.trust_tier:
                errors.append("SOURCE_TRUST_MISMATCH")

        self._validate_status(facts, original_text, source_record, errors)
        self._validate_entities(facts, errors)
        self._validate_required_evidence(facts, evidence_by_field, errors, results)
        self._validate_amount(facts, evidence_by_field, errors, results)
        self._validate_contract(facts, evidence_by_field, errors, results)
        self._validate_negation(facts, original_text, errors)

        if facts.event.event_type is EventType.STATISTICS:
            errors.append("BLOCKED_UNSUPPORTED_CATEGORY")
        for uncertain in facts.uncertain_fields:
            if uncertain in evidence_by_field:
                errors.append(f"UNCERTAIN_FIELD_MARKED_VERIFIED:{uncertain}")

        return FactValidationReport(
            passed=not errors,
            blocking_errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(dict.fromkeys(warnings)),
            field_results=results,
        )

    @staticmethod
    def _validate_status(
        facts: StructuredFacts,
        original_text: str,
        source_record: SourceRecord | None,
        errors: list[str],
    ) -> None:
        if facts.event.status is ReportStatus.OFFICIAL:
            if source_record is None or source_record.source_role not in OFFICIAL_SOURCE_ROLES:
                errors.append("OFFICIAL_REQUIRES_OFFICIAL_SOURCE")
            if "here we go" in original_text.casefold():
                errors.append("HERE_WE_GO_NOT_OFFICIAL")
        tentative = re.search(
            r"\b(may|could|considering|interested|interest|possibility|"
            r"может|рассматривает|интересуется|возможность)\b",
            original_text,
            re.IGNORECASE,
        )
        if tentative and facts.event.status in {
            ReportStatus.OFFICIAL,
            ReportStatus.CONFIRMED_REPORT,
        }:
            errors.append("STATUS_OVERSTATED")
        if facts.event.status is ReportStatus.RUMOUR and facts.event.event_type in {
            EventType.TRANSFER,
            EventType.LOAN,
        }:
            errors.append("RUMOUR_NOT_COMPLETED_DEAL")

    @staticmethod
    def _validate_entities(facts: StructuredFacts, errors: list[str]) -> None:
        if facts.subject.resolution_status is ResolutionStatus.UNRESOLVED:
            errors.append("UNRESOLVED_PRIMARY_SUBJECT")
        for field_name, club in (
            ("clubs.current", facts.clubs.current),
            ("clubs.from_club", facts.clubs.from_club),
            ("clubs.to_club", facts.clubs.to_club),
        ):
            if club is not None and club.resolution_status is ResolutionStatus.UNRESOLVED:
                errors.append(f"UNRESOLVED_ENTITY:{field_name}")
        for club in facts.clubs.interested_clubs:
            if club.resolution_status is ResolutionStatus.UNRESOLVED:
                errors.append("UNRESOLVED_ENTITY:clubs.interested_clubs")
        if (
            facts.clubs.from_club is not None
            and facts.clubs.to_club is not None
            and facts.clubs.from_club.entity_id == facts.clubs.to_club.entity_id
        ):
            errors.append("FROM_TO_CLUB_IDENTICAL")

    @staticmethod
    def _validate_required_evidence(
        facts: StructuredFacts,
        evidence: dict[str, list[Evidence]],
        errors: list[str],
        results: dict[str, str],
    ) -> None:
        required = {"primary_claim", "subject.canonical_name"}
        if facts.clubs.to_club is not None:
            required.add("clubs.to_club")
        if facts.clubs.from_club is not None:
            required.add("clubs.from_club")
        for field_name in sorted(required):
            if field_name not in evidence:
                errors.append(f"MISSING_EVIDENCE:{field_name}")
                results[field_name] = "missing"

    @staticmethod
    def _validate_amount(
        facts: StructuredFacts,
        evidence: dict[str, list[Evidence]],
        errors: list[str],
        results: dict[str, str],
    ) -> None:
        deal = facts.deal
        if deal.amount_value is None and deal.currency is None and deal.amount_raw is None:
            return
        amount_evidence = evidence.get("deal.amount")
        if not amount_evidence:
            errors.append("AMOUNT_WITHOUT_EVIDENCE")
            results["deal.amount"] = "missing"
            return
        parsed = parse_amount(amount_evidence[0].source_text)
        if parsed is None:
            errors.append("AMOUNT_UNPARSEABLE")
            return
        if parsed.value != deal.amount_value:
            errors.append("AMOUNT_VALUE_MISMATCH")
        if parsed.currency != deal.currency:
            errors.append("AMOUNT_CURRENCY_MISMATCH")

    @staticmethod
    def _validate_contract(
        facts: StructuredFacts,
        evidence: dict[str, list[Evidence]],
        errors: list[str],
        results: dict[str, str],
    ) -> None:
        if facts.deal.contract_years is None and facts.deal.contract_until is None:
            return
        if "deal.contract" not in evidence:
            errors.append("CONTRACT_WITHOUT_EVIDENCE")
            results["deal.contract"] = "missing"

    @staticmethod
    def _validate_negation(
        facts: StructuredFacts,
        original_text: str,
        errors: list[str],
    ) -> None:
        if NEGATION_RE.search(original_text) and not NEGATION_RE.search(
            facts.primary_claim
        ):
            errors.append("NEGATION_LOST")
