from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .source_language import (
    football_terminology_issues,
    normalize_final_football_terminology,
)


EDITORIAL_PASS = "EDITORIAL_PASS"
_SUPPRESSED_PUBLIC_SOURCE_NAMES = frozenset(
    {
        "albion",
        "albionmain",
        "telegram_albion_main",
        "английский акцент",
    }
)


@dataclass(frozen=True, slots=True)
class EditorialAssessment:
    decision: str
    issues: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.decision == EDITORIAL_PASS


@dataclass(frozen=True, slots=True)
class ClubEntry:
    entity_id: str
    display: str
    aliases: tuple[str, ...]
    forms: dict[str, str]
    grammatical_gender: str | None = None


@lru_cache(maxsize=1)
def _policy() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "config" / "russian_editorial_policy_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _source_registry() -> dict[str, dict[str, Any]]:
    path = Path(__file__).resolve().parents[2] / "config" / "source_registry.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item.get("canonical_name", "")).casefold(): item
        for item in payload.get("sources", ())
        if isinstance(item, dict) and item.get("canonical_name")
    }


def canonical_source_display(source_name: str) -> str | None:
    """Return a trusted source label only when current policy allows display."""
    if source_is_suppressed_publicly(source_name):
        return None
    item = _source_registry().get(source_name.strip().casefold())
    if item and item.get("enabled") and item.get("source_role") == "journalist":
        return str(item["canonical_name"])
    approved_outlets = {
        "the athletic": "The Athletic",
        "football.london": "Football.London",
    }
    return approved_outlets.get(source_name.strip().casefold())


def source_is_suppressed_publicly(source_name: str) -> bool:
    """Keep internal aggregator identity out of the public editorial caption."""

    normalized = re.sub(r"[^a-zа-яё0-9_]+", " ", source_name.casefold()).strip()
    return normalized in _SUPPRESSED_PUBLIC_SOURCE_NAMES


@lru_cache(maxsize=1)
def club_entries() -> tuple[ClubEntry, ...]:
    entries = []
    for raw in _policy()["canonical_clubs"]:
        aliases = tuple(dict.fromkeys((raw["display"], *raw.get("aliases", ()))))
        entries.append(
            ClubEntry(
                entity_id=str(raw["id"]),
                display=str(raw["display"]),
                aliases=aliases,
                forms={str(k): str(v) for k, v in raw.get("forms", {}).items()},
                grammatical_gender=(
                    str(raw["grammatical_gender"])
                    if raw.get("grammatical_gender") in {"masculine", "feminine"}
                    else None
                ),
            )
        )
    return tuple(entries)


def _fold(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).strip()


def _replace_alias(text: str, alias: str, replacement: str) -> str:
    return re.sub(
        rf"(?<![\w]){re.escape(alias)}(?![\w])",
        lambda _: replacement,
        text,
        flags=re.IGNORECASE,
    )


def canonicalize_clubs(text: str) -> str:
    """Render only curated club aliases; the visual emblem registry is not editorial truth."""
    value = text
    replacements: list[tuple[str, str]] = []
    for club in club_entries():
        replacements.extend((alias, club.display) for alias in club.aliases)
    for alias, display in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        value = _replace_alias(value, alias, display)
    # A short alias can be contained in its own canonical display (Villa in
    # Aston Villa). Collapse this mechanically before applying grammatical
    # forms so repeated normalization remains idempotent.
    value = re.sub(r"\b(?:Астон\s+){2,}Вилла\b", "Астон Вилла", value, flags=re.IGNORECASE)

    # Curated cases are applied only in unambiguous grammatical contexts.
    for club in club_entries():
        forms = club.forms
        display = club.display
        for preposition, case in (("из", "genitive"), ("у", "genitive"), ("к", "dative"), ("с", "instrumental")):
            form = forms.get(case, display)
            value = re.sub(
                rf"\b{preposition}\s+{re.escape(display)}\b",
                f"{preposition} {form}",
                value,
                flags=re.IGNORECASE,
            )
        # A medical is held at a club; this is location, not transfer direction.
        prepositional = forms.get("prepositional", display)
        value = re.sub(
            rf"(медицинск\w*\s+обследован\w*|медосмотр\w*)\s+в\s+{re.escape(display)}\b",
            lambda match: f"{match.group(1)} в {prepositional}",
            value,
            flags=re.IGNORECASE,
        )
        for form in set(forms.values()):
            value = re.sub(
                rf"[«\"]{re.escape(form)}[»\"]",
                form,
                value,
                flags=re.IGNORECASE,
            )
    return value


