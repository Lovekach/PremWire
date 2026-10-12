from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True, slots=True)
class EntityHygieneViolation:
    code: str
    detail: str


_TOKEN_RE = re.compile(r"(?<![\w])[\w'’\-]+(?![\w])", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_LATIN_WORD = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{1,}"
_LATIN_NAME = rf"{_LATIN_WORD}(?:\s+(?:{_LATIN_WORD}|de|van|von|da|dos)){{1,3}}"
_CYRILLIC_WORD = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_CYRILLIC_NAME = rf"{_CYRILLIC_WORD}(?:\s+{_CYRILLIC_WORD}){{1,3}}"
_SOURCE_PERSON_PATTERNS = (
    re.compile(
        rf"(?i:\b(?:goalkeeper|keeper|centre-back|center-back|full-back|defender|"
        rf"midfielder|winger|striker|forward|player|manager|coach|вратарь|"
        rf"голкипер|защитник|полузащитник|вингер|нападающий|форвард|игрок|тренер))"
        rf"\s+({_LATIN_NAME})",
    ),
    re.compile(rf"(?i:\baccording\s+to)\s+({_LATIN_NAME})"),
    re.compile(rf"\(({_LATIN_NAME})\)"),
    re.compile(
        rf"\b({_LATIN_NAME})\s+(?i:reports?|reported|says?|said|confirms?|confirmed)\b",
    ),
)
_OUTPUT_PERSON_ROLE_RE = re.compile(
    rf"(?i:\b(?:вратар\w*|голкипер\w*|защитник\w*|полузащитник\w*|"
    rf"вингер\w*|нападающ\w*|форвард\w*|игрок\w*|тренер\w*))\s+({_CYRILLIC_NAME})",
)
_OUTPUT_CYRILLIC_NAME_RE = re.compile(rf"(?<![\w])({_CYRILLIC_NAME})(?![\w])")
_TITLE_LATIN_SPAN_RE = re.compile(rf"(?<![\w]){_LATIN_NAME}(?![\w])")
_LOWER_ENGLISH_SEQUENCE_RE = re.compile(
    r"(?<![A-Za-z])[a-z]{3,}(?:[ -]+[a-z]{3,})+(?![A-Za-z])"
)
_UNTRANSLATED_EDITORIAL_PHRASES = (
    "undisclosed fee",
    "free transfer",
    "loan deal",
    "permanent deal",
    "medical booked",
    "deal agreed",
)
# Small irregular source-local spellings, not a publication entity registry.
# They only make the Cyrillic safety check deterministic where English
# pronunciation cannot be recovered reliably from letters alone.
_SOURCE_LOCAL_CYRILLIC_NAMES = {
    "ifeanyi ndukwe": "Ифеаньи Ндукве",
    "jamie bynoe-gittens": "Джейми Байно-Гиттенс",
    "jeyland mitchell": "Джейланд Митчелл",
    "joao pedro": "Жоао Педро",
}
_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
        "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
        "ш": "sh", "щ": "sh", "ы": "y", "э": "e", "ю": "yu",
        "я": "ya", "ь": "", "ъ": "",
    }
)


def mixed_script_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            match.group(0)
            for match in _TOKEN_RE.finditer(text)
            if _CYRILLIC_RE.search(match.group(0)) and _LATIN_RE.search(match.group(0))
        )
    )


def check_simple_entity_hygiene(text: str) -> tuple[EntityHygieneViolation, ...]:
    tokens = mixed_script_tokens(text)
    if not tokens:
        return ()
    return (
        EntityHygieneViolation(
            "MIXED_SCRIPT_ENTITY",
            "OUTPUT содержит смешанные кириллическо-латинские токены: "
            + ", ".join(tokens),
        ),
    )


def source_local_person_names(text: str) -> tuple[str, ...]:
    """Extract only person spans supported by narrow English-source context."""

    names: list[str] = []
    for pattern in _SOURCE_PERSON_PATTERNS:
        names.extend(match.group(1).strip(" .,:;()") for match in pattern.finditer(text))
    return tuple(dict.fromkeys(name for name in names if name))


def _latin_fold(value: str) -> str:
    expanded = value.translate(
        str.maketrans(
            {"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ß": "ss"}
        )
    )
    decomposed = unicodedata.normalize("NFKD", expanded)
    return "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    ).casefold()


def _transliteration_parts(value: str) -> tuple[str, ...]:
    has_cyrillic = bool(_CYRILLIC_RE.search(value))
    folded = _latin_fold(value)
    if has_cyrillic:
        folded = folded.translate(_CYRILLIC_TO_LATIN).replace("tts", "ts")
    parts = re.findall(r"[a-z]{2,}", folded)
    if has_cyrillic:
        return tuple(parts)

    normalized: list[str] = []
    for part in parts:
        part = re.sub(r"^ch(?=r)", "k", part)
        for source, target in (
            ("tsch", "ch"),
            ("sch", "sh"),
            ("ph", "f"),
            ("th", "t"),
            ("kh", "h"),
            ("tz", "ts"),
            ("ay", "ei"),
            ("ey", "ei"),
        ):
            part = part.replace(source, target)
        part = re.sub(r"z(?=io)", "ts", part)
        part = re.sub(r"z$", "s", part)
        part = re.sub(r"c(?![eiy])", "k", part)
        part = part.replace("w", "v").replace("q", "k")
        part = re.sub(r"y$", "i", part)
        normalized.append(part)
    return tuple(normalized)


