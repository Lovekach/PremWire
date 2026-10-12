from __future__ import annotations

import re
from enum import StrEnum

from .russian_editorial import club_entries
from .simple_factual_guard import normalized_numeric_claims


class SourceRisk(StrEnum):
    TRANSFER_STATUS = "TRANSFER_STATUS"
    MULTI_ENTITY = "MULTI_ENTITY"
    MULTI_CLUB = "MULTI_CLUB"
    DENSE_NUMERIC = "DENSE_NUMERIC"
    SCORE_RESULT = "SCORE_RESULT"
    ATTRIBUTION = "ATTRIBUTION"
    BID_OFFER = "BID_OFFER"
    UNKNOWN_PROPER_NAMES = "UNKNOWN_PROPER_NAMES"


_TRANSFER_RE = re.compile(
    r"\b(?:transfer|sign(?:ed|ing|s)?|join(?:ed|ing|s)?|loan|deal|agreement|"
    r"talks?|negotiat\w*|interest\w*|bid|offer|трансфер|переход|подпис\w*|"
    r"аренд\w*|сделк\w*|соглашени\w*|переговор\w*|интерес\w*|предложени\w*)\b",
    re.I,
)
_SCORE_RE = re.compile(
    r"(?:\b\d{1,3}\s*[:\-–—]\s*\d{1,3}\b|"
    r"\b(?:beat|defeat\w*|lost|loss|won|win|draw|побед\w*|обыграл\w*|"
    r"проиграл\w*|поражени\w*|ничь\w*|сч[её]т)\b)",
    re.I,
)
_ATTRIBUTION_RE = re.compile(
    r"\b(?:according\s+to|sources?|reported|reports?|told|said|says|confirm(?:ed|s)?\s+by|"
    r"по\s+данным|источник\w*|сообща\w*|заявил\w*|сказал\w*|подтвердил\w*)\b",
    re.I,
)
_BID_RE = re.compile(
    r"\b(?:bid|offer(?:ed|s)?|proposal|request(?:ed|s)?|предложени\w*|заявк\w*|запросил\w*)\b",
    re.I,
)
_STAT_RE = re.compile(r"(?<!\d)\d{1,3}\s*\+\s*\d{1,3}(?!\d)")
_QUOTED_ENTITY_RE = re.compile(r"[«\"]([^»\"\n]{2,80})[»\"]")
_LATIN_NAME_RE = re.compile(
    r"(?<![\w])"
    r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{1,}"
    r"(?:\s+(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{1,}|de|van|von|da|dos)){0,3}"
)
_CYRILLIC_NAME_RE = re.compile(
    r"(?<![\w])(?:[А-ЯЁ][а-яё]{2,})(?:\s+[А-ЯЁ][а-яё]{2,}){1,2}"
)
_GENERIC_LATIN = frozenset(
    {
        "BREAKING",
        "EXCL",
        "OFFICIAL",
        "SOURCE",
        "Source",
        "The",
        "Story",
        "Deal",
        "Fee",
        "Personal",
        "Negotiations",
        "Documents",
        "World Cup",
        "Premier League",
        "Champions League",
        "Europa League",
        "Sky Sports",
    }
)


def _known_club_ids(text: str) -> frozenset[str]:
    folded = text.casefold()
    found: set[str] = set()
    for club in club_entries():
        if any(
            re.search(rf"(?<![\w]){re.escape(alias.casefold())}(?![\w])", folded)
            for alias in club.aliases
        ):
            found.add(club.entity_id)
    return frozenset(found)


def proper_name_candidates(text: str) -> frozenset[str]:
    candidates = {
        match.group(0).strip(" .,:;()[]{}")
        for match in _LATIN_NAME_RE.finditer(text)
    }
    candidates.update(match.group(0) for match in _CYRILLIC_NAME_RE.finditer(text))
    candidates.update(match.group(1).strip() for match in _QUOTED_ENTITY_RE.finditer(text))
    return frozenset(
        value for value in candidates if value and value not in _GENERIC_LATIN
    )


def analyze_simple_source_risks(text: str) -> frozenset[SourceRisk]:
    risks: set[SourceRisk] = set()
    names = proper_name_candidates(text)
    clubs = _known_club_ids(text)
    numeric = normalized_numeric_claims(text)

    if _TRANSFER_RE.search(text):
        risks.add(SourceRisk.TRANSFER_STATUS)
    if len(names.union(clubs)) >= 3:
        risks.add(SourceRisk.MULTI_ENTITY)
    if len(clubs) >= 2 or len(_QUOTED_ENTITY_RE.findall(text)) >= 2:
        risks.add(SourceRisk.MULTI_CLUB)
    if len(numeric) >= 4 or len(_STAT_RE.findall(text)) >= 2:
        risks.add(SourceRisk.DENSE_NUMERIC)
    if _SCORE_RE.search(text):
        risks.add(SourceRisk.SCORE_RESULT)
    if _ATTRIBUTION_RE.search(text):
        risks.add(SourceRisk.ATTRIBUTION)
    if _BID_RE.search(text):
        risks.add(SourceRisk.BID_OFFER)

    known_aliases = {
        alias.casefold()
        for club in club_entries()
        for alias in club.aliases
    }
    latin_names = {
        match.group(0).strip(" .,:;()[]{}").casefold()
        for match in _LATIN_NAME_RE.finditer(text)
        if match.group(0).strip(" .,:;()[]{}") not in _GENERIC_LATIN
    }
    if any(value not in known_aliases for value in latin_names):
        risks.add(SourceRisk.UNKNOWN_PROPER_NAMES)
    return frozenset(risks)


_RELATION_RISKS = frozenset(
    {
        SourceRisk.MULTI_ENTITY,
        SourceRisk.MULTI_CLUB,
        SourceRisk.SCORE_RESULT,
    }
)
def dynamic_risk_instructions(risks: frozenset[SourceRisk]) -> str:
    rules: list[str] = []
    if risks.intersection(_RELATION_RISKS):
        rules.append(
            "RISK RELATIONS: сохраняй отдельно кто что сделал, кому и за какую "
            "команду; не объединяй факты, не угадывай клуб/лигу/победителя. "
            "Лучше два простых предложения или пропуск детали."
        )
    if SourceRisk.DENSE_NUMERIC in risks or SourceRisk.SCORE_RESULT in risks:
        rules.append(
            "RISK NUMBERS: не вычисляй числа; счёт сохраняй вместе с победителем; "
            "статистику 2+2 оставь как 2+2 или опусти."
        )
    if SourceRisk.TRANSFER_STATUS in risks:
        rules.append(
            "RISK STATUS: interest, talks, offer, agreement и official — разные "
            "стадии; не усиливай статус."
        )
    if SourceRisk.ATTRIBUTION in risks:
        rules.append(
            "RISK ATTRIBUTION: не меняй автора информации и не превращай источники "
            "в официальное подтверждение клуба."
        )
    if SourceRisk.BID_OFFER in risks:
        rules.append(
            "RISK OFFER: точно сохраняй направление отправитель → получатель → "
            "игрок/объект предложения или запроса."
        )
    if SourceRisk.UNKNOWN_PROPER_NAMES in risks:
        rules.append(
            "RISK NAMES: незнакомое имя не исправляй по памяти и не заменяй другим; "
            "передай точное имя SOURCE русской транслитерацией, без латиницы."
        )
    return "\n".join(rules)


__all__ = [
    "SourceRisk",
    "analyze_simple_source_risks",
    "dynamic_risk_instructions",
    "proper_name_candidates",
]