_RU_ONES = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
}
_RU_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}
_EN_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_EN_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _parse_small_number(words: str) -> int | None:
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё]+", words.casefold().replace("ё", "е"))
    if not tokens or len(tokens) > 3:
        return None
    total = 0
    for token in tokens:
        if token in _RU_ONES:
            total += _RU_ONES[token]
        elif token in _RU_TENS:
            total += _RU_TENS[token]
        elif token in _EN_ONES:
            total += _EN_ONES[token]
        elif token in _EN_TENS:
            total += _EN_TENS[token]
        elif token == "hundred":
            total = max(total, 1) * 100
        elif token in {"and", "и"}:
            continue
        else:
            return None
    return total


_NUMBER_WORDS = (
    r"(?:ноль|один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|"
    r"десять|одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|"
    r"шестнадцать|семнадцать|восемнадцать|девятнадцать|двадцать|тридцать|"
    r"сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|"
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|and)"
)
_NUMBER_PHRASE = rf"{_NUMBER_WORDS}(?:[ -]+{_NUMBER_WORDS}){{0,2}}"


def normalize_football_numbers(text: str) -> str:
    value = text

    def replace(match: re.Match[str]) -> str:
        number = _parse_small_number(match.group("words"))
        if number is None:
            return match.group(0)
        unit = match.group("unit").casefold()
        if unit.startswith(("миллион", "million")):
            return f"{number} млн"
        if unit.startswith(("тысяч", "thousand")):
            return f"{number} тыс."
        suffix = match.group("unit")
        return f"{number} {suffix}"

    unit_pattern = (
        r"(?P<unit>миллион(?:а|ов)?|тысяч(?:а|и)?|год(?:а)?|лет|матч(?:а|ей)?|"
        r"гол(?:а|ов)?|million(?:s)?|thousand|year(?:s)?|match(?:es)?|goal(?:s)?)"
    )
    value = re.sub(
        rf"(?P<words>{_NUMBER_PHRASE})(?:\s+|-){unit_pattern}",
        replace,
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b(\d+(?:[.,]\d+)?)\s+миллион(?:а|ов)?\b", r"\1 млн", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(\d+(?:[.,]\d+)?)\s+тысяч(?:а|и)?\b", r"\1 тыс.", value, flags=re.IGNORECASE)
    value = re.sub(r"\bфунтов\s+стерлингов\b", "фунтов", value, flags=re.IGNORECASE)
    return value


def numeric_fact_tokens(text: str) -> frozenset[str]:
    normalized = normalize_football_numbers(text)
    return frozenset(
        value.replace(",", ".")
        for value in re.findall(r"\d+(?:[.,]\d+)?", normalized)
    )


_ENGAGEMENT_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+|\n+)(?:А\s+)?(?:Что\s+думаете|Как\s+вам|"
    r"Кого\s+бы\s+вы|Кто\s+из|Согласны\s+ли\s+вы|Делитесь\s+мнением)[^.!?]*(?:[!?]+|$)",
    re.IGNORECASE,
)
_PROMOTION_RE = re.compile(
    r"\b(?:watch\s+now|watch\s+highlights|join\s+the\s+stream|read\s+more|"
    r"cast\s+your\s+vote|vote\s+now|presented\s+by|"
    r"смотрите\s+(?:здесь|сейчас)|посмотреть\s+(?:их\s+)?здесь|"
    r"присоединяйтесь\s+к\s+трансляции|мы\s+запускаем\s+.*трансляц|"
    r"промокод|букмекер|казино|affiliate)\b",
    re.IGNORECASE,
)
_LANGUAGE_SANITY = (
    re.compile(r"\bэтой\s+недели\b", re.IGNORECASE),
    re.compile(r"\bна\s+прошл(?:ой|ую)\s+ночь\b", re.IGNORECASE),
    re.compile(r"\bкотор(?:ый|ая|ое)\s+прошл(?:ый|ую|ое)\s+сезон", re.IGNORECASE),
    re.compile(r"\bполным?\s+защитник\w*\b", re.IGNORECASE),
    re.compile(r"\bподпис\w*\s+контракт\w*\s+со\s+своим\s+агент", re.IGNORECASE),
    re.compile(r"\b(?:Интер|Челси|Лацио|Милан|Тоттенхэм)\s+(?:ведут|готовы|хотят|рассматривают)\b", re.IGNORECASE),
)
_INVENTED_FILLER_RE = re.compile(
    r"\b(?:агентство\s+не\s+уточняет|ключевой\s+вопрос\s+заключается|"
    r"остаются\s+оптимистичными\s+в\s+своей\s+стратегии|"
    r"готов\w*\s+к\s+игре\s+на|начальн\w+\s+стади\w+\s+открытия\s+аукциона)\b",
    re.IGNORECASE,
)
_ALL_CAPS_ENTITY_RE = re.compile(r"\b[А-ЯЁ]{3,}(?:[- ][А-ЯЁ]{3,})+\b")
_SOURCE_LINE_RE = re.compile(
    r"(?:^|\n|(?<=[.!?])\s+)\s*(?:Источник|Source)\s*:\s*([^\n]+)",
    re.IGNORECASE,
)
_CYRILLIC_TO_LATIN = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e",
    "ж":"zh","з":"z","и":"i","й":"i","к":"k","л":"l","м":"m",
    "н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u",
    "ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sh","ы":"y",
    "э":"e","ю":"yu","я":"ya","ь":"","ъ":"",
})


