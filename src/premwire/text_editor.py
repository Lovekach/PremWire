from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request
from .http_transport import urlopen

from .config import EditorConfig
from .editorial import (
    build_card_copy_prompt,
    build_card_copy_retry_prompt,
    build_card_copy_review_prompt,
    build_editor_prompt,
    build_editorial_repair_prompt,
    build_factual_retry_prompt,
    build_polish_prompt,
    build_russian_retry_prompt,
)
from .russian_editorial import (
    assess_editorial,
    normalize_editorial_text,
    numeric_fact_tokens,
    source_is_promotional_only,
)
from .source_locked_editorial import (
    SourceLockedResult,
    build_source_fact_packet,
    build_source_locked_prompt,
    build_source_locked_repair_prompt,
    deterministic_fallback_draft,
    deterministic_language_repair,
    parse_source_locked_draft,
    render_source_locked_result,
    validate_source_locked_draft,
)
from .simple_editorial import (
    SIMPLE_EDITOR_REPAIR_SYSTEM_PROMPT,
    SIMPLE_EDITOR_SYSTEM_PROMPT,
    SIMPLE_EDITOR_USER_INSTRUCTION,
    assess_simple_source,
    sanitize_simple_publication_text,
    tiny_source_aware_cleanup,
)
from .simple_factual_guard import check_simple_factual_safety
from .simple_entity_hygiene import (
    check_simple_entity_hygiene,
    check_source_local_output_quality,
)
from .simple_source_risk import (
    analyze_simple_source_risks,
    dynamic_risk_instructions,
)


class EditorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _SimpleRepairIssue:
    code: str
    detail: str


def _simple_repair_issues(
    source_text: str,
    candidate: str,
) -> tuple[_SimpleRepairIssue, ...]:
    return tuple(
        _SimpleRepairIssue(violation.code, violation.detail)
        for violation in check_simple_factual_safety(source_text, candidate)
    ) + tuple(
        _SimpleRepairIssue(violation.code, violation.detail)
        for violation in check_simple_entity_hygiene(candidate)
    ) + tuple(
        _SimpleRepairIssue(violation.code, violation.detail)
        for violation in check_source_local_output_quality(source_text, candidate)
    )


_EXPLICIT_SOURCE_POSITION_RE = re.compile(
    r"\b(?:goalkeeper|keeper|вратар|defender|защитник|full-?back|right-?back|"
    r"left-?back|centre-?back|center-?back|midfielder|полузащитник|winger|вингер|"
    r"striker|forward|нападающ)\w*\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class DraftValidation:
    valid: bool
    issues: tuple[str, ...]


_SOFT_DRAFT_ISSUES = frozenset(
    {
        "long_dash",
        "missing_paragraphs",
        "unbalanced_quotes",
        "meta_preamble",
        "quoted_club_name",
    }
)


@dataclass(frozen=True, slots=True)
class CardCopy:
    category: str
    headline: str
    secondary: str


_META_PREFIXES = (
    "готовый текст",
    "вот готовый",
    "вариант поста",
    "я подготовил",
)
_TELEGRAM_REFERENCE = re.compile(r"(?:t\.me/|telegram[- ]?канал)", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*(?:https?://|www\.)[^)]+\)", re.IGNORECASE)
_PUBLIC_LINK_RE = re.compile(
    r"(?:https?://|www\.|(?:t\.me|telegram\.me|x\.com|twitter\.com)/)\S+"
    r"|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,24}"
    r"(?:/\S*)?",
    re.IGNORECASE,
)
_SOCIAL_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-zА-Яа-яЁё0-9_]+)")
_HASHTAG_RE = re.compile(r"(?<![\w#])#[A-Za-zА-Яа-яЁё0-9_]+")
_PROMOTIONAL_CTA_RE = re.compile(
    r"\b(?:подписывайся|подписывайтесь|подпишись|подпишитесь|"
    r"присоединяйся|присоединяйтесь|переходи|переходите|заходи|заходите|"
    r"оставайся с нами|оставайтесь с нами|следи за нами|следите за нами|"
    r"ставьте реакции|поставьте реакцию|делитесь постом|смотри(?:те)? все матчи)\b",
    re.IGNORECASE,
)
_SOURCE_ATTRIBUTION_RE = re.compile(
    r"(?:^|\n)\s*(?:источник|source)\s*:\s*[^\n]+",
    re.IGNORECASE,
)
_FILLER_PATTERNS = (
    re.compile(r"источник (?:предоставил|сообщил|передал)", re.IGNORECASE),
    re.compile(r"согласно имеющейся информации", re.IGNORECASE),
)
_COMPETITOR_SIGNATURES = (
    re.compile(r"\s*📝\s*Main Transfers\s*", re.IGNORECASE),
    re.compile(r"\s*🦁\s*Albion\s*[—-]?\s*", re.IGNORECASE),
)
_CARD_CATEGORIES = {
    "transfer",
    "failed_transfer",
    "rumour",
    "official",
    "breaking",
    "matchday",
    "lineup",
    "live",
    "full_time",
    "injury",
    "quote",
    "stats",
    "news",
}
_UNCERTAINTY_MARKERS = (
    "вероятно",
    "возможно",
    "может",
    "могут",
    "ожидается",
    "готовится",
    "готовы",
    "намерен",
    "намерена",
    "планирует",
    "должен",
    "должна",
    "по информации",
    "по данным",
    "переговор",
    "контакт",
    "интерес",
    "probably",
    "possibly",
    "could",
    "may",
    "expected",
    "reportedly",
    "talks",
    "negotiations",
    "in contact",
    "interest",
)
_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sh", "ы": "y", "э": "e", "ю": "yu",
        "я": "ya", "ь": "", "ъ": "",
    }
)
_KNOWN_BAD_SPELLINGS = (re.compile(r"\bчелзи\b", re.IGNORECASE),)
_TRANSLATED_CLUB_NAMES = frozenset(
    {
        "арсенал", "атлетико", "барселона", "бавария", "боруссия",
        "интер", "ливерпуль", "манчестер", "милан", "наполи", "псж",
        "реал", "рома", "челси", "ювентус",
    }
)
_GENERIC_ENTITY_WORDS = frozenset(
    {
        "association", "brand", "club", "director", "federation", "football",
        "president", "russian", "italian", "федерация", "футбольная",
        "директор", "президент", "бренд", "итальянская", "российским",
        "сборную", "италии",
    }
)
_PLAYER_POSITION_PATTERNS = {
    "goalkeeper": (
        re.compile(r"\b(?:вратар\w*|голкипер\w*)\b", re.IGNORECASE),
        re.compile(r"\b(?:goalkeeper|keeper)s?\b", re.IGNORECASE),
    ),
    "defender": (
        re.compile(r"\bзащитник\w*\b", re.IGNORECASE),
        re.compile(r"\b(?:defender|centre-back|center-back|full-back|left-back|right-back)s?\b", re.IGNORECASE),
    ),
    "midfielder": (
        re.compile(r"\b(?:полузащитник\w*|хавбек\w*)\b", re.IGNORECASE),
        re.compile(r"\b(?:midfielder|midfielders|midfield)\b", re.IGNORECASE),
    ),
    "winger": (
        re.compile(r"\bвингер\w*\b", re.IGNORECASE),
        re.compile(r"\bwingers?\b", re.IGNORECASE),
    ),
    "forward": (
        re.compile(r"\b(?:нападающ\w*|форвард\w*)\b", re.IGNORECASE),
        re.compile(r"\b(?:striker|forward)s?\b", re.IGNORECASE),
    ),
}
_CARD_ACTION_ROOTS = (
    "ВОЗГЛАВ", "ВЕРН", "ВЫГЛЯД", "ВЫИГР", "ВЫКУП", "ВЫРАЗ", "ДОГОВОР", "ЗАБИЛ",
    "ЗАВЕРШ", "ЗАИНТЕРЕС", "ИНТЕРЕС", "КРИТИК", "ЛИДИР", "НАЧАЛ", "НАЗНАЧ",
    "ИЗМЕН", "МОЛИЛ", "ОБСУЖ", "ОБЪЯВ", "ОПУБЛИК", "ОСТАН", "ОТКАЗ", "ОТМЕН", "ОТПУСК", "ОФОРМ", "ПЕРЕЙ",
    "ПЕРЕШ", "ПОБЛАГОДАР", "ПОКИН", "ПОЛУЧ", "ПОДПИС", "ПРЕКРАЩ",
    "ПЛАНИР", "ПОДТВЕРД", "ПОКАЗ", "ПРЕДЛОЖ", "ПРЕДСТАВ", "ПРЕТЕНД", "ПРОВЕЛ", "ПРОДОЛЖ", "ПРОКОММЕНТ", "ПРОПУСТ", "ПРОШ",
    "РАССМАТР", "РАСШИР", "СВЯЗАЛ", "СОГЛАС",
    "СДЕЛАЛ", "СООБЩ", "СОРВАЛ", "СКОНЧАЛ", "СЛЕДИТ", "СТАЛ", "СТАНЕТ", "ТРАВМИР",
    "УВОЛ", "УШЕЛ", "УШЁЛ", "ХОЧЕТ", "ЗАЯВ",
)
_CARD_ACTION_FAMILIES = (
    ("ВОЗГЛАВ", "ПРЕТЕНД", "КАНДИДАТ", "ГЛАВН", "ТРЕНЕР", "ПОСТ"),
    (
        "ВЫКУП", "ПОДПИС", "ПЕРЕЙ", "ПЕРЕШ", "ТРАНСФЕР", "СДЕЛК",
        "ДОГОВОР", "КОНТАКТ", "НАЧАЛ", "ОБСУЖ", "ОФОРМ", "ПЕРЕГОВОР",
        "ПРЕДЛОЖ", "СВЯЗАЛ", "СОГЛАС", "ЗАПРОС",
        "СДЕЛАЛ",
    ),
    ("ЗАИНТЕРЕС", "ИНТЕРЕС", "ЛИДИР", "СЛЕДИТ", "РАССМАТР"),
    ("ОТМЕН", "СОРВАЛ", "ПРЕКРАЩ", "ОТКАЗ"),
    ("РАСШИР", "УВЕЛИЧ"),
    ("ПОКИН", "УШЕЛ", "УШЁЛ", "УХОД"),
    ("ПОКАЗ", "ПРЕДСТАВ", "ОБЪЯВ"),
)
_CARD_SEMANTIC_STOPWORDS = {
    "and", "for", "from", "into", "the", "with",
    "без", "был", "была", "были", "для", "его", "или", "как", "может",
    "новый", "новая", "новые", "она", "они", "после", "перед", "при",
    "свой", "свою", "стал", "стала", "что", "этот",
    "bez", "byl", "byla", "byli", "dlya", "ego", "ili", "iz", "kak",
    "mozhet", "na", "novyi", "novaya", "novye", "o", "on", "ona", "oni",
    "ot", "po", "posle", "pered", "pri", "s", "so", "svoi", "svoyu",
    "stal", "stala", "u", "v", "vo", "chto", "etot",
}