def likely_same_proper_name(source_surface: str, output_surface: str) -> bool:
    """Accept only a confident source-local transliteration, never a lookup."""

    preferred = _SOURCE_LOCAL_CYRILLIC_NAMES.get(_latin_fold(source_surface))
    source_parts = _transliteration_parts(preferred or source_surface)
    output_parts = _transliteration_parts(output_surface)
    if not source_parts or len(source_parts) != len(output_parts):
        return False

    def same_part(source: str, output: str) -> bool:
        source_variants = {source, re.sub(r"i(?=[aeou])", "y", source)}
        for variant in source_variants:
            if variant == output:
                return True
            if variant.endswith("a") and output in {
                variant[:-1] + suffix for suffix in ("u", "e", "oi")
            }:
                return True
            if variant[-1:] not in "aeiouy" and output in {
                variant + suffix for suffix in ("a", "u", "om", "e")
            }:
                return True
        return False

    return all(
        same_part(source, output)
        for source, output in zip(source_parts, output_parts, strict=True)
    )


def _loosely_similar_name(source_surface: str, output_surface: str) -> bool:
    source_parts = _transliteration_parts(source_surface)
    output_parts = _transliteration_parts(output_surface)
    if not 1 <= len(source_parts) <= 4 or len(source_parts) != len(output_parts):
        return False
    ratios = [
        SequenceMatcher(None, source, output).ratio()
        for source, output in zip(source_parts, output_parts, strict=True)
    ]
    return ratios[-1] >= 0.65 and sum(ratios) / len(ratios) >= 0.58


def check_source_local_output_quality(
    source_text: str,
    output_text: str,
) -> tuple[EntityHygieneViolation, ...]:
    """Protect local English person names and reject leftover editorial prose."""

    violations: list[EntityHygieneViolation] = []
    source_names = source_local_person_names(source_text)
    output_names = tuple(
        dict.fromkeys(match.group(1) for match in _OUTPUT_CYRILLIC_NAME_RE.finditer(output_text))
    )
    role_names = tuple(
        dict.fromkeys(match.group(1) for match in _OUTPUT_PERSON_ROLE_RE.finditer(output_text))
    )
    for source_name in source_names:
        if re.search(rf"(?<![\w]){re.escape(source_name)}(?![\w])", output_text, re.I):
            preferred = _SOURCE_LOCAL_CYRILLIC_NAMES.get(_latin_fold(source_name))
            detail = (
                f'SOURCE person is "{source_name}"; render it in Cyrillic as '
                f'"{preferred}".'
                if preferred
                else f'SOURCE person is "{source_name}"; render this name in Cyrillic.'
            )
            violations.append(EntityHygieneViolation("LATIN_PERSON_NAME", detail))
            continue
        if any(likely_same_proper_name(source_name, name) for name in output_names):
            continue
        suspicious = next(
            (name for name in output_names if _loosely_similar_name(source_name, name)),
            None,
        )
        if suspicious is None and len(source_names) == 1 and len(role_names) == 1:
            suspicious = role_names[0]
        if suspicious is not None:
            violations.append(
                EntityHygieneViolation(
                    "NAME_MUTATION",
                    f'SOURCE person is "{source_name}"; suspicious OUTPUT spelling is '
                    f'"{suspicious}". Use the supported Cyrillic spelling or defer.',
                )
            )

    prose = output_text
    for source_name in source_names:
        prose = re.sub(
            rf"(?<![\w]){re.escape(source_name)}(?![\w])",
            " ",
            prose,
            flags=re.I,
        )
    found_phrases = {
        phrase
        for phrase in _UNTRANSLATED_EDITORIAL_PHRASES
        if re.search(rf"(?<![A-Za-z]){re.escape(phrase)}(?![A-Za-z])", prose, re.I)
    }
    prose = _TITLE_LATIN_SPAN_RE.sub(" ", prose)
    found_phrases.update(match.group(0) for match in _LOWER_ENGLISH_SEQUENCE_RE.finditer(prose))
    if found_phrases:
        violations.append(
            EntityHygieneViolation(
                "UNTRANSLATED_ENGLISH",
                "OUTPUT contains untranslated generic English prose: "
                + ", ".join(sorted(found_phrases, key=str.casefold)),
            )
        )
    return tuple(violations)


__all__ = [
    "EntityHygieneViolation",
    "check_source_local_output_quality",
    "check_simple_entity_hygiene",
    "likely_same_proper_name",
    "mixed_script_tokens",
    "source_local_person_names",
]
