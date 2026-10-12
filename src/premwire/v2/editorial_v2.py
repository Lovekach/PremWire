from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .facts import (
    ClassificationDecision,
    HeadlineContract,
    RussianCopyContract,
    StructuredFacts,
    TranslationOutputContract,
)
from .models import LlmTaskType
from .orchestration import StructuredLlmOrchestrator
from .resolution import SourceRecord
from .validation import FactValidationReport, NEGATION_RE


URL_RE = re.compile(r"https?://|www\.|t\.me/", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-zА-Яа-я0-9_]+")
CTA_RE = re.compile(
    r"\b(подписывай|подпишись|присоединяй|переходи по ссылке|"
    r"subscribe|follow us|join us)\b",
    re.IGNORECASE,
)
TRIVIAL_QUESTION_RE = re.compile(
    r"(что думаете|как вам|согласны|ваше мнение)\s*\?\s*$",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{4,}\b")


@dataclass(frozen=True, slots=True)
class CopyValidationReport:
    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HeadlineValidationReport:
    passed: bool
    errors: tuple[str, ...] = ()


def build_copy_prompt(
    facts: StructuredFacts,
    *,
    original_text: str,
    source_label: str,
    category: str,
) -> str:
    return (
        "Return JSON only. Write a natural Russian football news post using only "
        "VALIDATED_FACTS. Do not add facts, links, handles, CTA, questions or long "
        "dash. Use short paragraphs. If facts are limited, stay short.\n"
        f"CATEGORY={category}\nSOURCE_LABEL={source_label}\n"
        f"VALIDATED_FACTS={facts.model_dump_json()}\n"
        f"ORIGINAL_FOR_STYLE_ONLY={json.dumps(original_text, ensure_ascii=False)}"
    )


def build_headline_prompt(
    facts: StructuredFacts,
    *,
    primary_title: str,
    source_label: str,
) -> str:
    return (
        "Return JSON only. primary_title and source_label are fixed and must be "
        "copied exactly. Write only secondary_title: one verified main meaning, "
        "maximum 55 characters, no punctuation at the end and no question.\n"
        f"primary_title={primary_title}\nsource_label={source_label}\n"
        f"VALIDATED_FACTS={facts.model_dump_json()}"
    )


class CopyValidator:
    def validate(
        self,
        text: str,
        *,
        facts: StructuredFacts,
        category: str,
    ) -> CopyValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        stripped = text.strip()
        if URL_RE.search(stripped):
            errors.append("COPY_HAS_LINK")
        if HANDLE_RE.search(stripped):
            errors.append("COPY_HAS_HANDLE")
        if CTA_RE.search(stripped):
            errors.append("COPY_HAS_CTA")
        if "—" in stripped:
            errors.append("COPY_HAS_LONG_DASH")
        if stripped.endswith("?") or TRIVIAL_QUESTION_RE.search(stripped):
            errors.append("COPY_HAS_QUESTION")
        if not CYRILLIC_RE.search(stripped):
            errors.append("COPY_NOT_RUSSIAN")
        if re.search(r"\b(\w+)\b(?:\s+\1){2,}", stripped, re.IGNORECASE):
            errors.append("COPY_REPETITION")
        if not re.search(r"[.!…]$", stripped):
            errors.append("COPY_UNFINISHED")

        source_numbers = set(NUMBER_RE.findall(facts.primary_claim))
        for evidence in facts.evidence:
            source_numbers.update(NUMBER_RE.findall(evidence.source_text))
        copy_numbers = set(NUMBER_RE.findall(stripped))
        if not copy_numbers.issubset(source_numbers):
            errors.append("COPY_INVENTED_NUMBER")

        if NEGATION_RE.search(facts.primary_claim) and not NEGATION_RE.search(stripped):
            errors.append("COPY_NEGATION_LOST")

        allowed_latin = {
            facts.subject.canonical_name.casefold(),
            facts.source.account.casefold(),
        }
        allowed_latin.update(
            club.canonical_name.casefold()
            for club in (
                facts.clubs.current,
                facts.clubs.from_club,
                facts.clubs.to_club,
            )
            if club is not None
        )
        for word in LATIN_WORD_RE.findall(stripped):
            if not any(word.casefold() in value for value in allowed_latin):
                errors.append("COPY_ENGLISH_NOISE")
                break

        minimum = 140 if category == "breaking_v2" else 220
        maximum = 450 if category == "breaking_v2" else 700
        if len(stripped) < minimum:
            warnings.append("COPY_SHORT_SOURCE_LIMITED")
        if len(stripped) > maximum:
            errors.append("COPY_TOO_LONG")
        return CopyValidationReport(
            passed=not errors,
            errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(dict.fromkeys(warnings)),
        )


class HeadlineValidator:
    def validate(
        self,
        headline: HeadlineContract,
        *,
        facts: StructuredFacts,
        source_record: SourceRecord,
    ) -> HeadlineValidationReport:
        errors: list[str] = []
        primary = headline.primary_title.strip()
        secondary = headline.secondary_title.strip()
        allowed_primary = {
            value.casefold()
            for value in (
                facts.subject.surname_display,
                facts.subject.display_name_ru,
                facts.subject.canonical_name,
                facts.clubs.to_club.canonical_name if facts.clubs.to_club else None,
            )
            if value
        }
        if primary.casefold() not in allowed_primary:
            errors.append("HEADLINE_PRIMARY_NOT_RESOLVED_ENTITY")
        if len(primary) > 24 or len(primary.split()) > 3:
            errors.append("HEADLINE_PRIMARY_TOO_LONG")
        if any(mark in primary for mark in ".?!"):
            errors.append("HEADLINE_PRIMARY_PUNCTUATION")
        if NUMBER_RE.search(primary):
            errors.append("HEADLINE_PRIMARY_HAS_AMOUNT")
        if len(secondary) > 55:
            errors.append("HEADLINE_SECONDARY_TOO_LONG")
        if secondary.endswith((".", "?", "!")):
            errors.append("HEADLINE_SECONDARY_END_MARK")
        if "?" in secondary:
            errors.append("HEADLINE_SECONDARY_QUESTION")
        if headline.source_label != source_record.canonical_name.upper():
            errors.append("HEADLINE_SOURCE_MISMATCH")
        if headline.source_label.casefold() == "premwire":
            errors.append("PREMWIRE_NOT_PRIMARY_SOURCE")
        return HeadlineValidationReport(not errors, tuple(dict.fromkeys(errors)))


def deterministic_headline(
    facts: StructuredFacts,
    decision: ClassificationDecision,
    source_record: SourceRecord,
) -> HeadlineContract:
    primary = (
        facts.subject.surname_display
        or facts.subject.display_name_ru
        or facts.subject.canonical_name
    )
    if decision.template_intent == "transfer_v2":
        secondary = "ПЕРЕХОД В КЛУБ СОГЛАСОВАН"
    elif decision.template_intent == "loan_v2":
        secondary = "ОТПРАВИТСЯ В АРЕНДУ"
    else:
        secondary = facts.primary_claim.upper()[:55].rstrip(" .?!")
    return HeadlineContract(
        primary_title=primary[:24],
        secondary_title=secondary,
        source_label=source_record.canonical_name.upper(),
    )


class EditorialService:
    def __init__(self, orchestrator: StructuredLlmOrchestrator) -> None:
        self.orchestrator = orchestrator

    def create(
        self,
        *,
        facts: StructuredFacts,
        fact_report: FactValidationReport,
        decision: ClassificationDecision,
        original_text: str,
        source_record: SourceRecord,
        story_version: int,
    ) -> tuple[RussianCopyContract, HeadlineContract, CopyValidationReport, tuple[int, ...]]:
        if not fact_report.passed or not decision.supported_category:
            raise ValueError("editorial copy requires validated supported facts")
        copy_prompt = build_copy_prompt(
            facts,
            original_text=original_text,
            source_label=source_record.canonical_name,
            category=decision.template_intent or "",
        )
        copy_result = self.orchestrator.run_json(
            task_type=LlmTaskType.EDITORIAL_COPY_RU,
            story_id=facts.story_id,
            story_version=story_version,
            prompt=copy_prompt,
            contract=RussianCopyContract,
        )
        validator = CopyValidator()
        report = validator.validate(
            copy_result.value.post_text,
            facts=facts,
            category=decision.template_intent or "",
        )
        run_ids = list(copy_result.run_ids)
        copy_value = copy_result.value
        if not report.passed:
            rewrite_prompt = (
                copy_prompt
                + "\nFix only these validation errors without changing facts: "
                + json.dumps(report.errors)
            )
            rewrite_result = self.orchestrator.run_json(
                task_type=LlmTaskType.EDITORIAL_COPY_RU,
                story_id=facts.story_id,
                story_version=story_version,
                prompt=rewrite_prompt,
                contract=RussianCopyContract,
            )
            run_ids.extend(rewrite_result.run_ids)
            copy_value = rewrite_result.value
            report = validator.validate(
                copy_value.post_text,
                facts=facts,
                category=decision.template_intent or "",
            )
            if not report.passed:
                raise ValueError("COPY_VALIDATION_FAILED")

        fallback = deterministic_headline(facts, decision, source_record)
        headline_result = self.orchestrator.run_json(
            task_type=LlmTaskType.CARD_HEADLINE_RU,
            story_id=facts.story_id,
            story_version=story_version,
            prompt=build_headline_prompt(
                facts,
                primary_title=fallback.primary_title,
                source_label=fallback.source_label,
            ),
            contract=HeadlineContract,
        )
        run_ids.extend(headline_result.run_ids)
        headline = headline_result.value
        headline_report = HeadlineValidator().validate(
            headline, facts=facts, source_record=source_record
        )
        if not headline_report.passed:
            headline = fallback
        return copy_value, headline, report, tuple(run_ids)


def maybe_translate_for_x(
    *,
    x_enabled: bool,
    orchestrator: StructuredLlmOrchestrator,
    facts: StructuredFacts,
    russian_copy: RussianCopyContract,
    story_version: int,
) -> TranslationOutputContract | None:
    if not x_enabled:
        return None
    prompt = (
        "Return JSON only. Translate validated Russian copy into natural current "
        "English without adding facts. Preserve entity spellings.\n"
        f"FACTS={facts.model_dump_json()}\nCOPY={russian_copy.model_dump_json()}"
    )
    return orchestrator.run_json(
        task_type=LlmTaskType.TRANSLATION_EN,
        story_id=facts.story_id,
        story_version=story_version,
        prompt=prompt,
        contract=TranslationOutputContract,
    ).value