def infer_card_category(text: str) -> str:
    folded = text.casefold()
    if any(value in folded for value in (
        "сделка не состоится", "переговоры прекращены", "отказался от перехода",
        "отказалась от перехода", "трансфер сорвал", "переход сорвал",
        "сорвался", "сорвалась", "сделка отменена",
    )):
        return "failed_transfer"
    if any(value in folded for value in ("официально", "подписал контракт", "объявил о переходе")):
        return "official"
    if any(value in folded for value in ("травм", "поврежден", "повреждён")):
        return "injury"
    if any(value in folded for value in ("статистик", "голов", "ассист", "матчей")) and any(
        char.isdigit() for char in text
    ):
        return "stats"
    if any(value in folded for value in ("стартовый состав", "стартовые одиннадцать")):
        return "lineup"
    if any(value in folded for value in ("финальный свисток", "матч заверш", "итог матча")):
        return "full_time"
    if any(value in folded for value in (
        "интерес", "может перейти", "связывают", "слух", "следит за", "следят за",
    )):
        return "rumour"
    if any(value in folded for value in (
        "трансфер", "переход", "переговор", "выкупить", "подписать", "сделк",
    )):
        return "transfer"
    return "news"


def is_russian_text(text: str) -> bool:
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cyrillic < 5:
        return False
    return cyrillic / max(cyrillic + latin, 1) >= 0.45