def _strip_engagement(text: str) -> str:
    value = _ENGAGEMENT_RE.sub(" ", text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r" {2,}", " ", value)).strip()


def strip_terminal_body_period(text: str) -> str:
    """Drop only the final full stop from the public editorial body.

    PremWire uses a compact social-news style: full stops still separate
    sentences, while the final sentence does not need a trailing ``.``.
    Question marks, exclamation marks and ellipses are preserved.  A
    deterministic source block is kept byte-for-byte.
    """

    value = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    source_match = next(
        (match for match in _SOURCE_LINE_RE.finditer(value)),
        None,
    )
    if source_match is None:
        body = value
        suffix = ""
    else:
        body = value[: source_match.start()].rstrip()
        suffix = value[source_match.start():].lstrip()
    # A single sentence-ending dot is editorial punctuation; three dots are
    # meaningful and remain untouched.  Closing Russian/straight quotes may
    # follow the sentence-ending dot.
    body = re.sub(r"(?<!\.)\.(?=(?:[»”\"])?\s*$)", "", body)
    if not suffix:
        return body
    return f"{body}\n\n{suffix}" if body else suffix


def _canonical_source_block(text: str, source_name: str) -> str:
    matches = tuple(_SOURCE_LINE_RE.finditer(text))
    if not matches:
        return text.strip()
    value = _SOURCE_LINE_RE.sub("", text).strip()
    if not source_name.strip():
        return value
    return f"{value}\n\nИсточник: {source_name.strip()}".strip()


def normalize_editorial_text(text: str, source_name: str = "") -> str:
    value = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Model-generated trailing attribution can repeat inline without punctuation.
    # The single trusted attribution is appended below from configuration.
    value = re.split(r"\b(?:Источник|Source)\s*:", value, maxsplit=1, flags=re.I)[0].rstrip()
    value = _SOURCE_LINE_RE.sub("", value).strip()
    value = _strip_engagement(value)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", value) if part.strip()]
    non_promotional = [part for part in sentences if not _PROMOTION_RE.search(part)]
    if non_promotional and len(non_promotional) != len(sentences):
        value = " ".join(non_promotional)
    value = canonicalize_clubs(value)
    for source, target in _policy()["football_terms"].items():
        value = _replace_alias(value, source, target)
    value = normalize_final_football_terminology(value)
    value = normalize_football_numbers(value)
    value = re.sub(r"\s[–—-]\s", " — ", value)
    value = _ALL_CAPS_ENTITY_RE.sub(
        lambda match: " ".join(part.capitalize() for part in match.group(0).split()),
        value,
    )
    value = re.sub(r"\bэтой недели\b", "на этой неделе", value, flags=re.IGNORECASE)
    value = re.sub(r"\bна прошл(?:ой|ую) ночь\b", "прошлой ночью", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\b(котор(?:ый|ая|ое)) прошл(?:ый|ую|ое) сезон\b",
        r"\1 в прошлом сезоне",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"[«\"]([^»\"\n]+)[»\"]", lambda m: m.group(1) if any(_fold(m.group(1)) == _fold(a) for c in club_entries() for a in c.aliases) else m.group(0), value)
    source_display = canonical_source_display(source_name)
    if source_display:
        value = f"{value.strip()}\n\nИсточник: {source_display}"
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


_CLUB_VERB_FORMS: dict[str, tuple[str, str]] = {
    "согласовали": ("согласовал", "согласовала"),
    "согласились": ("согласился", "согласилась"),
    "получили": ("получил", "получила"),
    "подписали": ("подписал", "подписала"),
    "продали": ("продал", "продала"),
    "купили": ("купил", "купила"),
}


