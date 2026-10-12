from __future__ import annotations

import re
from dataclasses import dataclass


SOURCE_LANGUAGE_AMBIGUOUS = "SOURCE_LANGUAGE_AMBIGUOUS"
IMPOSSIBLE_FOOTBALL_TERM = "IMPOSSIBLE_FOOTBALL_TERM"


@dataclass(frozen=True, slots=True)
class SourceLanguageNormalization:
    raw_text: str
    clean_text: str
    corrections: tuple[str, ...]
    issue: str | None = None


# These are terminology substitutions, not semantic rewrites.  Every rule is
# deliberately narrow and leaves names, numbers, status and direction intact.
_SAFE_TERMINOLOGY_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "RIGHT_BACK_SOURCE_TYPO",
        re.compile(r"\bправового\s+защитника\b", re.I),
        "правого защитника",
    ),
    (
        "RIGHT_BACK_SOURCE_TYPO",
        re.compile(r"\bправовым\s+защитником\b", re.I),
        "правым защитником",
    ),
    (
        "RIGHT_BACK_SOURCE_TYPO",
        re.compile(r"\bправовой\s+защитник\b", re.I),
        "правый защитник",
    ),
    (
        "FULL_BACK_LITERAL",
        re.compile(r"\bполного\s+защитника\b", re.I),
        "крайнего защитника",
    ),
    (
        "FULL_BACK_LITERAL",
        re.compile(r"\bполным\s+защитником\b", re.I),
        "крайним защитником",
    ),
    (
        "FULL_BACK_LITERAL",
        re.compile(r"\bполный\s+защитник\b", re.I),
        "крайний защитник",
    ),
    ("RIGHT_BACK", re.compile(r"(?<![\w-])right[- ]back(?![\w-])", re.I), "правый защитник"),
    ("LEFT_BACK", re.compile(r"(?<![\w-])left[- ]back(?![\w-])", re.I), "левый защитник"),
    ("FULL_BACK", re.compile(r"(?<![\w-])full[- ]back(?![\w-])", re.I), "крайний защитник"),
    (
        "CENTRE_BACK",
        re.compile(r"(?<![\w-])(?:centre|center)[- ]back(?![\w-])", re.I),
        "центральный защитник",
    ),
    (
        "DEFENSIVE_MIDFIELDER",
        re.compile(r"(?<![\w-])defensive\s+midfielder(?![\w-])", re.I),
        "опорный полузащитник",
    ),
    (
        "ATTACKING_MIDFIELDER",
        re.compile(r"(?<![\w-])attacking\s+midfielder(?![\w-])", re.I),
        "атакующий полузащитник",
    ),
    ("STRIKER", re.compile(r"(?<![\w-])striker(?![\w-])", re.I), "нападающий"),
    ("GOALKEEPER", re.compile(r"(?<![\w-])goalkeeper(?![\w-])", re.I), "вратарь"),
    ("WINGER", re.compile(r"(?<![\w-])winger(?![\w-])", re.I), "вингер"),
    ("ACADEMY", re.compile(r"(?<![\w-])the\s+academy(?![\w-])", re.I), "клубная академия"),
)

_AMBIGUOUS_TERMINOLOGY = re.compile(
    r"\b(?:прав\w*\s*/\s*прав\w*|право(?:\.{2,}|\?+))\s+защитник\w*\b",
    re.I,
)

_IMPOSSIBLE_TERMINOLOGY = re.compile(
    r"\b(?:правов(?:ой|ого|ому|ым|ом|ые|ых)|полн(?:ый|ого|ому|ым|ом|ые|ых))\s+защитник\w*\b",
    re.I,
)


def normalize_source_language(text: str) -> SourceLanguageNormalization:
    """Apply only source-grounded, high-confidence terminology corrections."""

    raw = text
    value = raw.replace("\r\n", "\n").replace("\r", "\n")
    if _AMBIGUOUS_TERMINOLOGY.search(value):
        return SourceLanguageNormalization(raw, value, (), SOURCE_LANGUAGE_AMBIGUOUS)
    corrections: list[str] = []
    for code, pattern, replacement in _SAFE_TERMINOLOGY_RULES:
        value, count = pattern.subn(replacement, value)
        if count:
            corrections.extend([code] * count)
    return SourceLanguageNormalization(raw, value, tuple(corrections))


def normalize_final_football_terminology(text: str) -> str:
    """Use the same deterministic glossary for a generated editorial draft."""

    return normalize_source_language(text).clean_text


def football_terminology_issues(text: str) -> tuple[str, ...]:
    issues: list[str] = []
    if _AMBIGUOUS_TERMINOLOGY.search(text):
        issues.append(SOURCE_LANGUAGE_AMBIGUOUS)
    if _IMPOSSIBLE_TERMINOLOGY.search(text):
        issues.append(IMPOSSIBLE_FOOTBALL_TERM)
    return tuple(issues)