def clean_draft(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
        if value.casefold().startswith("markdown\n"):
            value = value[len("markdown\n") :].lstrip()
    value = value.replace("—", "-").strip()
    while value.count("»") > value.count("«"):
        if value.endswith("»."):
            value = value[:-2] + "."
        elif value.endswith("»"):
            value = value[:-1].rstrip()
        else:
            break
    return sanitize_publication_text(value)


def sanitize_publication_text(text: str) -> str:
    """Remove links, social handles and channel-promotion calls from a post."""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    value = re.sub(
        r"\([^()\n]*(?:https?://|www\.|t\.me/|telegram\.me/)[^()\n]*\)",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = _PUBLIC_LINK_RE.sub("", value)
    value = _SOCIAL_HANDLE_RE.sub(r"\1", value)
    value = _HASHTAG_RE.sub("", value)

    cleaned_paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n", value):
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", paragraph)
            if sentence.strip()
        ]
        kept = [
            sentence
            for sentence in sentences
            if not _PROMOTIONAL_CTA_RE.search(sentence)
            and sentence.strip(" -,:;.!?").casefold()
            not in {"подробности", "ссылка", "читать", "читать далее", "источник"}
        ]
        if not kept:
            continue
        cleaned = " ".join(kept)
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
        cleaned = re.sub(r"(?:🔗|⬇️?|👉)+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -,:;|")
        if cleaned:
            cleaned_paragraphs.append(cleaned)
    return "\n\n".join(cleaned_paragraphs).strip()


def format_editorial_paragraphs(text: str) -> str:
    """Split a valid long single-block draft without changing its wording."""
    value = clean_draft(text)
    if len(value) < 260 or "\n\n" in value:
        return value
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", value)
        if part.strip()
    ]
    if len(sentences) < 3:
        return value
    first_cut = max(1, (len(sentences) + 2) // 3)
    second_cut = max(first_cut + 1, (2 * len(sentences) + 2) // 3)
    groups = (
        sentences[:first_cut],
        sentences[first_cut:second_cut],
        sentences[second_cut:],
    )
    return "\n\n".join(" ".join(group) for group in groups if group)


def clean_source_text(text: str) -> str:
    value = text
    for pattern in _COMPETITOR_SIGNATURES:
        value = pattern.sub(" ", value)
    return sanitize_publication_text(value)


@lru_cache(maxsize=1)
def _club_aliases() -> frozenset[str]:
    aliases = {
        "арсенал", "челси", "ливерпуль", "барселона", "реал мадрид",
        "манчестер сити", "манчестер юнайтед", "тоттенхэм", "псж",
    }
    registry_path = Path(__file__).resolve().parents[2] / "config" / "club-emblems.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset(aliases)
    for club in payload.get("clubs", []):
        if not isinstance(club, dict):
            continue
        values = [club.get("name"), *(club.get("aliases_ru") or [])]
        for value in values:
            if isinstance(value, str) and value.strip():
                aliases.add(re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip())
    return frozenset(aliases)


def _has_quoted_club_name(text: str) -> bool:
    aliases = _club_aliases()
    case_suffixes = ("а", "у", "ом", "е", "ы", "и", "я", "ю", "ем")
    for value in re.findall(r"«([^»]{2,80})»|\"([^\"\n]{2,80})\"", text):
        candidate = next((part for part in value if part), "")
        normalized = re.sub(r"\s+", " ", candidate.casefold().replace("ё", "е")).strip()
        if normalized in aliases or any(
            normalized == alias + suffix
            for alias in aliases
            for suffix in case_suffixes
        ):
            return True
    return False


def _unquote_known_club_names(text: str) -> str:
    """Remove typographic quotes around club names, preserving quoted speech."""
    aliases = _club_aliases()
    case_suffixes = ("а", "у", "ом", "е", "ы", "и", "я", "ю", "ем")

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(1) or match.group(2) or ""
        normalized = re.sub(r"\s+", " ", candidate.casefold().replace("ё", "е")).strip()
        is_club = normalized in aliases or any(
            normalized == alias + suffix
            for alias in aliases
            for suffix in case_suffixes
        )
        return candidate if is_club else match.group(0)

    return re.sub(r"«([^»]{2,80})»|\"([^\"\n]{2,80})\"", replace, text)


def _player_positions(text: str) -> set[str]:
    return {
        position
        for position, patterns in _PLAYER_POSITION_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    }


def safe_source_fallback(text: str) -> str:
    value = clean_source_text(text).replace("—", "-").replace("–", "-")
    value = "".join(
        " " if unicodedata.category(char) in {"So", "Sk", "Mn"} else char
        for char in value
    )
    value = re.sub(r"\s+", " ", value).strip(" -")
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    if value and value[-1] not in ".!?»":
        value += "."
    return sanitize_publication_text(value)


def _latin_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    )
    folded = without_marks.casefold().translate(_CYRILLIC_TO_LATIN)
    return re.sub(r"\s+", " ", "".join(char if char.isalnum() else " " for char in folded)).strip()


def _name_phrases(text: str) -> tuple[str, ...]:
    values = re.findall(
        r"(?<![@#])\b(?:[A-ZА-ЯЁ][a-zа-яё]+)(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){1,2}\b",
        text,
    )
    filtered = (
        value
        for value in values
        if not any(part.casefold() in _GENERIC_ENTITY_WORDS for part in value.split())
    )
    return tuple(dict.fromkeys(_latin_name(value) for value in filtered))


def _surface_name_phrases(text: str) -> tuple[tuple[str, ...], ...]:
    """Return source name/club phrases with their original Russian spelling."""
    values = re.findall(
        r"(?<![@#])\b(?:[A-ZА-ЯЁ][a-zа-яё]+)(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){1,2}\b",
        text,
    )
    return tuple(tuple(value.upper().split()) for value in values)


def _similar_name(first: str, second: str) -> bool:
    first_parts = first.split()
    second_parts = second.split()
    if len(first_parts) < 2 or len(second_parts) < 2:
        return False
    # A sentence opener can be captured together with the following proper
    # name ("Однако Флорентино Перес"). Match the surname plus any earlier
    # component instead of treating that opener as a fabricated person.
    # Common Russian transliterations can differ strongly from the Latin
    # spelling (Andy -> Энди, City -> Сити). Keep the threshold permissive
    # enough for those forms while still requiring both name components.
    surname_matches = (
        SequenceMatcher(None, first_parts[-1], second_parts[-1]).ratio() >= 0.48
    )
    earlier_matches = any(
        SequenceMatcher(None, first, second).ratio() >= 0.48
        for first in first_parts[:-1]
        for second in second_parts[:-1]
    )
    return surname_matches and earlier_matches


def factual_consistency_issues(source: str, draft: str) -> tuple[str, ...]:
    issues: list[str] = []
    clean_source = safe_source_fallback(source)
    # A journalist credit is allowed editorial metadata.  It must not be
    # mistaken for a fabricated person in the factual comparison.
    draft_for_facts = _SOURCE_ATTRIBUTION_RE.sub("", draft).strip()
    if len(clean_source) < 220 and len(draft_for_facts) > max(
        round(len(clean_source) * 2.5), len(clean_source) + 150
    ):
        issues.append("unsupported_expansion")

    source_numbers = set(numeric_fact_tokens(clean_source))
    draft_numbers = set(numeric_fact_tokens(draft_for_facts))
    if not draft_numbers.issubset(source_numbers):
        issues.append("new_number")

    source_stems = {
        token.casefold()[:5]
        for token in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", clean_source)
    }
    cross_script_translation = (
        len(re.findall(r"[А-Яа-яЁё]", clean_source)) < 5
        and len(re.findall(r"[А-Яа-яЁё]", draft_for_facts)) >= 5
    )
    if not cross_script_translation:
        unsupported_names: list[str] = []
        for match in re.finditer(r"\b[А-ЯЁA-Z][а-яёa-z]{2,}\b", draft_for_facts):
            before = draft_for_facts[: match.start()].rstrip()
            if not before or before[-1] in ".!?\n":
                continue
            token = match.group(0)
            if token.casefold()[:5] not in source_stems:
                unsupported_names.append(token)
        if unsupported_names:
            issues.append("new_named_entity")
    else:
        # English-to-Russian transliteration has too many valid variants for a
        # deterministic name comparison (Bayern/Бавария, accented surnames,
        # Russian inflection).  Keep the stronger numeric, position and
        # certainty checks below; do not reject a whole post on this noisy
        # cross-script heuristic.
        pass
    source_folded = clean_source.casefold()
    draft_folded = draft_for_facts.casefold()
    source_is_uncertain = any(
        marker in source_folded for marker in _UNCERTAINTY_MARKERS
    )
    draft_keeps_uncertainty = any(
        marker in draft_folded for marker in _UNCERTAINTY_MARKERS
    )
    if source_is_uncertain and not draft_keeps_uncertainty:
        issues.append("certainty_inflation")
    source_positions = _player_positions(clean_source)
    draft_positions = _player_positions(draft_for_facts)
    if not draft_positions.issubset(source_positions):
        issues.append("new_player_position")
    return tuple(issues)


def validate_draft(text: str, max_chars: int) -> DraftValidation:
    issues: list[str] = []
    stripped = text.strip()
    if not stripped:
        issues.append("empty")
    if len(stripped) > max_chars:
        issues.append("too_long")
    if stripped and not is_russian_text(stripped):
        issues.append("not_russian")
    dash_checked = re.sub(
        r"(?:\b[А-ЯЁ][А-Яа-яЁё -]{1,80}\s+—\s+(?:игрок|новый\s+игрок)\b|"
        r"\bКонтракт\s+—\s+до\s+20\d{2}\s+года\b)",
        " ",
        stripped,
        flags=re.I,
    )
    if "—" in dash_checked:
        issues.append("long_dash")
    if _TELEGRAM_REFERENCE.search(stripped):
        issues.append("telegram_aggregator_reference")
    if (
        _PUBLIC_LINK_RE.search(stripped)
        or _SOCIAL_HANDLE_RE.search(stripped)
        or _HASHTAG_RE.search(stripped)
    ):
        issues.append("external_link_or_social_handle")
    if _PROMOTIONAL_CTA_RE.search(stripped):
        issues.append("promotional_call_to_action")
    if any(pattern.search(stripped) for pattern in _FILLER_PATTERNS):
        issues.append("generic_source_filler")
    if stripped.count("«") != stripped.count("»"):
        issues.append("unbalanced_quotes")
    if any(pattern.search(stripped) for pattern in _COMPETITOR_SIGNATURES):
        issues.append("competitor_signature")
    if re.search(r"\b(?=[A-Za-zА-Яа-яЁё]*[A-Za-z])(?=[A-Za-zА-Яа-яЁё]*[А-Яа-яЁё])[A-Za-zА-Яа-яЁё]+\b", stripped):
        issues.append("mixed_script_word")
    if any(pattern.search(stripped) for pattern in _KNOWN_BAD_SPELLINGS):
        issues.append("known_name_misspelling")
    if _has_quoted_club_name(stripped):
        issues.append("quoted_club_name")
    if len(stripped) >= 360 and "\n\n" not in stripped:
        issues.append("missing_paragraphs")
    first_line = stripped.splitlines()[0].casefold() if stripped else ""
    if any(first_line.startswith(prefix) for prefix in _META_PREFIXES):
        issues.append("meta_preamble")
    return DraftValidation(not issues, tuple(issues))


def repair_soft_draft(text: str, max_chars: int) -> str | None:
    """Fix presentation-only defects without asking the model to rewrite facts."""
    validation = validate_draft(text, max_chars)
    if validation.valid:
        return text.strip()
    if not set(validation.issues).issubset(_SOFT_DRAFT_ISSUES):
        return None

    repaired = text.strip().replace("—", "-")
    if "quoted_club_name" in validation.issues:
        repaired = _unquote_known_club_names(repaired)
    if "meta_preamble" in validation.issues:
        lines = repaired.splitlines()
        if lines and any(lines[0].casefold().startswith(prefix) for prefix in _META_PREFIXES):
            repaired = "\n".join(lines[1:]).strip()
    if "unbalanced_quotes" in validation.issues:
        repaired = repaired.replace("«", "").replace("»", "")
    if "missing_paragraphs" in validation.issues and "\n\n" not in repaired:
        sentences = [
            value.strip()
            for value in re.split(r"(?<=[.!?])\s+", repaired)
            if value.strip()
        ]
        if len(sentences) >= 2:
            split_at = max(1, len(sentences) // 2)
            repaired = " ".join(sentences[:split_at]) + "\n\n" + " ".join(sentences[split_at:])
        else:
            comma_positions = [match.end() for match in re.finditer(r",\s+", repaired)]
            if comma_positions:
                midpoint = len(repaired) / 2
                split_at = min(comma_positions, key=lambda value: abs(value - midpoint))
                repaired = repaired[:split_at].rstrip() + "\n\n" + repaired[split_at:].lstrip()
            else:
                # A long one-sentence update may have neither a second full
                # stop nor a comma.  A paragraph break at a word boundary is
                # purely presentational and does not rewrite any fact.
                spaces = [match.start() for match in re.finditer(r"\s+", repaired)]
                if spaces:
                    midpoint = len(repaired) / 2
                    split_at = min(spaces, key=lambda value: abs(value - midpoint))
                    repaired = repaired[:split_at].rstrip() + "\n\n" + repaired[split_at:].lstrip()

    repaired = format_editorial_paragraphs(repaired)
    return repaired if validate_draft(repaired, max_chars).valid else None


class OllamaTextEditor:
    def __init__(self, config: EditorConfig) -> None:
        self.config = config
        self.last_simple_trace: dict[str, Any] = {}

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        from urllib.parse import urlsplit
        address = urlsplit(self.config.base_url)
        if (address.scheme not in {'http', 'https'} or not address.hostname
                or address.username or address.password or address.query or address.fragment
                or not path.startswith('/api/') or '?' in path or '#' in path):
            raise EditorError('INVALID_OLLAMA_ENDPOINT')
        if address.scheme == 'http' and address.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise EditorError('REMOTE_OLLAMA_REQUIRES_HTTPS')
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.config.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise EditorError(f"Ollama HTTP {exc.code}; response details withheld") from None
        except URLError as exc:
            raise EditorError(
                "Ollama недоступна. Установите и запустите приложение Ollama."
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EditorError("Ollama вернула некорректный ответ") from None

    def available_models(self) -> tuple[str, ...]:
        payload = self._request("GET", "/api/tags")
        models = payload.get("models", []) if isinstance(payload, dict) else []
        return tuple(
            str(model.get("name"))
            for model in models
            if isinstance(model, dict) and model.get("name")
        )

    def verify(self) -> tuple[bool, str]:
        models = self.available_models()
        configured = self.config.model.casefold()
        found = any(
            name.casefold() == configured
            or name.casefold().removesuffix(":latest") == configured
            for name in models
        )
        if found:
            return True, f"Ollama готова, модель {self.config.model} установлена"
        return False, (
            f"Ollama работает, но модель {self.config.model} не установлена. "
            f"Выполните: ollama pull {self.config.model}"
        )

    def _generate(self, prompt: str, *, temperature: float | None = None) -> str:
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": self.config.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "keep_alive": self.config.keep_alive,
                "options": {
                    "temperature": (
                        self.config.temperature if temperature is None else temperature
                    ),
                    "num_ctx": self.config.context_length,
                    "num_predict": 500,
                },
            },
        )
        try:
            text = str(payload["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise EditorError("В ответе Ollama нет готового текста") from exc
        return format_editorial_paragraphs(text)

    def _simple_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "temperature": self.config.temperature,
            "num_ctx": self.config.context_length,
            "num_predict": self.config.simple_num_predict,
        }
        for key, value in (
            ("top_p", self.config.simple_top_p),
            ("top_k", self.config.simple_top_k),
            ("seed", self.config.simple_seed),
            ("presence_penalty", self.config.simple_presence_penalty),
        ):
            if value is not None:
                options[key] = value
        return options

    def _simple_chat(self, system_content: str, user_content: str) -> str:
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
                "think": False,
                "keep_alive": self.config.keep_alive,
                "options": self._simple_options(),
            },
        )
        try:
            value = str(payload["message"]["content"]).strip()
        except (KeyError, TypeError) as exc:
            raise EditorError("SIMPLE_EDITOR_INVALID_RESPONSE") from exc
        if not value:
            raise EditorError("SIMPLE_EDITOR_EMPTY_RESPONSE")
        return value

    def editorial_rank(self, source_text: str) -> dict[str, object]:
        """Score story value only; this output is never article-writing context."""

        categories = (
            "MATCH_RESULT", "MATCH_NEWS", "TRANSFER_OFFICIAL", "TRANSFER_UPDATE",
            "INJURY", "TEAM_NEWS", "MANAGER_NEWS", "QUOTE", "CONTRACT",
            "TOURNAMENT_NEWS", "GENERAL_FOOTBALL",
        )
        schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(categories)},
                "importance": {"type": "integer", "minimum": 0, "maximum": 100},
                "interest": {"type": "integer", "minimum": 0, "maximum": 100},
                "novelty": {"type": "integer", "minimum": 0, "maximum": 100},
                "reason": {"type": "string", "maxLength": 160},
                "recommended_mode": {
                    "type": "string",
                    "enum": ["NORMAL", "SAFE_SUMMARY"],
                },
            },
            "required": [
                "category", "importance", "interest", "novelty", "reason",
                "recommended_mode",
            ],
            "additionalProperties": False,
        }
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are PremWire's football editorial desk. Rank whether a fresh "
                            "story is worth showing now. General football knowledge may affect "
                            "importance and interest, but must never create article facts. "
                            "Complexity suggests SAFE_SUMMARY, not rejection. Scores MUST use "
                            "the full 0-100 scale, never 0-10: routine stories are usually "
                            "30-55, major stories 75-95, exceptional breaking stories up to "
                            "100. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "SOURCE FOR RANKING ONLY:\n" + source_text,
                    },
                ],
                "stream": False,
                "think": False,
                "keep_alive": self.config.keep_alive,
                "format": schema,
                "options": {
                    "temperature": 0.0,
                    "num_ctx": self.config.context_length,
                    "num_predict": 180,
                },
            },
        )
        try:
            content = str(payload["message"]["content"]).strip()
            result = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise EditorError("EDITORIAL_DESK_INVALID_RESPONSE") from exc
        if not isinstance(result, dict):
            raise EditorError("EDITORIAL_DESK_INVALID_RESPONSE")
        return result

    def _generate_simple_rewrite(self, source_text: str) -> str:
        risks = analyze_simple_source_risks(source_text)
        risk_instructions = dynamic_risk_instructions(risks)
        position_constraint = ""
        if not _EXPLICIT_SOURCE_POSITION_RE.search(source_text):
            position_constraint = (
                "\n\nSOURCE CONTAINS NO PLAYER POSITION. Запрещены слова форвард, "
                "нападающий, вингер, полузащитник, защитник и вратарь."
            )
        risk_block = f"\n{risk_instructions}" if risk_instructions else ""
        user_content = (
            "SOURCE:\n"
            f"{source_text}\n\n"
            "INSTRUCTION:\n"
            f"{SIMPLE_EDITOR_USER_INSTRUCTION}"
            f"{risk_block}"
            f"{position_constraint}"
        )
        return self._simple_chat(SIMPLE_EDITOR_SYSTEM_PROMPT, user_content)

    def _generate_simple_safe_summary(self, source_text: str) -> str:
        risks = analyze_simple_source_risks(source_text)
        risk_instructions = dynamic_risk_instructions(risks)
        risk_block = f"\n{risk_instructions}" if risk_instructions else ""
        user_content = (
            "SOURCE:\n"
            f"{source_text}\n\n"
            "INSTRUCTION:\n"
            "Сделай точное изложение в 2–4 предложениях и двух коротких абзацах, "
            "если SOURCE содержит несколько фактов. Ориентир 350–650 знаков, "
            "но не дополняй короткий источник ради объёма. Сохрани главный факт "
            "и 2–3 явные полезные детали: условия, статистику, существенную цитату. "
            "В новости о завершении карьеры сохраняй число матчей за сборную, "
            "если оно дано; в результате — счёт и положение команд, в трансфере — "
            "срок и сумму. Разделяй основной факт и детали пустой строкой. "
            "Опускай только неоднозначные или не относящиеся к новости детали. Для результата "
            "матча сохрани победителя и точный счёт; заметный дубль/хет-трик можно "
            "сохранить. Не интерпретируй механику гола, home/away, договорный статус "
            "или причину события: при неоднозначности опусти такую деталь. "
            "После углового или стандарта достаточно назвать автора гола и факт "
            "гола — не приписывай игроку конкретный удар или передачу. "
            "Ничего не вычисляй и не добавляй из памяти."
            f"{risk_block}"
        )
        return self._simple_chat(SIMPLE_EDITOR_SYSTEM_PROMPT, user_content)

    def _generate_simple_repair(
        self,
        source_text: str,
        candidate: str,
        violations: tuple[_SimpleRepairIssue, ...],
    ) -> str:
        violation_text = "\n".join(
            f"- {violation.code}: {violation.detail}" for violation in violations
        )
        targeted_instruction = ""
        if any(
            violation.code == "AMBIGUOUS_STAT_ROLE" for violation in violations
        ):
            targeted_instruction = (
                "\nОБЯЗАТЕЛЬНО: убери формулировки «автор голов»/«голеадор». "
                "Не удаляй только цифры: напиши нейтрально «набрал 2+2» или "
                "верни поддержанное SOURCE описание роли. Если SOURCE прямо "
                "называет игроков главными звёздами, сохрани «главными звёздами»."
            )
        if any(violation.code == "NAME_MUTATION" for violation in violations):
            targeted_instruction += (
                "\nОБЯЗАТЕЛЬНО: используй ровно указанное в нарушении имя SOURCE. "
                "Не придумывай другого человека. Передай имя русской "
                "транслитерацией; латиницу в готовом тексте не оставляй."
            )
        if any(violation.code == "LATIN_PERSON_NAME" for violation in violations):
            targeted_instruction += (
                "\nОБЯЗАТЕЛЬНО: транслитерируй указанное source-local имя "
                "кириллицей. Сохрани все части и дефисы имени, не заменяй человека."
            )
        if any(
            violation.code == "UNTRANSLATED_ENGLISH" for violation in violations
        ):
            targeted_instruction += (
                "\nОБЯЗАТЕЛЬНО: переведи generic English prose на естественный "
                "русский. Source-local имена людей передай кириллицей. "
                "Для transfer phrases используй: undisclosed fee — сумма трансфера "
                "не разглашается; free transfer — свободный агент; loan deal — "
                "аренда; permanent deal — постоянный трансфер; medical booked — "
                "медосмотр запланирован; deal agreed — сделка согласована."
            )
        user_content = (
            "SOURCE:\n"
            f"{source_text}\n\n"
            "CANDIDATE:\n"
            f"{candidate}\n\n"
            "FACTUAL VIOLATIONS:\n"
            f"{violation_text}\n\n"
            "Исправь candidate так, чтобы нарушения исчезли. "
            "Верни только исправленный русский текст."
            f"{targeted_instruction}"
        )
        return self._simple_chat(SIMPLE_EDITOR_REPAIR_SYSTEM_PROMPT, user_content)

    def _sanitize_simple_candidate(
        self,
        source_name: str,
        source_text: str,
        candidate: str,
    ) -> str:
        cleaned = tiny_source_aware_cleanup(source_text, candidate)
        try:
            return sanitize_simple_publication_text(
                cleaned,
                source_name=source_name,
                max_chars=self.config.max_output_chars,
            )
        except ValueError as exc:
            raise EditorError(str(exc)) from exc

    def simple_rewrite(
        self,
        source_name: str,
        source_text: str,
        *,
        editorial_mode: str = "NORMAL",
    ) -> str:
        """Rewrite once, then allow one targeted factual repair when required."""

        self.last_simple_trace = {
            "initial_attempts": 0,
            "repair_used": False,
            "risk_flags": [],
            "rewrite_seconds": 0.0,
            "repair_seconds": 0.0,
            "initial_violations": [],
            "final_violations": [],
            "guard_passed": False,
            "editorial_mode": editorial_mode,
        }
        decision = assess_simple_source(source_text[: self.config.max_source_chars])
        if not decision.allowed:
            raise EditorError(decision.reason or "SKIP_NON_FOOTBALL")
        risks = analyze_simple_source_risks(decision.clean_text)
        self.last_simple_trace["risk_flags"] = sorted(risk.value for risk in risks)
        last_error: EditorError | None = None
        raw = ""
        for attempt in range(2):
            self.last_simple_trace["initial_attempts"] = attempt + 1
            try:
                rewrite_started = time.perf_counter()
                if editorial_mode == "SAFE_SUMMARY":
                    raw = self._generate_simple_safe_summary(decision.clean_text)
                else:
                    raw = self._generate_simple_rewrite(decision.clean_text)
                self.last_simple_trace["rewrite_seconds"] += (
                    time.perf_counter() - rewrite_started
                )
                last_error = None
                break
            except EditorError as exc:
                if "rewrite_started" in locals():
                    self.last_simple_trace["rewrite_seconds"] += (
                        time.perf_counter() - rewrite_started
                    )
                last_error = exc
        if last_error is not None:
            raise last_error
        candidate = self._sanitize_simple_candidate(
            source_name,
            decision.clean_text,
            raw,
        )
        violations = _simple_repair_issues(decision.clean_text, candidate)
        self.last_simple_trace["initial_violations"] = [
            violation.code for violation in violations
        ]
        if violations:
            self.last_simple_trace["repair_used"] = True
            repair_started = time.perf_counter()
            repaired_raw = self._generate_simple_repair(
                decision.clean_text,
                candidate,
                violations,
            )
            self.last_simple_trace["repair_seconds"] = (
                time.perf_counter() - repair_started
            )
            candidate = self._sanitize_simple_candidate(
                source_name,
                decision.clean_text,
                repaired_raw,
            )
            violations = _simple_repair_issues(decision.clean_text, candidate)
        self.last_simple_trace["final_violations"] = [
            violation.code for violation in violations
        ]
        if violations:
            codes = ",".join(violation.code for violation in violations)
            raise EditorError(f"SIMPLE_EDITOR_FACTUAL_GUARD_FAILED:{codes}")
        self.last_simple_trace["guard_passed"] = True
        return candidate

    def _generate_card_json(self, prompt: str, *, temperature: float) -> str:
        schema = {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "secondary": {"type": "string"},
            },
            "required": ["headline", "secondary"],
            "additionalProperties": False,
        }
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты редактор заголовков футбольного канала. "
                            "Верни ровно один JSON-объект по заданной схеме. "
                            "Не добавляй Markdown, пояснения или массив."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "keep_alive": self.config.keep_alive,
                "format": schema,
                "options": {
                    "temperature": temperature,
                    "num_predict": 180,
                },
            },
        )
        try:
            return clean_draft(str(payload["message"]["content"]))
        except (KeyError, TypeError) as exc:
            raise EditorError("В ответе Ollama нет JSON для карточки") from exc

    def _generate_source_locked_json(self, prompt: str, *, temperature: float) -> str:
        schema = {
            "type": "object",
            "properties": {
                "sentences": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "fact_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["text", "fact_ids"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["sentences"],
            "additionalProperties": False,
        }
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты source-locked редактор PremWire. Верни ровно один JSON-объект "
                            "по схеме. Каждое предложение обязано ссылаться на существующие fact_ids."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "keep_alive": self.config.keep_alive,
                "format": schema,
                "options": {
                    "temperature": temperature,
                    "num_predict": 500,
                },
            },
        )
        try:
            return clean_draft(str(payload["message"]["content"]))
        except (KeyError, TypeError) as exc:
            raise EditorError("В ответе Ollama нет source-locked JSON") from exc

    def rewrite(self, source_name: str, source_text: str) -> str:
        return self.rewrite_supported(source_name, source_text).final_text

    def rewrite_supported(self, source_name: str, source_text: str) -> SourceLockedResult:
        # Preserve line boundaries and @handles until the source-locked layer
        # separates quoted/reposted stories and trusted attribution. Generic
        # publication sanitization collapses precisely those structural cues.
        source = source_text.replace("\r\n", "\n").replace("\r", "\n")[: self.config.max_source_chars]
        packet = build_source_fact_packet(source, source_name)
        if packet.intent != "NEWS" or source_is_promotional_only(source):
            raise EditorError("EDITORIAL_FAIL_PROMOTIONAL: PROMOTIONAL_PRIMARY")
        if packet.relevance_class == "OPTIONAL_OFF_PITCH":
            raise EditorError("EDITORIAL_FAIL_NON_FOOTBALL_RELEVANCE: OPTIONAL_OFF_PITCH")
        if packet.source_language_issue:
            raise EditorError(f"EDITORIAL_FAIL_TERMINOLOGY: {packet.source_language_issue}")
        if not packet.facts:
            raise EditorError("EDITORIAL_FAIL_FACT_RELATION: NO_SOURCE_FACTS")
        if packet.unresolved_entities:
            raise EditorError("EDITORIAL_FAIL_ENTITY: UNRESOLVED_SOURCE_ENTITY")
        if packet.provenance_issue:
            raise EditorError(f"EDITORIAL_FAIL_SOURCE: {packet.provenance_issue}")

        def accepted(draft):
            validation = validate_source_locked_draft(
                packet,
                draft,
                max_chars=self.config.max_output_chars,
            )
            result = render_source_locked_result(packet, draft)
            public_validation = validate_draft(result.final_text, self.config.max_output_chars)
            if validation.passed and public_validation.valid:
                return result, (), result
            issues = tuple(dict.fromkeys((*validation.issues, *public_validation.issues)))
            return None, issues, result

        def generate(prompt: str, *, temperature: float):
            try:
                raw = self._generate_source_locked_json(prompt, temperature=temperature)
                return parse_source_locked_draft(raw)
            except (EditorError, ValueError) as exc:
                raise EditorError(f"EDITORIAL_FAIL_SCHEMA: {exc}") from exc

        fallback = deterministic_fallback_draft(packet)
        try:
            first = generate(
                build_source_locked_prompt(packet),
                # Source-locked realization favours determinism over stylistic
                # variety; factual freedom is intentionally minimal in V3.
                temperature=min(self.config.temperature, 0.1),
            )
        except EditorError:
            if fallback is not None:
                fallback_result, _, _ = accepted(fallback)
                if fallback_result is not None:
                    return fallback_result
            raise
        first_result, first_issues, _ = accepted(first)
        if first_result is not None:
            return first_result

        deterministic_repair = deterministic_language_repair(packet, first, first_issues)
        if deterministic_repair is not None and deterministic_repair != first:
            repaired_result, repaired_issues, _ = accepted(deterministic_repair)
            if repaired_result is not None:
                return repaired_result
            first_issues = tuple(dict.fromkeys((*first_issues, *repaired_issues)))
        try:
            repaired = generate(
                build_source_locked_repair_prompt(packet, first, first_issues),
                temperature=0.1,
            )
        except EditorError as exc:
            if fallback is not None:
                fallback_result, _, _ = accepted(fallback)
                if fallback_result is not None:
                    return fallback_result
            raise EditorError("EDITORIAL_FAIL_LANGUAGE: repair unavailable") from exc
        repaired_result, issues, _ = accepted(repaired)
        if repaired_result is not None:
            return repaired_result
        deterministic_repair = deterministic_language_repair(packet, repaired, issues)
        if deterministic_repair is not None and deterministic_repair != repaired:
            repaired_result, deterministic_issues, _ = accepted(deterministic_repair)
            if repaired_result is not None:
                return repaired_result
            issues = tuple(dict.fromkeys((*issues, *deterministic_issues)))
        if fallback is not None:
            fallback_result, fallback_issues, _ = accepted(fallback)
            if fallback_result is not None:
                return fallback_result
            issues = tuple(dict.fromkeys((*issues, *fallback_issues)))
        if any(issue == "PROMOTIONAL_PRIMARY" for issue in issues):
            decision = "EDITORIAL_FAIL_PROMOTIONAL"
        elif any("ENTITY" in issue or "CLUB" in issue for issue in issues):
            decision = "EDITORIAL_FAIL_ENTITY"
        elif any("SOURCE" in issue for issue in issues):
            decision = "EDITORIAL_FAIL_SOURCE"
        elif any("LANGUAGE" in issue or "SENTENCE" in issue for issue in issues):
            decision = "EDITORIAL_FAIL_LANGUAGE"
        else:
            decision = "EDITORIAL_FAIL_FACT_RELATION"
        raise EditorError(f"{decision}: " + ", ".join(issues or ("unknown",)))

    def translate_for_x(self, russian_text: str, max_chars: int) -> str:
        """Translate an already approved Telegram post for the English X feed.

        This is deliberately a translation, not a second editorial rewrite: X
        must mirror the published Telegram fact set and never become an
        independent source of new claims.
        """
        source = clean_source_text(russian_text)[: self.config.max_source_chars]
        body_limit = max(80, max_chars - 42)
        prompt = (
            "Translate the Russian football news below into concise, current, "
            "natural conversational English for an X post. Preserve every fact, uncertainty marker, "
            "name, club and number. Do not add context, opinions, calls to "
            "action, links, @handles, hashtags, source credits or a title. "
            f"Keep it under {body_limit} characters. Return only the English post.\n\n"
            f"Russian post:\n{source}"
        )
        translated = self._generate(prompt, temperature=0.1).strip()
        translated = re.sub(r"(?<!\w)#[\w-]+", "", translated)
        translated = re.sub(r"(?<!\w)@[\w_]+", "", translated)
        translated = _PUBLIC_LINK_RE.sub("", translated)
        translated = re.sub(r"\s+", " ", translated).strip(" -\n\t")
        if not translated or len(re.findall(r"[A-Za-z]", translated)) < 8:
            raise EditorError("Ollama returned no usable English X translation")
        if len(translated) > body_limit:
            shortened = translated[: body_limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
            translated = (shortened or translated[:body_limit]).rstrip(".!?") + "…"
        return translated

    def _parse_card_copy(self, raw: str) -> CardCopy:
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`").strip()
            if candidate.casefold().startswith("json"):
                candidate = candidate[4:].lstrip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            match = re.search(r"\{.*\}", candidate, re.DOTALL)
            if not match:
                raise EditorError("Ollama не вернула JSON для карточки") from exc
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError as nested:
                raise EditorError("Ollama вернула некорректный JSON для карточки") from nested
        if not isinstance(payload, dict):
            raise EditorError("Ollama вернула некорректные данные карточки")
        def clean_card_line(value: object) -> str:
            line = clean_draft(str(value))
            line = re.sub(r"[\n\r]+", " ", line)
            line = line.replace('"', "").replace("«", "").replace("»", "")
            return re.sub(r"\s{2,}", " ", line).rstrip(".!?,:; ").upper()

        headline = clean_card_line(payload.get("headline", ""))
        secondary = clean_card_line(payload.get("secondary", ""))
        return CardCopy(
            "news",
            headline,
            secondary,
        )

    def _validate_card_copy(self, copy: CardCopy, draft_text: str) -> CardCopy:
        category = infer_card_category(draft_text)
        headline = copy.headline
        secondary = copy.secondary
        if category not in _CARD_CATEGORIES:
            category = "news"
        words = headline.split()
        if not 2 <= len(words) <= 9:
            raise EditorError("Заголовок карточки должен содержать от 2 до 9 слов")
        if not is_russian_text(headline) or (
            secondary and not is_russian_text(secondary)
        ):
            raise EditorError("Надписи карточки должны быть на русском языке")
        generic_openings = (
            "СТАЛО ИЗВЕСТНО",
            "СООБЩАЕТСЯ ЧТО",
            "ПО ДАННЫМ ИСТОЧНИКА",
            "КАК СООБЩАЕТ",
            "ИСТОЧНИК СООБЩИЛ",
            "НОВЫЕ ДЕТАЛИ",
        )
        if headline.startswith(generic_openings):
            raise EditorError("Заголовок карточки является вводной фразой, а не сутью новости")
        incomplete_endings = {
            "А",
            "БЕЗ",
            "В",
            "ВО",
            "ДЛЯ",
            "И",
            "ИЗ",
            "К",
            "КАК",
            "НА",
            "НЕ",
            "НО",
            "О",
            "ОБ",
            "ОТ",
            "ПО",
            "ПРИ",
            "С",
            "СО",
            "ЧТО",
        }
        if headline.split()[-1] in incomplete_endings:
            raise EditorError("Заголовок карточки обрывается на служебном слове")
        if words[0] in {"В", "ВО", "ДО", "ЗА", "ИЗ", "НА", "ПО", "ПОСЛЕ", "ПЕРЕД", "ПРИ", "С"}:
            raise EditorError("Заголовок карточки не должен начинаться с предлога")
        forbidden_placeholders = {
            "ИГРОК", "ФУТБОЛИСТ", "КЛУБ", "КОМАНДА", "ГЕРОЙ",
            "ОБЪЕКТ", "ДЕЙСТВИЕ", "ТЕКСТ",
        }
        if any(word in forbidden_placeholders for word in words):
            raise EditorError("Заголовок карточки заменил героя общим словом")
        incomplete_last_words = {
            "ВЕДУЩИХ", "ФИНАЛЬНОГО", "МЕДИЦИНСКОЕ", "ВОЗМОЖНОСТЬ",
            "ОДНИМ", "НОВЫЕ", "ГЛАВНЫХ", "СЛЕДУЮЩИМ",
        }
        if words[-1] in incomplete_last_words or re.search(
            r"(?:ОГО|ЕГО|ОМУ|ЕМУ|ЫХ|ИХ|АЯ|ЯЯ|ОЕ|ЕЕ)$", words[-1]
        ):
            raise EditorError("Заголовок карточки оканчивается незавершённым определением")
        if category not in {"stats", "matchday", "lineup", "live", "full_time"} and not any(
            root in headline for root in _CARD_ACTION_ROOTS
        ):
            raise EditorError("В заголовке карточки нет ясного действия")
        if category in {"transfer", "rumour", "official", "news", "breaking", "injury", "quote"}:
            unfinished_action_endings = (
                "ГОТОВ", "ГОТОВА", "ХОЧЕТ", "МОЖЕТ", "РАССМАТРИВАЕТ",
                "ПЛАНИРУЕТ", "НАМЕРЕН", "НАМЕРЕНА", "ОБСУЖДАЕТ",
                "ВЫКУПИТЬ", "ПОДПИСАТЬ", "ПРИГЛАСИТЬ",
            )
            if words[-1] in unfinished_action_endings:
                raise EditorError("Заголовок карточки не называет объект действия")
        if re.search(r"(?:ПОКИНУЛ|ПОКИНУЛА|ОСТАВИЛ|ОСТАВИЛА) ПОСТ$", headline):
            raise EditorError("Заголовок карточки не уточняет, какой пост покинул герой")
        if category == "rumour" and "интерес" in draft_text.casefold():
            club_first_endings = ("ЗАИНТЕРЕСОВАН", "ЗАИНТЕРЕСОВАНА", "ЗАИНТЕРЕСОВАНЫ")
            if headline.endswith(club_first_endings):
                raise EditorError("В заголовке слуха игрок должен стоять перед заинтересованными клубами")
        if category == "failed_transfer":
            failed_markers = (
                "сорвал",
                "не состоится",
                "прекращены",
                "прекратил переговоры",
                "прекратила переговоры",
                "отказался от перехода",
                "отказалась от перехода",
                "сделка отменена",
            )
            if not any(marker in draft_text.casefold() for marker in failed_markers):
                raise EditorError(
                    "Стиль сорвавшегося трансфера требует прямого подтверждения отмены сделки"
                )
        failed_headline_markers = (
            "СОРВАЛ", "НЕ СОСТОИТСЯ", "ОТМЕНЕН", "ОТМЕНЁН", "ОТМЕНЕНА",
            "ОТМЕНЕНЫ", "ПРЕКРАЩЕНЫ", "ПРЕКРАЩЕНА",
        )
        if category != "failed_transfer" and any(
            marker in headline for marker in failed_headline_markers
        ):
            raise EditorError(
                "Стиль сорвавшегося трансфера требует прямого подтверждения отмены сделки"
            )
        if len(secondary.split()) > 4:
            raise EditorError("Подзаголовок карточки длиннее 4 слов")
        forbidden = ("—", "\n", "\r", '"', "«", "»")
        if any(value in headline or value in secondary for value in forbidden):
            raise EditorError("Надписи карточки содержат запрещённые символы")
        draft_folded = draft_text.casefold()
        headline_folded = headline.casefold()
        if " не " in f" {draft_folded} " and " не " not in f" {headline_folded} ":
            negation_sensitive_roots = (
                "отпуск",
                "прода",
                "покин",
                "уход",
                "подпис",
                "продл",
                "переход",
            )
            if any(
                root in draft_folded and root in headline_folded
                for root in negation_sensitive_roots
            ):
                raise EditorError("Заголовок карточки потерял смысловое отрицание")

        # A fluent sentence about another story is still invalid. Verify that
        # its entities, actions and numbers belong to this exact draft.
        draft_upper = draft_text.upper()
        headline_actions = [root for root in _CARD_ACTION_ROOTS if root in headline]
        for action in headline_actions:
            if action in draft_upper:
                continue
            family = next(
                (values for values in _CARD_ACTION_FAMILIES if action in values),
                (),
            )
            if family and any(value in draft_upper for value in family):
                continue
            raise EditorError("Действие в заголовке не подтверждается текстом поста")

        draft_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", draft_text))
        headline_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", headline))
        if not headline_numbers.issubset(draft_numbers):
            raise EditorError("В заголовке появилась цифра, которой нет в посте")

        # Small local models sometimes invent a wrong Russian case inside a
        # person's name (for example Рафаэля instead of source Рафаэла). A
        # concise card can always use the surname, so reject any fuzzy copy of
        # a source name whose spelling was changed.
        for source_phrase in _surface_name_phrases(draft_text):
            size = len(source_phrase)
            for start in range(0, len(words) - size + 1):
                candidate_phrase = tuple(words[start : start + size])
                if candidate_phrase == source_phrase:
                    continue
                if all(
                    SequenceMatcher(None, candidate, source).ratio() >= 0.78
                    for candidate, source in zip(candidate_phrase, source_phrase)
                ):
                    raise EditorError(
                        "В заголовке изменено написание имени или названия команды"
                    )

        draft_tokens = _latin_name(draft_text).split()
        headline_tokens = _latin_name(headline).split()
        action_tokens = {
            _latin_name(root)
            for root in _CARD_ACTION_ROOTS
            if root in headline
        }
        content_tokens = [
            token
            for token in headline_tokens
            if len(token) >= 3
            and token not in _CARD_SEMANTIC_STOPWORDS
            and not any(action.startswith(token[:5]) or token.startswith(action[:5]) for action in action_tokens)
        ]

        def token_matches(token: str) -> bool:
            return any(
                token[:5] == source[:5]
                or SequenceMatcher(None, token, source).ratio() >= 0.76
                for source in draft_tokens
                if len(source) >= 3
            )

        matched_content = sum(token_matches(token) for token in content_tokens)
        required_content = min(2, len(content_tokens))
        if matched_content < required_content:
            raise EditorError("Герой или клуб в заголовке не совпадает с текстом поста")
        return CardCopy(category, headline, secondary)

    def card_copy(self, draft_text: str) -> CardCopy:
        initial = self._generate_card_json(
            build_card_copy_prompt(draft_text),
            temperature=0.15,
        )
        try:
            return self._validate_card_copy(self._parse_card_copy(initial), draft_text)
        except EditorError as first_error:
            reviewed = self._generate_card_json(
                build_card_copy_review_prompt(draft_text, initial)
                + f"\n\nПРИЧИНА ОТКЛОНЕНИЯ: {first_error}",
                temperature=0.05,
            )
        try:
            return self._validate_card_copy(self._parse_card_copy(reviewed), draft_text)
        except EditorError as second_error:
            retried = self._generate_card_json(
                build_card_copy_retry_prompt(draft_text, reviewed, str(second_error)),
                temperature=0.0,
            )
            return self._validate_card_copy(self._parse_card_copy(retried), draft_text)

    def photo_subject(self, source_text: str) -> str:
        raw = self._generate(
            "Определи главный объект для поиска реальной фотографии к футбольной новости. "
            "Если есть конкретный человек, верни только его общепринятое полное имя. "
            "Если человека нет, верни клуб и слово stadium, например Liverpool FC stadium. "
            "Не добавляй пояснения, кавычки и знаки. Если визуальный объект определить "
            "невозможно, верни NONE.\n\n"
            + source_text[:3000],
            temperature=0.0,
        ).strip()
        value = re.sub(r"[^A-Za-zА-Яа-яЁё' -]", "", raw.splitlines()[0]).strip()
        if value.casefold() == "none" or not 1 <= len(value.split()) <= 7:
            raise EditorError("Не удалось определить героя для фото")
        return value