def normalize_club_verb_agreement(text: str) -> str:
    """Correct only high-confidence club-subject plural calques.

    English collective club names often reach the Russian draft with a plural
    verb.  Curated grammatical metadata is authoritative; an unknown club is
    made explicit as ``Клуб X`` instead of guessing its gender.
    """

    value = text
    for club in club_entries():
        for plural, (masculine, feminine) in _CLUB_VERB_FORMS.items():
            replacement = (
                masculine
                if club.grammatical_gender in {None, "masculine"}
                else feminine
            )
            subject = club.display if club.grammatical_gender else f"Клуб {club.display}"
            value = re.sub(
                rf"(?<![\w]){re.escape(club.display)}\s+{plural}\b",
                f"{subject} {replacement}",
                value,
                flags=re.IGNORECASE,
            )
    return value


def club_ids_in_text(text: str) -> frozenset[str]:
    ids: set[str] = set()
    for club in club_entries():
        surfaces = (*club.aliases, *club.forms.values())
        if any(re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", text, re.IGNORECASE) for alias in surfaces):
            ids.add(club.entity_id)
    return frozenset(ids)


def editorial_issues(
    source_text: str,
    final_text: str,
    *,
    source_name: str = "",
) -> tuple[str, ...]:
    issues: list[str] = []
    folded = final_text.casefold()
    if _PROMOTION_RE.search(final_text):
        issues.append("PROMOTIONAL_CONTENT")
    if _ENGAGEMENT_RE.search(final_text):
        issues.append("ENGAGEMENT_BAIT")
    if any(pattern.search(final_text) for pattern in _LANGUAGE_SANITY):
        issues.append("LANGUAGE_SANITY")
    if football_terminology_issues(final_text):
        issues.append("TERMINOLOGY_INVALID")
    for club in club_entries():
        if not club.grammatical_gender:
            continue
        if any(
            re.search(rf"(?<![\w]){re.escape(club.display)}\s+{plural}\b", final_text, re.I)
            for plural in _CLUB_VERB_FORMS
        ):
            issues.append("LANGUAGE_SUBJECT_VERB_AGREEMENT")
            break
    if re.search(r"\bсделк\w*\s+с\s+(?:клубн\w+\s+)?академи\w*\b", final_text, re.I):
        issues.append("LANGUAGE_ACADEMY_COUNTERPARTY")
    if re.search(r"\bсделк\w*[^.!?]{0,50}\bдля\s+подписания\b", final_text, re.I):
        issues.append("LANGUAGE_LITERAL_SIGNING_CONSTRUCTION")
    if _ALL_CAPS_ENTITY_RE.search(final_text):
        issues.append("ALL_CAPS_ENTITY")
    if _INVENTED_FILLER_RE.search(final_text):
        issues.append("INVENTED_FILLER")
    if (
        re.search(r"\b(?:переш[её]л|подписал|стал\s+игроком)\b", final_text, re.IGNORECASE)
        and re.search(r"\b(?:финальн\w+\s+стади\w+|ещ[её]\s+не\s+завершен\w*|переговоры\s+продолжаются)\b", final_text, re.IGNORECASE)
    ):
        issues.append("FACT_RELATION")

    for club in club_entries():
        for alias in club.aliases:
            if re.search(r"[A-Za-z]", alias) and re.search(
                rf"(?<![\w]){re.escape(alias)}(?![\w])", final_text, re.IGNORECASE
            ):
                issues.append("ENGLISH_CLUB_LEAK")
                break
    if re.search(r"[«\"](?:[^»\"\n]{1,80})[»\"]", final_text):
        quoted = re.findall(r"[«\"]([^»\"\n]{1,80})[»\"]", final_text)
        if any(any(_fold(value) == _fold(alias) for club in club_entries() for alias in club.aliases) for value in quoted):
            issues.append("CLUB_QUOTE_STYLE")

    if re.search(rf"\b{_NUMBER_PHRASE}\s+(?:год|года|лет|миллион|миллиона|миллионов|тысяч|матч|матча|матчей|гол|гола|голов)\b", final_text, re.IGNORECASE):
        issues.append("NUMBER_WORD_STYLE")
    corrupted = _policy().get("corrupted_tokens", ())
    if any(token.casefold() in folded for token in corrupted):
        issues.append("ENTITY_CORRUPTION")

    source_matches = tuple(_SOURCE_LINE_RE.finditer(final_text))
    if source_matches:
        if len(source_matches) != 1 or source_matches[0].group(1).strip() != source_name.strip():
            issues.append("SOURCE_NAME_VALIDITY")
        if final_text[source_matches[-1].end():].strip():
            issues.append("SOURCE_FORMAT")

    source_clubs = club_ids_in_text(source_text)
    final_clubs = club_ids_in_text(final_text)
    if not final_clubs.issubset(source_clubs):
        issues.append("INVENTED_ENTITY")

    # A new mid-sentence capitalised token is suspicious. Sentence openers are
    # excluded; curated club renderings are handled by the identity check above.
    source_words = re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", source_text.casefold())
    source_latin = [
        word.translate(_CYRILLIC_TO_LATIN) if re.search(r"[а-яё]", word) else word
        for word in source_words
    ]
    allowed_surfaces = {
        token.casefold()
        for club in club_entries()
        if club.entity_id in source_clubs
        for surface in (club.display, *club.forms.values())
        for token in surface.split()
    }
    for match in re.finditer(r"\b[А-ЯЁ][а-яё]{3,}\b", _SOURCE_LINE_RE.sub("", final_text)):
        before = final_text[:match.start()].rstrip()
        if not before or before[-1] in ".!?\n":
            continue
        token = match.group(0).casefold()
        if token in allowed_surfaces:
            continue
        token_latin = token.translate(_CYRILLIC_TO_LATIN)
        if not any(
            token[:5] == source_word[:5]
            or SequenceMatcher(None, token_latin, source_word).ratio() >= 0.52
            for source_word in source_latin
        ):
            issues.append("INVENTED_ENTITY")
            break

    source_numbers = numeric_fact_tokens(source_text)
    final_numbers = numeric_fact_tokens(final_text)
    if not final_numbers.issubset(source_numbers):
        issues.append("FACT_RELATION")

    source_folded = source_text.casefold()
    # Direction-specific fail closed for the common buyer/bid/from construction.
    if re.search(r"\b(?:bid|offer|move|sign|want|interested)\b", source_folded) and " from " in source_folded:
        ordered_hits: list[tuple[int, str]] = []
        for club in club_entries():
            positions = [source_folded.find(alias.casefold()) for alias in club.aliases]
            positions = [position for position in positions if position >= 0]
            if positions:
                ordered_hits.append((min(positions), club.entity_id))
        ordered = [entity_id for _, entity_id in sorted(ordered_hits)]
        if len(ordered) >= 2:
            seller = next((club for club in club_entries() if club.entity_id == ordered[-1]), None)
            if seller and re.search(
                rf"\b(?:переход|перейд\w*|перехода)\b[^.!?]{{0,50}}\bв\s+{re.escape(seller.display)}\b",
                final_text,
                re.IGNORECASE,
            ):
                issues.append("FACT_RELATION")

    return tuple(dict.fromkeys(issues))


def assess_editorial(source_text: str, final_text: str, *, source_name: str = "") -> EditorialAssessment:
    issues = editorial_issues(source_text, final_text, source_name=source_name)
    if not issues:
        return EditorialAssessment(EDITORIAL_PASS, ())
    priority = (
        ("PROMOTIONAL_CONTENT", "EDITORIAL_FAIL_PROMOTIONAL"),
        ("FACT_RELATION", "EDITORIAL_FAIL_FACT_RELATION"),
        ("INVENTED_ENTITY", "EDITORIAL_FAIL_ENTITY"),
        ("ENTITY_CORRUPTION", "EDITORIAL_FAIL_ENTITY"),
        ("SOURCE_NAME_VALIDITY", "EDITORIAL_FAIL_SOURCE"),
        ("TERMINOLOGY_INVALID", "EDITORIAL_FAIL_TERMINOLOGY"),
        ("LANGUAGE_SANITY", "EDITORIAL_FAIL_GRAMMAR"),
    )
    decision = next((code for issue, code in priority if issue in issues), "EDITORIAL_FAIL_LANGUAGE")
    return EditorialAssessment(decision, issues)


def source_is_promotional_only(text: str) -> bool:
    if not _PROMOTION_RE.search(text):
        return False
    cleaned = _PROMOTION_RE.sub(" ", text)
    cleaned = re.sub(r"(?:https?://|www\.)\S+", " ", cleaned, flags=re.IGNORECASE)
    news_markers = re.compile(
        r"\b(?:sign|signed|join|joined|loaned|bid|offer|injur|score|win|lose|"
        r"переш|переход|трансфер|аренд|подпис|предлож|травм|забил|выигр)\w*\b",
        re.IGNORECASE,
    )
    return not bool(news_markers.search(cleaned))
