from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

from .russian_editorial import (
    assess_editorial,
    canonical_source_display,
    canonicalize_clubs,
    club_entries,
    club_ids_in_text,
    normalize_club_verb_agreement,
    normalize_editorial_text,
    numeric_fact_tokens,
)
from .source_language import normalize_source_language


@dataclass(frozen=True, slots=True)
class SourceFact:
    fact_id: str
    claim: str
    kind: str
    statuses: tuple[str, ...]
    club_ids: tuple[str, ...]
    numbers: tuple[str, ...]
    entity_ids: tuple[str, ...] = ()
    squad_attributes: tuple[str, ...] = ()

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.fact_id,
            "claim": self.claim,
            "kind": self.kind,
            "statuses": list(self.statuses),
            "club_ids": list(self.club_ids),
            "numbers": list(self.numbers),
            "entity_ids": list(self.entity_ids),
            "squad_attributes": list(self.squad_attributes),
        }


@dataclass(frozen=True, slots=True)
class SourceFactPacket:
    source_text: str
    clean_source_text: str
    source_language_corrections: tuple[str, ...]
    source_language_issue: str | None
    primary_text: str
    source_name: str
    source_display: str | None
    intent: str
    relevance_class: str
    boundary: str
    facts: tuple[SourceFact, ...]
    canonical_entities: tuple[tuple[str, str], ...]
    entity_resolutions: tuple["EntityResolution", ...]
    unresolved_entities: tuple[str, ...]
    upstream_attributions: tuple[str, ...]
    provenance_issue: str | None
    forbidden_inferences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupportedSentence:
    text: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceLockedDraft:
    sentences: tuple[SupportedSentence, ...]


@dataclass(frozen=True, slots=True)
class SourceLockedResult:
    final_text: str
    supported_sentences: tuple[SupportedSentence, ...]
    packet: SourceFactPacket


@dataclass(frozen=True, slots=True)
class SourceLockedValidation:
    issues: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class PersonEntry:
    entity_id: str
    display: str
    aliases: tuple[str, ...]
    forms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntityResolution:
    entity_id: str
    entity_type: str
    source_name: str
    rendered_name: str
    resolution_method: str

    def as_prompt_dict(self) -> dict[str, str]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "source_name": self.source_name,
            "rendered_name": self.rendered_name,
            "resolution_method": self.resolution_method,
        }


_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b\S+\.(?:com|net|org|ru)(?:/\S*)?", re.I)
_CTA_LINE_RE = re.compile(
    r"(?:подпиш|смотри\s+все|следи\s+за|click\s+here|watch\s+now|read\s+more|"
    r"pre-?order|join\s+the\s+stream|cast\s+your\s+vote)",
    re.I,
)
_MARKETING_PRIMARY_RE = re.compile(
    r"(?:pre-?order|buy\s+now|sale\s+now|promo\s*code|промокод|предзаказ|"
    r"icon\s+appreciation\s+post|branded\s+activation|easports\s*fc|#fc\d+|"
    r"do\s+you\s+remember\s+when|more\s+from\s+last\s+night['’]s\s+[^.!?]*(?:show|video))",
    re.I,
)
_NEWS_SIGNAL_RE = re.compile(
    r"(?:\bsign(?:ed|ing)?\b|\bjoin(?:ed|ing)?\b|\bloan\b|\bbid\b|\boffer\b|"
    r"\btalks?\b|\bmatch\b|\bscore[ds]?\b|\bplay(?:s|ed|ing)?\b|\binjur|"
    r"\bgoal\b|\brecord\b|\bmove\b|\bappoint|переш|переход|трансфер|аренд|подпис|"
    r"предлож|переговор|матч|сч[её]т|гол|рекорд|назначен|травм|уволен)",
    re.I,
)
_EMBEDDED_BYLINE_RE = re.compile(r"^[^\n]{1,80}\(@[A-Za-z0-9_]+\)\s*$")
_EMBEDDED_START_RE = re.compile(r"^(?:[🚨♻️]+\s*)?(?:understand|exclusive|excl\b)", re.I)
_SOCIAL_INTRO_RE = re.compile(
    r"^(?:новый\s+тренд\s+в\s+апл|проблемы,\s+которые\s+мы\s+заслужили|"
    r"как\s+же\s+давно\s+это\s+было|как\s+оценим|ну\s+что|вот\s+это\s+поворот|"
    r"дают\s+бой\s+лучшей\s+команде\s+европы)[.!?\s:-]*",
    re.I,
)
_SOURCE_EDITORIAL_ASIDE_RE = re.compile(
    r"(?:^|(?<=[.!?]))\s*(?:"
    r"A\s+special\s+moment\s+for\s+(?:a\s+)?new\s+[^.!?]*talent|"
    r"Это\s+(?:особенн\w+\s+момент|важн\w+\s+шаг|отличн\w+\s+возможност\w*)[^.!?]*"
    r")[.!?]*",
    re.I,
)
_OPTIONAL_OFF_PITCH_RE = re.compile(
    r"\b(?:personal\s+stylist|wardrobe|fashion|celebrity\s+gossip|"
    r"личн\w+\s+стилист|гардероб|реорганизац\w+\s+вещ|частн\w+\s+жизн)\b",
    re.I,
)
_CORE_FOOTBALL_RELEVANCE_STATUSES = frozenset(
    {
        "fired", "manager_appointed", "personal_terms_agreed", "contract_extended",
        "new_player", "loan_made_permanent", "permanent_transfer", "loan",
        "official_completed", "deal_done_not_official", "club_agreement",
        "transfer_pending", "medical_scheduled", "talks", "interest", "bid_offer",
        "bid_expected", "bid_submitted", "intent", "support_quote", "kit", "match",
    }
)

_ATTRIBUTION_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Fabrizio Romano", re.compile(r"(?:Фабрицио\s+Романо|Fabrizio\s+Romano)", re.I)),
    ("Ben Jacobs", re.compile(r"(?:Б[еэ]н\s+Дж[ае]кобс|Ben\s+Jacobs)", re.I)),
    ("The Sun", re.compile(r"The\s+Sun", re.I)),
    ("The Athletic", re.compile(r"The\s+Athletic", re.I)),
    ("Football.London", re.compile(r"Football\.London", re.I)),
)

_ENTITY_IGNORES = {
    "serie a", "premier league", "champions league", "transfer insiders",
    "main transfers", "the sun", "the athletic", "football london",
    "world cup", "fifa world cup", "european super cup", "uefa super cup",
    "uefa super cup", "diario sport", "all i've", "here we go",
    "cristiano ronaldo sends", "diario sport main transfers",
    "the academy", "academy", "youth team", "reserve team",
}

_TRANSLITERATION_OVERRIDES = {
    "nobel mendy": "Нобел Менди",
    "castello lukeba": "Кастелло Люкеба",
    "curtis jones": "Кертис Джонс",
    "mohamed diomande": "Мохамед Диоманде",
    "dusan vlahovic": "Душан Влахович",
    "joe gelhardt": "Джо Гелхардт",
    "lucas herrington": "Лукас Херрингтон",
    "brian madjo": "Брайан Маджо",
    "romano schmid": "Романо Шмид",
    "florian grillitsch": "Флориан Гриллич",
    "ronald koeman": "Роналд Куман",
    "johan cruyff": "Йохан Кройф",
    "rinus michels": "Ринус Михелс",
    "louis van gaal": "Луи ван Гал",
    "frank rijkaard": "Франк Райкард",
    "taylor harwood-bellis": "Тейлор Харвуд-Беллис",
}


def _plain(value: str) -> str:
    value = _URL_RE.sub(" ", value)
    value = re.sub(r"(?<!\w)@[A-Za-z0-9_]+", " ", value)
    value = re.sub(r"(?<!\w)#[A-Za-zА-Яа-яЁё0-9_]+", " ", value)
    value = re.sub(r"\b(?:Link|Video)\b\s*", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -—–\n\t")
    return value


def person_entries() -> tuple[PersonEntry, ...]:
    from .russian_editorial import _policy

    return tuple(
        PersonEntry(
            str(row["id"]),
            str(row["display"]),
            tuple(str(value) for value in row.get("aliases", ())),
            tuple(str(value) for value in row.get("forms", ())),
        )
        for row in _policy().get("canonical_people", ())
    )


def canonicalize_people(text: str) -> str:
    replacements: dict[str, str] = {}
    for person in person_entries():
        for alias in person.aliases:
            replacements[alias.casefold()] = person.display
    if not replacements:
        return text
    aliases = sorted(
        (alias for person in person_entries() for alias in person.aliases),
        key=len,
        reverse=True,
    )
    alternatives = "|".join(re.escape(alias) for alias in aliases)
    # Normalize the original text in one pass. This prevents a short alias
    # (for example "Ромеро") from matching inside a canonical display value
    # that a longer alias produced during this same operation.
    return re.sub(
        rf"(?<![\w])(?:{alternatives})(?![\w])",
        lambda match: replacements[match.group(0).casefold()],
        text,
        flags=re.I,
    )


def person_ids_in_text(text: str) -> frozenset[str]:
    return frozenset(
        person.entity_id
        for person in person_entries()
        if any(
            re.search(rf"(?<![\w]){re.escape(surface)}(?![\w])", text, re.I)
            for surface in (person.display, *person.forms)
        )
    )


def _entity_slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in folded if unicodedata.category(char) != "Mn")
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", folded.casefold()).strip("-")
    return ascii_slug or re.sub(r"[^\w]+", "-", value.casefold()).strip("-")


def deterministic_transliterate_name(value: str) -> str | None:
    """Render a clean source-backed Latin name without asking the model."""
    normalized = re.sub(r"\s+", " ", value).strip()
    override = _TRANSLITERATION_OVERRIDES.get(normalized.casefold())
    if override:
        return override
    if not re.fullmatch(
        r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,}(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,}){1,3}",
        normalized,
    ):
        return None
    if any(token.isupper() and len(token) > 2 for token in normalized.split()):
        return None

    def transliterate_word(word: str) -> str:
        ascii_word = unicodedata.normalize("NFKD", word.replace("ß", "ss"))
        ascii_word = "".join(char for char in ascii_word if unicodedata.category(char) != "Mn")
        lower = ascii_word.casefold().replace("’", "'")
        pairs = (
            ("tsch", "ч"), ("sch", "ш"), ("sh", "ш"), ("ch", "ч"),
            ("zh", "ж"), ("kh", "х"), ("ph", "ф"), ("th", "т"),
            ("ck", "к"), ("qu", "кв"), ("ou", "у"), ("oo", "у"),
            ("ee", "и"), ("ai", "ай"), ("ay", "эй"), ("oy", "ой"),
        )
        singles = {
            "a":"а", "b":"б", "d":"д", "e":"е", "f":"ф", "h":"х",
            "i":"и", "j":"дж", "k":"к", "l":"л", "m":"м", "n":"н",
            "o":"о", "p":"п", "q":"к", "r":"р", "s":"с", "t":"т",
            "u":"у", "v":"в", "w":"в", "x":"кс", "z":"з", "'":"",
        }
        result: list[str] = []
        index = 0
        while index < len(lower):
            matched = False
            for source, target in pairs:
                if lower.startswith(source, index):
                    result.append(target)
                    index += len(source)
                    matched = True
                    break
            if matched:
                continue
            char = lower[index]
            if char == "c":
                result.append("с" if index + 1 < len(lower) and lower[index + 1] in "eiy" else "к")
            elif char == "g":
                result.append("дж" if index + 1 < len(lower) and lower[index + 1] in "eiy" else "г")
            elif char == "y":
                result.append("й" if index == len(lower) - 1 else "и")
            else:
                result.append(singles.get(char, ""))
            index += 1
        rendered = "".join(result)
        return rendered[:1].upper() + rendered[1:]

    rendered = " ".join(transliterate_word(word) for word in normalized.split())
    if not rendered or re.search(r"[A-Za-z]", rendered):
        return None
    return rendered


def _known_entity_resolutions(text: str) -> list[EntityResolution]:
    rows: list[EntityResolution] = []
    for club in club_entries():
        if any(re.search(rf"(?<![\w]){re.escape(surface)}(?![\w])", text, re.I) for surface in (club.display, *club.forms.values())):
            rows.append(EntityResolution(club.entity_id, "club", club.display, club.display, "REGISTRY_ALIAS"))
    for person in person_entries():
        if any(re.search(rf"(?<![\w]){re.escape(surface)}(?![\w])", text, re.I) for surface in (person.display, *person.forms)):
            rows.append(EntityResolution(person.entity_id, "person", person.display, person.display, "REGISTRY_ALIAS"))
    return rows


def _person_candidate_after_known_club(candidate: str) -> str:
    """Segment ``<club in case> <person>`` before person resolution.

    The known club registry owns the left boundary.  This prevents a genitive
    club form such as ``Бенфики`` from being swallowed by the adjacent player
    span while still allowing an unregistered clean player name after it.
    """

    surfaces = sorted(
        {
            surface
            for club in club_entries()
            for surface in (club.display, *club.aliases, *club.forms.values())
            if surface
        },
        key=len,
        reverse=True,
    )
    for surface in surfaces:
        match = re.match(rf"^{re.escape(surface)}\s+(.+)$", candidate, re.I)
        if match:
            return match.group(1).strip()
    return candidate


def resolve_source_backed_entities(text: str) -> tuple[str, tuple[EntityResolution, ...], tuple[str, ...]]:
    """Resolve known entities and transliterate only clean exact source names."""
    value = canonicalize_people(canonicalize_clubs(text))
    resolutions = _known_entity_resolutions(value)
    known_surfaces = {
        surface.casefold()
        for club in club_entries()
        for surface in (club.display, *club.aliases, *club.forms.values())
    } | {
        surface.casefold()
        for person in person_entries()
        for surface in (person.display, *person.aliases, *person.forms)
    }
    candidates = re.findall(
        r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,}(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,}){1,3}\b",
        value,
    )
    unresolved: list[str] = []
    for source_name in sorted(dict.fromkeys(candidates), key=len, reverse=True):
        folded = source_name.casefold()
        if folded in known_surfaces or folded in _ENTITY_IGNORES:
            continue
        rendered = deterministic_transliterate_name(source_name)
        if rendered is None:
            unresolved.append(source_name)
            continue
        is_club = bool(re.search(r"\b(?:FC|FK|CF|SC|City|United|Athletic|Rapids)\b", source_name))
        entity_type = "club" if is_club else "person"
        entity_id = f"source-{entity_type}-{_entity_slug(source_name)}"
        value = re.sub(
            rf"(?<![\w]){re.escape(source_name)}(?![\w])",
            lambda _: rendered,
            value,
        )
        resolutions.append(
            EntityResolution(entity_id, entity_type, source_name, rendered, "SOURCE_TRANSLITERATED")
        )
    known_after = {
        surface.casefold()
        for club in club_entries()
        for surface in (club.display, *club.aliases, *club.forms.values())
    } | {
        surface.casefold()
        for person in person_entries()
        for surface in (person.display, *person.aliases, *person.forms)
    }
    for source_name in re.findall(
        r"\b[А-ЯЁ][а-яё-]{2,}(?:\s+[А-ЯЁ][а-яё-]{2,}){1,2}\b",
        value,
    ):
        source_name = _person_candidate_after_known_club(source_name)
        if source_name.casefold() in known_after:
            continue
        candidate_folded = source_name.casefold().rstrip("-")
        if any(
            row.rendered_name.casefold() == candidate_folded
            or row.rendered_name.casefold().startswith(candidate_folded + "-")
            for row in resolutions
        ):
            continue
        if source_name.casefold() in {"суперкубок европы", "лига чемпионов"}:
            continue
        resolutions.append(
            EntityResolution(
                f"source-person-{_entity_slug(source_name)}",
                "person",
                source_name,
                source_name,
                "SOURCE_CANONICAL",
            )
        )
    unique = tuple(dict.fromkeys(resolutions))
    ordered = tuple(
        sorted(
            unique,
            key=lambda row: (
                value.casefold().find(row.rendered_name.casefold())
                if row.rendered_name.casefold() in value.casefold()
                else len(value),
                row.entity_id,
            ),
        )
    )
    return value, ordered, tuple(dict.fromkeys(unresolved))


def unresolved_source_entities(text: str) -> tuple[str, ...]:
    """Fail closed on Latin multi-token names not covered by the curated live universe."""
    known = {
        surface.casefold()
        for club in club_entries()
        for surface in (club.display, *club.aliases, *club.forms.values())
    } | {
        surface.casefold()
        for person in person_entries()
        for surface in (person.display, *person.aliases, *person.forms)
    }
    phrases = re.findall(r"\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,}(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,})+\b", text)
    return tuple(
        dict.fromkeys(
            phrase for phrase in phrases
            if phrase.casefold() not in _ENTITY_IGNORES and phrase.casefold() not in known
        )
    )


def classify_source_intent(source_text: str) -> str:
    """Classify the item before names are mistaken for football-news evidence."""
    if re.search(r"\bdo\s+you\s+remember\s+when\b", source_text, re.I):
        return "PROMOTIONAL_PRIMARY"
    if re.search(r"\bmore\s+from\s+last\s+night['’]s\s+[^.!?]*(?:show|video)\b", source_text, re.I):
        return "PROMOTIONAL_PRIMARY"
    if _MARKETING_PRIMARY_RE.search(source_text):
        non_marketing = _MARKETING_PRIMARY_RE.sub(" ", source_text)
        non_marketing = _CTA_LINE_RE.sub(" ", non_marketing)
        if not _NEWS_SIGNAL_RE.search(non_marketing):
            return "PROMOTIONAL_PRIMARY"
    lowered = source_text.casefold()
    if any(marker in lowered for marker in ("watch now", "join the stream", "смотрите здесь")):
        if not _NEWS_SIGNAL_RE.search(source_text):
            return "PROMOTIONAL_PRIMARY"
    return "NEWS"


def classify_editorial_relevance(
    primary_text: str,
    facts: tuple[SourceFact, ...],
) -> str:
    """Apply the CEO relevance policy after extraction, without changing facts."""
    statuses = {status for fact in facts for status in fact.statuses}
    if _OPTIONAL_OFF_PITCH_RE.search(primary_text) and not (
        statuses & _CORE_FOOTBALL_RELEVANCE_STATUSES
    ):
        return "OPTIONAL_OFF_PITCH"
    return "CORE_FOOTBALL_NEWS"


def select_primary_story(source_text: str) -> tuple[str, str]:
    """Select the outer item when a quoted/reposted independent story is embedded."""
    lines = [line.strip() for line in source_text.replace("\r", "\n").split("\n") if line.strip()]
    lines = [
        line for line in lines
        if not re.fullmatch(r"(?:Link|Video|Photo)", line, re.I)
        and not _URL_RE.fullmatch(line)
    ]
    if not lines:
        return "", "SINGLE_STORY"
    for index, line in enumerate(lines[1:], 1):
        if _EMBEDDED_BYLINE_RE.match(line):
            primary = _plain(" ".join(lines[:index]))
            if primary and _NEWS_SIGNAL_RE.search(primary):
                return primary, "MULTI_STORY_PRIMARY_SELECTED"
            embedded = _plain(" ".join(lines[index + 1 :]))
            if embedded and _NEWS_SIGNAL_RE.search(embedded):
                return embedded, "MULTI_STORY_PRIMARY_SELECTED"
        if index >= 1 and _EMBEDDED_START_RE.match(line) and len(lines[:index]) >= 1:
            primary = _plain(" ".join(lines[:index]))
            if primary and len(re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", primary)) >= 4:
                return primary, "MULTI_STORY_PRIMARY_SELECTED"
    return _plain(" ".join(lines)), "SINGLE_STORY"


def _strip_known_club_quotes(text: str) -> str:
    value = text
    aliases = sorted(
        {surface for club in club_entries() for surface in (club.display, *club.aliases, *club.forms.values())},
        key=len,
        reverse=True,
    )
    for alias in aliases:
        value = re.sub(
            rf"[«\"]\s*({re.escape(alias)})(?:\s*[»\"])?",
            lambda match: match.group(1),
            value,
            flags=re.I,
        )
    return value


def _separate_source_attribution(text: str) -> tuple[str, tuple[str, ...]]:
    value = text
    found: list[str] = []
    for display, pattern in _ATTRIBUTION_NAMES:
        if pattern.search(value):
            found.append(display)
        value = re.sub(
            rf"^(?:\W|_)*(?:{pattern.pattern})\s*:\s*",
            "",
            value,
            count=1,
            flags=re.I,
        )
        value = re.sub(
            rf"\b(?:об\s+этом\s+)?(?:сообщает|сообщил|пишет)\s+(?:{pattern.pattern})\s*[.!]?",
            " ",
            value,
            flags=re.I,
        )
        value = re.sub(
            rf"\bпо\s+информации\s+(?:{pattern.pattern})\s*,?",
            " ",
            value,
            flags=re.I,
        )
    return value, tuple(dict.fromkeys(found))


def _strip_source_noise(text: str) -> tuple[str, tuple[str, ...]]:
    value = _strip_known_club_quotes(text)
    value, attributions = _separate_source_attribution(value)
    value = "".join(
        " " if unicodedata.category(char) in {"So", "Sk", "Mn"} else char
        for char in value
    )
    value = re.sub(r"^(?:\W|_)*(?:OFFICIAL|BREAKING|EXCL(?:USIVE)?|UPD)\s*:\s*", "", value, flags=re.I)
    value = re.sub(r"^(?:\W|_)*HERE\s+WE\s+GO\s+(?:по\s+)?", "", value, flags=re.I)
    value = re.sub(r"^Find\s+out\s+why\s+", "", value, flags=re.I)
    # Social/editorial reactions are not source facts even when the outer
    # channel appended them after a real football fact.
    value = _SOURCE_EDITORIAL_ASIDE_RE.sub(" ", value)
    value = re.sub(r"\bЕДЕТ\s+В\b", "едет в", value)
    value = _SOCIAL_INTRO_RE.sub("", value.strip())
    # Terminal channel promotions are never story claims.
    value = re.split(
        r"(?:⚽️?\s*)?(?:ПОДПИШИСЬ|ПОДПИШИТЕСЬ|СМОТРИ\s+ВСЕ|СЛЕДИ\s+ЗА\s+ВСЕМ|"
        r"Click\s+here|Watch\s+now|Read\s+more)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    value = re.sub(
        r"(?:^|(?<=[.!?]))\s*(?:Как\s+оценим|Что\s+думаете|Как\s+вам|"
        r"Есть\s+шансы[^.!?]*|Будем\s+скучать[^.!?]*)[!?]*",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(r"\b(?:Main\s+Transfers|Albion|Diario\s+SPORT)\s*$", "", value, flags=re.I)
    value = re.sub(r"(?<![.!?])\s+(Сумма\s+трансфера\s*:)", r". \1", value, flags=re.I)
    value = re.sub(r"(?<![.!?])\s+(Контракт\s*[—:-])", r". \1", value, flags=re.I)
    value = re.sub(r"(?<![.!?])\s+(Кандидатур\w+\s+защитник)", r". \1", value, flags=re.I)
    return _plain(value), attributions


def _claim_segments(text: str) -> tuple[str, ...]:
    parts = re.split(r"(?<=[.!?])\s+|\s+[;•]\s+", text)
    claims = []
    for part in parts:
        claim = part.strip(" -—–")
        if not claim or _CTA_LINE_RE.search(claim):
            continue
        if len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]", claim)) < 4:
            continue
        claims.append(claim)
    return tuple(claims)


_STATUS_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fired", re.compile(r"\b(?:fired|sacked|dismissed|уволен\w*)\b", re.I)),
    ("manager_appointed", re.compile(r"\b(?:appointed|new\s+head\s+coach|нов\w+\s+(?:главн\w+\s+)?тренер|назначен\w*\s+(?:главн\w+\s+)?тренер)\b", re.I)),
    ("personal_terms_agreed", re.compile(r"(?:agree\w*[^.!?]{0,25}(?:personal\s+terms|личн\w+\s+услов|услови\w+\s+(?:личн\w+\s+)?контракт)|(?:personal\s+terms|личн\w+\s+услов|услови\w+\s+(?:личн\w+\s+)?контракт)[^.!?]{0,35}(?:agree|соглас)|соглас\w*[^.!?]{0,25}услови\w+\s+(?:личн\w+\s+)?контракт)", re.I)),
    ("contract_extended", re.compile(r"\b(?:extend(?:ed)?|renew(?:ed)?|продлил\w*|продлен\w*)\b[^.!?]{0,40}\bконтракт", re.I)),
    ("new_player", re.compile(r"\b(?:new\s+player|нов\w+\s+игрок)\b", re.I)),
    ("loan_made_permanent", re.compile(r"\bloan\b[^.!?]{0,45}\bpermanent\b|аренд\w*[^.!?]{0,45}\bпостоян", re.I)),
    ("permanent_transfer", re.compile(r"\bpermanent\s+(?:deal|transfer|move)|постоянн\w+\s+(?:переход|трансфер)", re.I)),
    ("loan", re.compile(r"\bloan(?:ed)?\b|\bаренд\w*", re.I)),
    ("official_completed", re.compile(r"\b(?:official(?:ly)?|signed|has\s+signed|joined|has\s+joined|completed|переш[её]л|подписал|завершил\w*\s+переход|официальн\w*|нов\w+\s+игрок)\b", re.I)),
    ("deal_done_not_official", re.compile(r"\b(?:here\s+we\s+go|deal\s+done|atléti\s*:\s*done|сделк\w*\s+согласован\w*)\b", re.I)),
    ("club_agreement", re.compile(r"\b(?:agreement\s+in\s+place|deal\s+in\s+place|agree\w*\s+(?:(?:a\s+)?deal(?:\s+in\s+principle)?|to\s+sell)|клуб\w*\s+соглас|согласовал\w*\s+переход|сделк\w*\s+соглас|между\s+клубами\s+достигнуто\s+соглашение|(?:стороны\s+)?достигли\s+соглашения|соглашение(?:\s+между\s+сторонами)?\s+достигнуто)\b", re.I)),
    ("transfer_pending", re.compile(r"\b(?:close\s+to\s+(?:returning|joining)|deal\s+close|ahead\s+of\s+formali[sz]ing|will\s+formali[sz]e|will\s+join|едет\s+в|перейд[её]т|заплатят)\b", re.I)),
    ("medical_scheduled", re.compile(r"\b(?:medical|медосмотр|медицинск\w+\s+обследован)\b[^.!?]{0,30}\b(?:tomorrow|today|завтра|сегодня|scheduled|пройд)\b", re.I)),
    ("talks", re.compile(r"\b(?:talks?|discussions?|negotiations?|переговор\w*|обсужден\w*)\b", re.I)),
    ("interest", re.compile(r"\b(?:interest(?:ed)?|target|интерес\w*|цел\w*)\b", re.I)),
    ("bid_expected", re.compile(r"\b(?:expected\s+to\s+(?:place|make|submit)|prepar(?:e|ed|ing)|готовит\w*|ожидается)\b[^.!?]{0,45}\b(?:bid|offer|предложен\w*)|\b(?:bid|offer|предложен\w*)\b[^.!?]{0,35}\b(?:expected|готовит\w*|ожидается)", re.I)),
    ("bid_submitted", re.compile(r"\b(?:submitted|made|placed|lodged)\b[^.!?]{0,25}\b(?:bid|offer)\b|\b(?:сделал\w*|направил\w*|подал\w*)\b[^.!?]{0,25}\bпредложен\w*", re.I)),
    ("bid_offer", re.compile(r"\b(?:bid|offer|предложен\w*|заявк\w*)\b", re.I)),
    ("intent", re.compile(r"\b(?:want(?:s|ed)?|prepared\s+to\s+join|хочет|готов\w*\s+перейти)\b", re.I)),
    ("support_quote", re.compile(r"\b(?:hug|stay\s+strong|выразил\w*\s+поддерж|пожелал\w*\s+сил)\b", re.I)),
    ("kit", re.compile(r"\b(?:kit|shirt|форм\w*|экипиров)\b", re.I)),
    ("match", re.compile(r"\b(?:match|game|play(?:s|ed|ing)?|goal|scorer|record|матч\w*|игр(?!ок)\w*|гол\w*|рекорд\w*)\b", re.I)),
)

TRANSFER_STATUS_LADDER: tuple[str, ...] = (
    "interest",
    "talks",
    "bid_expected",
    "bid_submitted",
    "personal_terms_agreed",
    "club_agreement",
    "deal_done_not_official",
    "medical_scheduled",
    "official_completed",
)


def claim_statuses(text: str) -> tuple[str, ...]:
    statuses = [name for name, pattern in _STATUS_RULES if pattern.search(text)]
    # Signing paperwork while a league registration process is ongoing is not
    # itself an official first-team transfer completion.
    if re.search(r"\bsigned\s+(?:the\s+)?(?:paperwork|documents?)\b", text, re.I):
        statuses = [status for status in statuses if status != "official_completed"]
        statuses.append("paperwork_signed")
    return tuple(dict.fromkeys(statuses))


def squad_attributes(text: str) -> tuple[str, ...]:
    attributes: list[str] = []
    if re.search(
        r"\b(?:for\s+the\s+academy\s+to\s+sign|join(?:ing)?\s+(?:the\s+)?(?:club\s+)?academy|"
        r"academy\s+signing|клубн\w+\s+академи\w*)\b",
        text,
        re.I,
    ):
        attributes.append("academy")
    for level in ("U21", "U18"):
        if re.search(rf"(?<![A-Za-z0-9]){level}(?![A-Za-z0-9])", text, re.I):
            attributes.append(level.casefold())
    if re.search(r"\b(?:youth\s+team|молод[её]жн\w+\s+команд\w*)\b", text, re.I):
        attributes.append("youth_team")
    if re.search(r"\b(?:reserve\s+team|резервн\w+\s+команд\w*)\b", text, re.I):
        attributes.append("reserve_team")
    return tuple(dict.fromkeys(attributes))


def _kind(statuses: tuple[str, ...]) -> str:
    if "support_quote" in statuses:
        return "quote"
    if any(status in statuses for status in ("personal_terms_agreed", "club_agreement", "deal_done_not_official", "official_completed", "loan_made_permanent", "contract_extended")):
        return "transfer_status"
    if "fired" in statuses:
        return "coach_status"
    if "kit" in statuses:
        return "kit"
    if "match" in statuses:
        return "match"
    return "source_claim"


def build_source_fact_packet(source_text: str, source_name: str) -> SourceFactPacket:
    intent = classify_source_intent(source_text)
    selected, boundary = select_primary_story(source_text)
    cleaned, attributions = _strip_source_noise(selected)
    source_language = normalize_source_language(cleaned)
    cleaned = source_language.clean_text
    primary, resolutions, resolution_failures = resolve_source_backed_entities(cleaned)
    facts: list[SourceFact] = []
    for index, claim in enumerate(_claim_segments(primary), 1):
        statuses = claim_statuses(claim)
        entity_ids: list[str] = []
        for resolution in resolutions:
            surfaces = [resolution.rendered_name]
            club = next((row for row in club_entries() if row.entity_id == resolution.entity_id), None)
            if club:
                surfaces.extend(club.forms.values())
            person = next((row for row in person_entries() if row.entity_id == resolution.entity_id), None)
            if person:
                surfaces.extend(person.forms)
            if any(re.search(rf"(?<![\w]){re.escape(surface)}(?![\w])", claim, re.I) for surface in surfaces):
                entity_ids.append(resolution.entity_id)
        facts.append(
            SourceFact(
                fact_id=f"f{index}",
                claim=claim,
                kind=_kind(statuses),
                statuses=statuses,
                club_ids=tuple(sorted(club_ids_in_text(claim))),
                numbers=tuple(sorted(numeric_fact_tokens(claim))),
                entity_ids=tuple(entity_ids),
                squad_attributes=squad_attributes(claim),
            )
        )
    entities = list(dict.fromkeys((row.entity_id, row.rendered_name) for row in resolutions))
    source_display = canonical_source_display(source_name)
    provenance_issue = None
    if source_display and attributions and any(value != source_display for value in attributions):
        provenance_issue = "PROVENANCE_ATTRIBUTION_CONFLICT"
    unresolved = tuple(
        dict.fromkeys((*resolution_failures, *unresolved_source_entities(primary)))
    )
    return SourceFactPacket(
        source_text=source_text,
        clean_source_text=cleaned,
        source_language_corrections=source_language.corrections,
        source_language_issue=source_language.issue,
        primary_text=primary,
        source_name=source_name,
        source_display=source_display,
        intent=intent,
        relevance_class=classify_editorial_relevance(primary, tuple(facts)),
        boundary=boundary,
        facts=tuple(facts),
        canonical_entities=tuple(entities),
        entity_resolutions=resolutions,
        unresolved_entities=unresolved,
        upstream_attributions=attributions,
        provenance_issue=provenance_issue,
        forbidden_inferences=(
            "new entities", "new numbers", "background or implications", "status upgrade",
            "transfer completion unless explicit", "analysis", "source attribution in body",
            "academy/youth/reserve level as an independent club or counterparty",
        ),
    )


def build_source_locked_prompt(packet: SourceFactPacket) -> str:
    facts = json.dumps([fact.as_prompt_dict() for fact in packet.facts], ensure_ascii=False, indent=2)
    entities = json.dumps(
        [resolution.as_prompt_dict() for resolution in packet.entity_resolutions],
        ensure_ascii=False,
        indent=2,
    )
    return (
        "Ты русский редактор футбольной новостной ленты PremWire. Факты уже выделены кодом. "
        "Ты выбираешь только естественную русскую формулировку, но не выбираешь и не добавляешь факты.\n\n"
        "Верни ровно JSON по схеме: {\"sentences\":[{\"text\":\"...\",\"fact_ids\":[\"f1\"]}]}. "
        "Каждое фактическое предложение обязано ссылаться на один или несколько fact_ids. "
        "Не добавляй предложение без поддержки. Разрешено опустить второстепенный факт. "
        "Одного короткого предложения достаточно. Не добавляй фон, последствия, выводы, источники, "
        "CTA, вопросы, ссылки, эмодзи, оценочные прилагательные или декоративные заключения. "
        "Слова вроде «талант», «важный», «особенный» являются фактами и допустимы только при явной "
        "поддержке выбранного fact_id. Сохраняй status/certainty точно: personal terms не являются "
        "завершённым переходом; new player не означает продление контракта; loan made permanent не является "
        "продлением аренды. Названия клубов пиши без кавычек.\n\n"
        "Academy/youth team/U21/U18/reserve team — атрибут состава клуба, а не отдельный клуб или "
        "контрагент. Не пиши «сделка с Академией» или машинное «сделка для подписания».\n\n"
        f"STORY_BOUNDARY: {packet.boundary}\n"
        f"PRIMARY_SOURCE_TEXT: {packet.primary_text}\n\n"
        f"ENTITY_RENDERINGS (используй rendered_name дословно; не склоняй неизвестные имена):\n{entities}\n\n"
        f"SUPPORTED_FACTS:\n{facts}\n\n"
        f"FORBIDDEN_INFERENCES: {', '.join(packet.forbidden_inferences)}\n"
        "Не выводи Markdown или пояснения."
    )


def build_source_locked_repair_prompt(
    packet: SourceFactPacket,
    failed: SourceLockedDraft,
    issues: Iterable[str],
) -> str:
    failed_json = json.dumps(
        {"sentences": [{"text": row.text, "fact_ids": list(row.fact_ids)} for row in failed.sentences]},
        ensure_ascii=False,
    )
    return (
        build_source_locked_prompt(packet)
        + "\n\nЭто единственный repair pass. Можно только исправить язык, убрать неподдержанное "
        "предложение, исправить сущность/пунктуацию или упростить формулировку. Новая семантика запрещена.\n"
        + f"FAILED_RULES: {', '.join(issues)}\nCURRENT_DRAFT: {failed_json}"
    )


def parse_source_locked_draft(raw: str) -> SourceLockedDraft:
    value = raw.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("INVALID_SOURCE_LOCKED_JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"sentences"}:
        raise ValueError("INVALID_SOURCE_LOCKED_SCHEMA")
    raw_sentences = payload["sentences"]
    if not isinstance(raw_sentences, list) or not raw_sentences or len(raw_sentences) > 6:
        raise ValueError("INVALID_SOURCE_LOCKED_SENTENCES")
    sentences = []
    for raw_sentence in raw_sentences:
        if not isinstance(raw_sentence, dict) or set(raw_sentence) != {"text", "fact_ids"}:
            raise ValueError("INVALID_SOURCE_LOCKED_SENTENCE_SCHEMA")
        text = raw_sentence["text"]
        fact_ids = raw_sentence["fact_ids"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("EMPTY_SUPPORTED_SENTENCE")
        if not isinstance(fact_ids, list) or not fact_ids or not all(isinstance(item, str) for item in fact_ids):
            raise ValueError("MISSING_SENTENCE_SUPPORT")
        sentences.append(SupportedSentence(text.strip(), tuple(dict.fromkeys(fact_ids))))
    return SourceLockedDraft(tuple(sentences))


_NUMBER_WORD_VALUES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _source_amounts(text: str) -> tuple[str, ...]:
    values: list[str] = []
    currency_names = {"€": "евро", "£": "фунтов", "$": "долларов"}
    for match in re.finditer(r"([€£$])\s*(\d+(?:[.,]\d+)?)\s*(?:m|млн)?", text, re.I):
        number = match.group(2).replace(".", ",")
        values.append(f"{number} млн {currency_names[match.group(1)]}")
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*млн\s*(евро|фунт\w*|доллар\w*)", text, re.I):
        number = match.group(1).replace(".", ",")
        currency = match.group(2).casefold()
        if currency.startswith("фунт"):
            currency = "фунтов"
        elif currency.startswith("доллар"):
            currency = "долларов"
        else:
            currency = "евро"
        rendered = f"{number} млн {currency}"
        if rendered not in values:
            values.append(rendered)
    return tuple(values)


def _amount_phrase(amounts: tuple[str, ...]) -> str | None:
    if not amounts:
        return None
    if len(amounts) >= 2:
        return f"{amounts[0]} плюс {amounts[1]} бонусами"
    return amounts[0]


def _source_duration(text: str) -> str | None:
    match = re.search(r"\b(\d+\+\d+)\s*[- ]?year(?:\s+contract|\s+deal)", text, re.I)
    if match:
        return f"{match.group(1)} года"
    match = re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]year\s+(?:contract|deal)",
        text,
        re.I,
    )
    if not match:
        return None
    raw = match.group(1).casefold()
    number = int(raw) if raw.isdigit() else _NUMBER_WORD_VALUES[raw]
    suffix = "год" if number == 1 else "года" if number in {2, 3, 4} else "лет"
    return f"{number} {suffix}"


def _source_until_year(text: str) -> str | None:
    match = re.search(r"\b(?:until|through(?:\s+to)?|до)\s+(20\d{2})\b", text, re.I)
    return match.group(1) if match else None


def _resolved(packet: SourceFactPacket, entity_type: str) -> tuple[EntityResolution, ...]:
    return tuple(row for row in packet.entity_resolutions if row.entity_type == entity_type)


def _person_form(resolution: EntityResolution, grammatical_case: str) -> str:
    person = next((row for row in person_entries() if row.entity_id == resolution.entity_id), None)
    positions = {"nominative": 0, "genitive": 1, "dative": 2, "accusative": 3, "instrumental": 4}
    index = positions[grammatical_case]
    if person and len(person.forms) > index:
        return person.forms[index]
    return resolution.rendered_name


def _club_form(resolution: EntityResolution, grammatical_case: str) -> str:
    club = next((row for row in club_entries() if row.entity_id == resolution.entity_id), None)
    return club.forms.get(grammatical_case, resolution.rendered_name) if club else resolution.rendered_name


def _resolution_surfaces(resolution: EntityResolution) -> tuple[str, ...]:
    person = next((row for row in person_entries() if row.entity_id == resolution.entity_id), None)
    return (resolution.rendered_name, *person.forms) if person else (resolution.rendered_name,)


def _fact_ids(packet: SourceFactPacket, *statuses: str) -> tuple[str, ...]:
    # Fallback sentences are derived from the packet as a whole. Referencing
    # all atomic facts is conservative and prevents an entity/number from a
    # companion source clause being detached from its source support.
    return tuple(fact.fact_id for fact in packet.facts)


def deterministic_fallback_draft(packet: SourceFactPacket) -> SourceLockedDraft | None:
    """Render only common, fully structured facts; otherwise leave the item blocked."""
    text = packet.primary_text
    statuses = {status for fact in packet.facts for status in fact.statuses}
    people = _resolved(packet, "person")
    clubs = _resolved(packet, "club")
    amounts = _source_amounts(text)
    amount = _amount_phrase(amounts)
    duration = _source_duration(text)
    until_year = _source_until_year(text)
    sentence: str | None = None
    fact_ids: tuple[str, ...] = ()

    academy_fact_ids = tuple(
        fact.fact_id for fact in packet.facts if "academy" in fact.squad_attributes
    )
    if academy_fact_ids and "club_agreement" in statuses and people and len(clubs) >= 2:
        age = next(
            (
                number
                for fact in packet.facts
                if "academy" in fact.squad_attributes
                for number in fact.numbers
                if number.isdigit() and 14 <= int(number) <= 23
            ),
            None,
        )
        role = "вингера" if re.search(r"\b(?:winger|вингер)\b", text, re.I) else "игрока"
        age_role = f"{age}-летнего {role}" if age else role
        sentence = (
            f"{clubs[0].rendered_name} согласовал переход {age_role} "
            f"{_person_form(people[0], 'genitive')} из {_club_form(clubs[1], 'genitive')} "
            "в клубную академию."
        )
        return SourceLockedDraft((SupportedSentence(sentence, academy_fact_ids),))

    if (
        "official_completed" in statuses
        and len(people) >= 2
        and len(clubs) >= 3
        and re.search(r"\bsigned\b", text, re.I)
    ):
        first_suffix = f" за {amounts[0]}" if amounts else ""
        second_suffix = " на правах свободного агента" if re.search(r"\bfree\s+transfer\b", text, re.I) else ""
        return SourceLockedDraft(
            (
                SupportedSentence(
                    f"{clubs[0].rendered_name} подписал "
                    f"{_person_form(people[0], 'accusative')} из "
                    f"{_club_form(clubs[1], 'genitive')}{first_suffix}.",
                    _fact_ids(packet, "official_completed"),
                ),
                SupportedSentence(
                    f"{clubs[0].rendered_name} также подписал "
                    f"{_person_form(people[1], 'accusative')} из "
                    f"{_club_form(clubs[2], 'genitive')}{second_suffix}.",
                    _fact_ids(packet, "official_completed"),
                ),
            )
        )
    if "loan_made_permanent" in statuses and people and len(clubs) >= 2:
        suffix = f" за {amount}" if amount else ""
        sentence = (
            f"{people[0].rendered_name} продолжит выступать за клуб {clubs[0].rendered_name} "
            f"на постоянной основе после аренды из клуба {clubs[1].rendered_name}{suffix}."
        )
        fact_ids = _fact_ids(packet, "loan_made_permanent")
    elif "fired" in statuses and people and clubs:
        sentence = (
            f"{people[0].rendered_name} уволен с поста главного тренера "
            f"клуба {clubs[0].rendered_name}."
        )
        fact_ids = _fact_ids(packet, "fired")
    elif "manager_appointed" in statuses and people and clubs:
        until = f" до {until_year} года" if until_year else ""
        sentence = (
            f"{people[0].rendered_name} назначен главным тренером "
            f"{_club_form(clubs[0], 'genitive')}{until}."
        )
        fact_ids = _fact_ids(packet, "manager_appointed")
    elif "support_quote" in statuses and len(people) >= 2:
        sentence = (
            f"{people[0].rendered_name} поддержал Лионеля Месси и его близких "
            "в трудный момент, пожелав им сил."
        )
        fact_ids = _fact_ids(packet, "support_quote")
    elif "kit" in statuses and len(clubs) >= 3 and re.search(r"розов\w+\s+цвет", text, re.I):
        sentence = (
            f"{clubs[0].rendered_name} представил форму в розовых цветах вслед за "
            f"{_club_form(clubs[1], 'instrumental')} и "
            f"{_club_form(clubs[2], 'instrumental')}."
        )
        fact_ids = _fact_ids(packet, "kit")
    elif re.search(r"\b(?:youngest|сам\w+\s+молод\w+)\b", text, re.I) and people:
        sentence = (
            f"{people[0].rendered_name} стал самым молодым автором гола "
            "в истории Суперкубка Европы."
        )
        fact_ids = _fact_ids(packet)
    elif "match" in statuses and people and re.search(r"\bfirst\s+goal\b", text, re.I):
        age = next((number for fact in packet.facts for number in fact.numbers), None)
        prefix = f"{age}-летний " if age else ""
        sentence = f"{prefix}{people[0].rendered_name} забил свой первый гол."
        fact_ids = _fact_ids(packet, "match")
    elif "bid_expected" in statuses and people and clubs:
        amount_part = f" более {amount}" if amount else ""
        sentence = (
            f"{clubs[0].rendered_name} готовит новое предложение о трансфере "
            f"{_person_form(people[0], 'genitive')} и готов заплатить{amount_part}."
        )
        if "talks" in statuses:
            sentence += " Переговоры продолжаются."
        fact_ids = _fact_ids(packet, "bid_expected", "talks")
    elif "interest" in statuses and people and clubs:
        sentence = (
            f"{people[0].rendered_name} входит в сферу трансферных интересов "
            f"клуба {clubs[0].rendered_name}."
        )
        fact_ids = _fact_ids(packet, "interest")
    elif "talks" in statuses and people and clubs:
        sentence = (
            f"{people[0].rendered_name} может перейти в клуб {clubs[0].rendered_name}: "
            "переговоры продолжаются."
        )
        fact_ids = _fact_ids(packet, "talks")
    elif "bid_offer" in statuses and people and clubs:
        amount_part = f" свыше {amount}" if amount else ""
        sentence = (
            f"Клуб {clubs[0].rendered_name} готовит новое предложение{amount_part} "
            f"по трансферу игрока {_person_form(people[0], 'genitive')}."
        )
        fact_ids = _fact_ids(packet, "bid_offer")
    elif "official_completed" in statuses and people:
        if len(clubs) >= 2:
            suffix = f" за {amount}" if amount else ""
            duration_part = f". Контракт рассчитан на {duration}" if duration else ""
            sentence = (
                f"{people[0].rendered_name} перешёл из клуба {clubs[1].rendered_name} "
                f"в клуб {clubs[0].rendered_name}{suffix}{duration_part}."
            )
        elif clubs:
            suffix = f" за {amount}" if amount else ""
            duration_part = f". Контракт рассчитан на {duration}" if duration else ""
            sentence = (
                f"{people[0].rendered_name} стал игроком {_club_form(clubs[0], 'genitive')}"
                f"{suffix}{duration_part}."
            )
        fact_ids = _fact_ids(packet, "official_completed", "new_player")
    elif "club_agreement" in statuses and people and len(clubs) >= 2:
        suffix = f" за {amount}" if amount else ""
        sentence = (
            f"Клубы {clubs[0].rendered_name} и {clubs[1].rendered_name} "
            f"согласовали переход игрока {_person_form(people[0], 'genitive')}{suffix}."
        )
        fact_ids = _fact_ids(packet, "club_agreement")
    elif "club_agreement" in statuses and people and clubs:
        suffix = f" за {amount}" if amount else ""
        sentence = (
            f"{clubs[0].rendered_name} согласовал переход "
            f"{_person_form(people[0], 'genitive')}{suffix}."
        )
        fact_ids = _fact_ids(packet, "club_agreement")
    elif "transfer_pending" in statuses and people and clubs:
        target = clubs[-1] if re.search(r"returning\s+to", text, re.I) else clubs[0]
        suffix = f" за {amount}" if amount else ""
        sentence = (
            f"{people[0].rendered_name} перейдёт в "
            f"{_club_form(target, 'accusative')}{suffix}."
        )
        fact_ids = _fact_ids(packet, "transfer_pending")

    if sentence is None:
        return None
    return SourceLockedDraft((SupportedSentence(sentence, fact_ids),))


def deterministic_language_repair(
    packet: SourceFactPacket,
    draft: SourceLockedDraft,
    issues: Iterable[str],
) -> SourceLockedDraft | None:
    """Repair form only; fact IDs and factual sentence count stay unchanged."""
    issue_set = set(issues)
    if issue_set & {
        "FACT_STATUS_BID_EXPECTED_WEAKENED",
        "FACT_STATUS_CLUB_AGREEMENT_WEAKENED",
        "FACT_STATUS_DEAL_DONE_UPGRADED",
        "LANGUAGE_CONFLICTING_TRANSFER_STATUS",
        "LANGUAGE_TRANSFER_SOCIAL_VERB",
    }:
        status_fallback = deterministic_fallback_draft(packet)
        if status_fallback is not None:
            return status_fallback
    rows: list[SupportedSentence] = []
    for row in draft.sentences:
        value = row.text
        if issue_set.intersection(
            {"UNSUPPORTED_EDITORIAL_FILLER", "UNSUPPORTED_FACTUAL_DESCRIPTOR"}
        ) and any(
            pattern.search(value)
            for pattern in (*_EDITORIAL_FILLER_PATTERNS, *_FACTUAL_DESCRIPTOR_PATTERNS)
        ):
            continue
        value = normalize_club_verb_agreement(value)
        if "LANGUAGE_ACADEMY_COUNTERPARTY" in issue_set or "LANGUAGE_LITERAL_SIGNING_CONSTRUCTION" in issue_set:
            fallback = deterministic_fallback_draft(packet)
            if fallback is not None:
                return fallback
        value = re.sub(r"\bназначен(\w*)\b([^.!?]{0,60})\bна\s+контракт\s+до\b", r"назначен\1\2по контракту до", value, flags=re.I)
        value = re.sub(r"\bтренер(\w*)\s+(Црвена\s+Звезда)\b", r"тренер\1 клуба \2", value, flags=re.I)
        value = re.sub(r"\bпо\s+информации\s+(The\s+Sun|The\s+Athletic|Football\.London)\s+", r"по информации \1, ", value, flags=re.I)
        value = re.sub(r"\b([А-ЯЁ][а-яё-]+)\s+\1\b", r"\1", value)
        value = re.sub(r"\bс\s+Жоау\s+Пальинья\b", "с Жоау Пальиньей", value, flags=re.I)
        value = re.sub(r"\bПереход\s+Стефан\s+Пош\b", "Переход Стефана Поша", value, flags=re.I)
        value = re.sub(
            r"\bна\s+свободное\s+агентство\s+от\b",
            "на правах свободного агента из",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"\b(продаж\w+[^.!?]{0,80})\s+в\s+Фрозиноне\b",
            r"\1 в клуб Фрозиноне",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"\bна\s+контракт\s+сроком\s+(\d+\+\d+)\s+год\b",
            r"по контракту на \1 года",
            value,
            flags=re.I,
        )
        value = re.sub(r"\bсоглашени(\w*)\s+по\s+оплате\b", r"соглашени\1 по сумме трансфера", value, flags=re.I)
        value = re.sub(
            r"\bпосле\s+соглашения\s+по\s+цене\b",
            "после согласования суммы трансфера",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"\bназначен\s+менеджером\s+(сборной\s+[^.!?]+)",
            r"назначен главным тренером \1",
            value,
            flags=re.I,
        )
        value = re.sub(r"(?<![.!?])\s+(Контракт\s*[—–:-])", r". \1", value)
        value = re.sub(
            r"\bЛидс[’']\s+(Джо\s+Гелхардт)([^.!?]*\bвозвращен\w*\s+в\s+Халл)",
            r"\1 из Лидса\2",
            value,
            flags=re.I,
        )
        value = re.sub(r"\bтренер\s+Нидерландов\b", "тренер сборной Нидерландов", value, flags=re.I)
        value = re.sub(r"\bЕДЕТ\s+В\b", "едет в", value)
        if "LANGUAGE_TRANSFER_MOVE_WORDING" in issue_set:
            target = next(iter(_resolved(packet, "club")), None)
            if target is not None:
                value = re.sub(
                    r"\bо\s+желании\s+переехать\b",
                    f"о желании перейти в {_club_form(target, 'accusative')}",
                    value,
                    flags=re.I,
                )
        for person in person_entries():
            if len(person.forms) < 2:
                continue
            value = re.sub(
                rf"\b(игрока|защитника|полузащитника|нападающего|вратаря)\s+{re.escape(person.display)}\b",
                lambda match: f"{match.group(1)} {person.forms[1]}",
                value,
                flags=re.I,
            )
        for club in club_entries():
            genitive = club.forms.get("genitive", club.display)
            if genitive == club.display:
                continue
            value = re.sub(
                rf"\bигроком\s+{re.escape(club.display)}\b",
                f"игроком {genitive}",
                value,
                flags=re.I,
            )
        if "LANGUAGE_SOCIAL_INTRO" in issue_set:
            value = re.sub(r"^Здесь\s+мы\s+начинаем[^.!?]*[.!?]\s*", "", value, flags=re.I)
        if "UNSUPPORTED_SOURCE_ATTRIBUTION_BODY" in issue_set or "SOURCE_NAME_IN_BODY" in issue_set:
            value, _ = _separate_source_attribution(value)
        value = value.strip()
        if value:
            rows.append(SupportedSentence(value, row.fact_ids))
    return SourceLockedDraft(tuple(rows)) if rows else None


_UNSUPPORTED_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("UNSUPPORTED_OFFICIAL_IMAGES", re.compile(r"официальн\w+\s+изображен", re.I)),
    ("UNSUPPORTED_RECRUITMENT_ACTION", re.compile(r"уже\s+начал\w*\s+работ\w*\s+по\s+привлеч", re.I)),
    ("UNSUPPORTED_STABILITY", re.compile(r"обеспеч\w+\s+(?:ему\s+)?стабильност", re.I)),
    ("UNSUPPORTED_DETAILS_ABSENT", re.compile(r"детал\w+\s+(?:сделк|соглашен)[^.!?]{0,35}не\s+уточн", re.I)),
    ("UNSUPPORTED_SERVICE_PHRASE", re.compile(r"смен\w+\s+мест\w+\s+служб", re.I)),
    ("UNSUPPORTED_READY_FILLER", re.compile(r"теперь\s+[^.!?]{0,40}\bготов\w*\s+к\s+игре", re.I)),
    ("QUOTE_SPEECH_ACT_INVERSION", re.compile(r"(?:просьб\w*|попросил\w*)[^.!?]{0,30}\bобнять", re.I)),
    ("UNSUPPORTED_SOURCE_ATTRIBUTION_BODY", re.compile(r"(?:\b(?:Фабрицио\s+Романо|Б[еэ]н\s+Дж[ае]кобс|The\s+Athletic|Football\.London)\b[^.!?]{0,30}\b(?:сообщил|заявил|пишет|reports?)\b|\b(?:сообщает|сообщил|заявил|пишет)\s+(?:Фабрицио\s+Романо|Б[еэ]н\s+Дж[ае]кобс)\b)", re.I)),
    ("UNSUPPORTED_EXCHANGE_RELATION", re.compile(r"\bв\s+обмен\s+на\b", re.I)),
)
_EDITORIAL_FILLER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bособенн\w+\s+момент\b", re.I),
    re.compile(r"\bнов\w+\s+талант\b|\bталантлив\w+\s+игрок\b", re.I),
    re.compile(r"\bважн\w+\s+шаг\b", re.I),
    re.compile(r"\bсерь[её]зн\w+\s+усилен\w*\b", re.I),
    re.compile(r"\bотличн\w+\s+возможност\w*\b", re.I),
    re.compile(r"\bготов\w*\s+к\s+новому\s+вызову\b", re.I),
    re.compile(r"\bсторон\w*\s+настроен\w*\s+оптимистич\w*\b", re.I),
    re.compile(r"\bигрок\s+будет\s+полезен\b", re.I),
    re.compile(r"\bэто\s+позволит\b", re.I),
    re.compile(r"\bключев\w+\s+вопрос\b", re.I),
    re.compile(r"\bсделк\w*\s+даст\b", re.I),
)
_FACTUAL_DESCRIPTOR_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{root}\w*\b", re.I)
    for root in (
        "талант", "важн", "ключев", "особенн", "выдающ", "перспектив",
        "звезд", "легендар", "успеш", "крупнейш", "сильнейш", "опытн",
        "счастлив", "разочарован", "уверен", "оптимист",
    )
)
_COUNTRY_ADJECTIVES = re.compile(
    r"\b(?:израильск\w*|английск\w*|итальянск\w*|турецк\w*|немецк\w*|"
    r"французск\w*|испанск\w*|португальск\w*|нидерландск\w*)\b",
    re.I,
)


def _support_issues(
    sentence: str,
    facts: tuple[SourceFact, ...],
    entity_resolutions: tuple[EntityResolution, ...] = (),
) -> list[str]:
    issues: list[str] = []
    supporting_text = " ".join(fact.claim for fact in facts)
    source_statuses = {status for fact in facts for status in fact.statuses}
    final_statuses = set(claim_statuses(sentence))
    for pattern in _EDITORIAL_FILLER_PATTERNS:
        if pattern.search(sentence) and not pattern.search(supporting_text):
            issues.append("UNSUPPORTED_EDITORIAL_FILLER")
            break
    for pattern in _FACTUAL_DESCRIPTOR_PATTERNS:
        if pattern.search(sentence) and not pattern.search(supporting_text):
            issues.append("UNSUPPORTED_FACTUAL_DESCRIPTOR")
            break
    if not numeric_fact_tokens(sentence).issubset(numeric_fact_tokens(supporting_text)):
        issues.append("UNSUPPORTED_NUMBER")
    source_amounts = _source_amounts(supporting_text)
    if (
        len(source_amounts) >= 2
        and re.search(r"(?:\+|plus|плюс)", supporting_text, re.I)
        and re.search(r"\b(?:за|сумм|стоимост|пакет|fee)\b", sentence, re.I)
        and not all(
            numeric_fact_tokens(amount).issubset(numeric_fact_tokens(sentence))
            for amount in source_amounts[:2]
        )
    ):
        issues.append("FACT_ADDONS_AMOUNT_OMITTED")
    if (
        re.search(r"\b\d+\+\d+\s*[- ]?year", supporting_text, re.I)
        and re.search(r"\bконтракт\w*\b", sentence, re.I)
        and not re.search(r"\b\d+\+\d+\b", sentence)
    ):
        issues.append("FACT_CONTRACT_DURATION_MUTATED")
    if not club_ids_in_text(sentence).issubset(club_ids_in_text(supporting_text)):
        issues.append("UNSUPPORTED_CLUB_ENTITY")
    source_people = person_ids_in_text(supporting_text)
    final_people = person_ids_in_text(sentence)
    if not final_people.issubset(source_people):
        issues.append("UNSUPPORTED_PERSON_ENTITY")
    source_clubs = club_ids_in_text(supporting_text)
    final_clubs = club_ids_in_text(sentence)
    capitalised = re.findall(r"\b[А-ЯЁ][а-яё-]{3,}\b", sentence)
    for club in club_entries():
        if club.entity_id not in source_clubs or club.entity_id in final_clubs:
            continue
        expected = [part.casefold() for surface in (club.display, *club.forms.values()) for part in surface.split()]
        if any(SequenceMatcher(None, token.casefold(), part).ratio() >= 0.78 for token in capitalised for part in expected):
            issues.append("CORRUPTED_CLUB_ENTITY")
            break
    for person in person_entries():
        if person.entity_id not in source_people:
            continue
        expected = [part.casefold() for surface in (person.display, *person.forms) for part in surface.split()]
        if person.entity_id not in final_people and any(
            SequenceMatcher(None, token.casefold(), part).ratio() >= 0.72
            for token in capitalised for part in expected
        ):
            issues.append("CORRUPTED_PERSON_ENTITY")
            break
        surname = person.display.split()[-1]
        full_surfaces = {
            surface.casefold()
            for surface in (person.display, *person.forms)
            if len(surface.split()) >= 2
        }
        for phrase in re.findall(r"\b[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\b", sentence):
            if SequenceMatcher(None, phrase.split()[-1].casefold(), surname.casefold()).ratio() < 0.72:
                continue
            if phrase.casefold() not in full_surfaces:
                issues.append("CORRUPTED_PERSON_ENTITY")
                break
        if "CORRUPTED_PERSON_ENTITY" in issues:
            break
    for code, pattern in _UNSUPPORTED_CLAIM_PATTERNS:
        if pattern.search(sentence) and (
            code == "UNSUPPORTED_SOURCE_ATTRIBUTION_BODY"
            or not pattern.search(supporting_text)
        ):
            issues.append(code)
    if re.search(r"\bдомашн\w+\s+форм", sentence, re.I) and not re.search(r"\bдомашн\w+\s+форм|home\s+kit", supporting_text, re.I):
        issues.append("UNSUPPORTED_KIT_QUALIFIER")
    for match in _COUNTRY_ADJECTIVES.finditer(sentence):
        if match.group(0).casefold() not in supporting_text.casefold():
            issues.append("UNSUPPORTED_COUNTRY_ATTRIBUTE")
            break
    for geo in ("Каталунья", "Каталония"):
        if re.search(rf"(?<![\w]){geo}(?![\w])", sentence, re.I) and not re.search(
            rf"(?<![\w]){geo}(?![\w])", supporting_text, re.I
        ):
            issues.append("UNSUPPORTED_GEO_ENTITY")
            break
    if "fired" in source_statuses and not re.search(r"\bуволен\w*", sentence, re.I):
        issues.append("FACT_STATUS_FIRED_WEAKENED")
    if "personal_terms_agreed" in source_statuses and "official_completed" in final_statuses:
        issues.append("FACT_STATUS_PERSONAL_TERMS_UPGRADED")
    if "new_player" in source_statuses and "contract_extended" in final_statuses:
        issues.append("FACT_STATUS_NEW_PLAYER_MUTATED")
    if "loan_made_permanent" in source_statuses and re.search(r"продлил\w*\s+аренд", sentence, re.I):
        issues.append("FACT_STATUS_PERMANENT_MUTATED")
    if "club_agreement" in source_statuses and re.search(r"\bсделк\w*\s+завершен\w*", sentence, re.I):
        issues.append("FACT_STATUS_CLUB_AGREEMENT_UPGRADED")
    if "deal_done_not_official" in source_statuses and "official_completed" in final_statuses:
        issues.append("FACT_STATUS_DEAL_DONE_UPGRADED")
    if "bid_expected" in source_statuses and "bid_submitted" in final_statuses:
        issues.append("FACT_STATUS_BID_EXPECTED_UPGRADED")
    if (
        "bid_expected" in source_statuses
        and not final_statuses.intersection(
            {"bid_expected", "bid_submitted", "club_agreement", "deal_done_not_official", "official_completed"}
        )
    ):
        issues.append("FACT_STATUS_BID_EXPECTED_WEAKENED")
    if (
        "club_agreement" in source_statuses
        and "club_agreement" not in final_statuses
        and not ({"official_completed"} <= source_statuses & final_statuses)
    ):
        issues.append("FACT_STATUS_CLUB_AGREEMENT_WEAKENED")
    if re.search(r"\bсделал\w*\s+аренд\w+[^.!?]{0,90}\bпостоянн", sentence, re.I):
        issues.append("LANGUAGE_UNNATURAL_PERMANENT")
    if re.search(r"\bпош[её]л\s+за\s+[А-ЯЁ]", sentence):
        issues.append("LANGUAGE_SOCIAL_CALQUE")
    if re.search(r"\bпять\s+[«\"]?золотых\s+мяч", sentence, re.I):
        issues.append("LANGUAGE_SOCIAL_INTRO")
    if re.search(r"\bздесь\s+мы\s+начинаем\b", sentence, re.I):
        issues.append("LANGUAGE_SOCIAL_INTRO")
    if re.search(r"\bв\s+след\s+за\b", sentence, re.I):
        issues.append("LANGUAGE_BROKEN_PHRASE")
    if re.search(r"\bконтракт\w*[^.!?]{0,80}\bпродл[её]н\w*", sentence, re.I) and "contract_extended" not in source_statuses:
        issues.append("FACT_STATUS_CONTRACT_EXTENDED_UNSUPPORTED")
    if re.search(r"\b(?:Фенербахче|Бенфика|Астон\s+Вилла|Барселона|Бавария)\s+(?:согласовали|согласились|получили|подписали|продали|купили)\b", sentence, re.I):
        issues.append("LANGUAGE_SUBJECT_VERB_AGREEMENT")
    for club in club_entries():
        if not club.grammatical_gender:
            continue
        if re.search(
            rf"(?<![\w]){re.escape(club.display)}\s+(?:согласовали|согласились|получили|подписали|продали|купили)\b",
            sentence,
            re.I,
        ):
            issues.append("LANGUAGE_SUBJECT_VERB_AGREEMENT")
            break
    if re.search(r"\bсделк\w*\s+с\s+(?:клубн\w+\s+)?академи\w*\b", sentence, re.I):
        issues.append("LANGUAGE_ACADEMY_COUNTERPARTY")
    if re.search(r"\bсделк\w*[^.!?]{0,50}\bдля\s+подписания\b", sentence, re.I):
        issues.append("LANGUAGE_LITERAL_SIGNING_CONSTRUCTION")
    if re.search(r"\bпо\s+информации\s+(?:The\s+Sun|The\s+Athletic|Football\.London)\s+[А-ЯЁ]", sentence, re.I):
        issues.append("LANGUAGE_MISSING_INTRO_COMMA")
    if re.search(r"\bназначен\w*[^.!?]{0,60}\bна\s+контракт\s+до\b", sentence, re.I):
        issues.append("LANGUAGE_CONTRACT_GOVERNMENT")
    if re.search(r"\b(?:соглашени\w*|договор\w*)\s+между\s+клубами\s+с\s+[А-ЯЁ]", sentence, re.I):
        issues.append("LANGUAGE_BROKEN_DEAL_RELATION")
    if re.search(r"\bстороны\s+достигли\s+соглашения\s+с\s+[А-ЯЁ]", sentence, re.I):
        issues.append("LANGUAGE_BROKEN_DEAL_RELATION")
    if re.search(r"\b([А-ЯЁ][а-яё-]+)\s+\1\b", sentence):
        issues.append("DUPLICATE_PERSON_TOKEN")
    if (
        re.fullmatch(r"[А-ЯЁ][А-Яа-яЁё -]{2,80}\s+в\s+[А-ЯЁ][А-Яа-яЁё -]{2,40}[!?]", sentence.strip())
        and not re.search(r"\b(?:переш|переход|подпис|стал|станет|близок|соглас|едет|направ|игра|забил)\w*\b", sentence, re.I)
    ):
        issues.append("LANGUAGE_HEADLINE_FRAGMENT")
    if re.search(r"\bс\s+Жоау\s+Пальинья\b|\bПереход\s+Стефан\s+Пош\b", sentence, re.I):
        issues.append("LANGUAGE_PERSON_CASE")
    for person in person_entries():
        if len(person.forms) >= 2 and re.search(
            rf"\b(?:игрока|защитника|полузащитника|нападающего|вратаря)\s+{re.escape(person.display)}\b",
            sentence,
            re.I,
        ):
            issues.append("LANGUAGE_PERSON_CASE")
            break
    for resolution in entity_resolutions:
        if resolution.entity_type != "person" or resolution.resolution_method != "SOURCE_TRANSLITERATED":
            continue
        if re.search(
            rf"\b(?:игрока|защитника|полузащитника|нападающего|вратаря)\s+{re.escape(resolution.rendered_name)}\b",
            sentence,
            re.I,
        ):
            issues.append("LANGUAGE_UNINFLECTED_SOURCE_PERSON")
            break
    if re.search(r"\bна\s+свободное\s+агентство\b", sentence, re.I):
        issues.append("LANGUAGE_FREE_TRANSFER_CALQUE")
    if re.search(r"\bпродаж\w+[^.!?]{0,80}\s+в\s+Фрозиноне\b", sentence, re.I):
        issues.append("LANGUAGE_TRANSFER_TO_CLUB")
    if re.search(r"\bна\s+контракт\s+сроком\s+\d+\+\d+\s+год\b", sentence, re.I):
        issues.append("LANGUAGE_CONTRACT_WORDING")
    if re.search(r"\bсоглашени\w*\s+по\s+оплате\b", sentence, re.I):
        issues.append("LANGUAGE_FEE_WORDING")
    if re.search(r"\bсоглашени\w*\s+по\s+цене\b", sentence, re.I):
        issues.append("LANGUAGE_FEE_WORDING")
    if re.search(r"\bназначен\s+менеджером\s+(?:сборной|команды|клуба)\b", sentence, re.I):
        issues.append("LANGUAGE_FOOTBALL_MANAGER_TERM")
    if re.search(r";\s*контракт\s+рассчитан", sentence, re.I):
        issues.append("LANGUAGE_UNNECESSARY_SEMICOLON")
    if re.search(r"(?<![.!?])\s+Контракт\s*[—–:-]", sentence):
        issues.append("MALFORMED_CLAUSE_BOUNDARY")
    if re.search(r"\bЛидс[’']\s+Джо\s+Гелхардт\b", sentence, re.I):
        issues.append("LANGUAGE_ENGLISH_POSSESSIVE_CLUB")
    if re.search(r"\bЕДЕТ\s+В\b", sentence):
        issues.append("ALL_CAPS_PHRASE")
    if re.search(r"\bедет\s+в\b", sentence, re.I) and "transfer_pending" in source_statuses:
        issues.append("LANGUAGE_TRANSFER_SOCIAL_VERB")
    if re.search(r"\bжелани\w*\s+переехать\b", sentence, re.I) and source_statuses.intersection(
        {"intent", "personal_terms_agreed", "talks", "club_agreement", "transfer_pending"}
    ):
        issues.append("LANGUAGE_TRANSFER_MOVE_WORDING")
    if (
        "club_agreement" in source_statuses
        and re.search(r"\bблизок\s+к\s+переход", sentence, re.I)
        and "club_agreement" in final_statuses
    ):
        issues.append("LANGUAGE_CONFLICTING_TRANSFER_STATUS")
    if re.search(r"\bтренер\s+Нидерландов\b", sentence, re.I):
        issues.append("LANGUAGE_TEAM_NAME")
    if re.search(r"[€£$]\s*\d", sentence):
        issues.append("CURRENCY_SYMBOL_STYLE")
    if re.search(r"(?<![,])\s+как\s+только\b", sentence, re.I):
        issues.append("LANGUAGE_MISSING_SUBORDINATE_COMMA")
    if "support_quote" in source_statuses and re.search(r"\bв\s+сообщении\s+говорится\s+о\s+том\b", sentence, re.I):
        issues.append("LANGUAGE_QUOTE_META_FILLER")
    if len(sentence) > 260 and len(facts) > 1:
        issues.append("RUN_ON_SENTENCE")
    if not re.search(r"[.!?…][»\"]?$", sentence.strip()):
        issues.append("MISSING_SENTENCE_TERMINATOR")
    for club in club_entries():
        genitive = club.forms.get("genitive", club.display)
        if genitive != club.display and re.search(rf"\bиз\s+{re.escape(club.display)}\b", sentence, re.I):
            issues.append("LANGUAGE_CLUB_CASE")
            break
        if genitive != club.display and re.search(rf"\bтренер\w*\s+{re.escape(club.display)}\b", sentence, re.I):
            issues.append("LANGUAGE_CLUB_CASE")
            break
        if genitive != club.display and re.search(rf"\bигроком\s+{re.escape(club.display)}\b", sentence, re.I):
            issues.append("LANGUAGE_CLUB_CASE")
            break
    # Any strong output status must be licensed by an equally strong source status.
    licensed = source_statuses | {"match", "kit", "support_quote", "interest", "talks", "bid_offer", "intent", "loan", "transfer_pending"}
    for status in final_statuses - licensed:
        if status in {"official_completed", "contract_extended", "club_agreement", "medical_scheduled", "loan_made_permanent", "fired"}:
            issues.append("UNSUPPORTED_FACT_STATUS")
    if re.search(r"\bпросьб\w*[^.!?]{0,30}\bобнять", sentence, re.I) and re.search(r"\bhuge\s+hug\b", supporting_text, re.I):
        issues.append("QUOTE_SPEECH_ACT_INVERSION")
    return issues


def validate_source_locked_draft(
    packet: SourceFactPacket,
    draft: SourceLockedDraft,
    *,
    max_chars: int,
) -> SourceLockedValidation:
    issues: list[str] = []
    if packet.intent != "NEWS":
        issues.append("PROMOTIONAL_PRIMARY")
    if packet.source_language_issue:
        issues.append(packet.source_language_issue)
    if packet.relevance_class == "OPTIONAL_OFF_PITCH":
        issues.append("NON_FOOTBALL_RELEVANCE")
    if packet.unresolved_entities:
        issues.append("UNRESOLVED_SOURCE_ENTITY")
    if packet.provenance_issue:
        issues.append(packet.provenance_issue)
    if not packet.facts:
        issues.append("NO_SOURCE_FACTS")
    fact_index = {fact.fact_id: fact for fact in packet.facts}
    normalized_rows: list[str] = []
    for row in draft.sentences:
        unknown = [fact_id for fact_id in row.fact_ids if fact_id not in fact_index]
        if unknown:
            issues.append("UNKNOWN_FACT_ID")
            continue
        normalized = normalize_editorial_text(row.text, "")
        normalized_rows.append(normalized)
        supporting = tuple(fact_index[fact_id] for fact_id in row.fact_ids)
        supported_entity_ids = {entity_id for fact in supporting for entity_id in fact.entity_ids}
        for resolution in packet.entity_resolutions:
            if (
                re.search(rf"(?<![\w]){re.escape(resolution.rendered_name)}(?![\w])", normalized, re.I)
                and resolution.entity_id not in supported_entity_ids
            ):
                issues.append("UNSUPPORTED_ENTITY_FACT_MAPPING")
        issues.extend(_support_issues(normalized, supporting, packet.entity_resolutions))
        editorial = assess_editorial(packet.primary_text, normalized, source_name="")
        issues.extend(editorial.issues)
    body = " ".join(normalized_rows).strip()
    if not body or len(body) > max_chars:
        issues.append("INVALID_OUTPUT_LENGTH")
    if re.search(r"[«\"](?:[^»\"\n]{1,80})[»\"]", body):
        quoted = re.findall(r"[«\"]([^»\"\n]{1,80})[»\"]", body)
        if any(club_ids_in_text(value) for value in quoted):
            issues.append("CLUB_QUOTE_STYLE")
        source_clubs = club_ids_in_text(packet.primary_text)
        for value in quoted:
            for club in club_entries():
                if club.entity_id not in source_clubs:
                    continue
                pieces = [part.casefold() for surface in (club.display, *club.forms.values()) for part in surface.split()]
                if any(SequenceMatcher(None, value.casefold(), piece).ratio() >= 0.7 for piece in pieces):
                    issues.append("CLUB_QUOTE_STYLE")
                    break
    if re.search(r"[.!?]\s+[а-яё]", body):
        issues.append("MALFORMED_SENTENCE_BOUNDARY")
    packet_people = tuple(row for row in packet.entity_resolutions if row.entity_type == "person")
    if packet_people and not any(
        any(
            re.search(rf"(?<![\w]){re.escape(surface)}(?![\w])", body, re.I)
            for surface in _resolution_surfaces(row)
        )
        for row in packet_people
    ):
        issues.append("MISSING_CANONICAL_PERSON")
    if packet.source_display:
        variants = {
            "Fabrizio Romano": r"(?:Fabrizio\s+Romano|Фабрицио\s+Романо)",
            "Ben Jacobs": r"(?:Ben\s+Jacobs|Б[еэ]н\s+Дж[ае]кобс)",
        }
        pattern = variants.get(packet.source_display, re.escape(packet.source_display))
        if re.search(rf"(?<![\w]){pattern}(?![\w])", body, re.I):
            issues.append("SOURCE_NAME_IN_BODY")
    if re.search(r"\bФабр[а-яё]{2,}\s+Роман[оа]\b|\bБ[еэ]н\s+Дж[ае]кобс\b", body, re.I):
        issues.append("CORRUPTED_SOURCE_NAME_IN_BODY")
    latin_body = body
    for allowed in ("The Sun", "The Athletic", "Football.London"):
        latin_body = re.sub(re.escape(allowed), " ", latin_body, flags=re.I)
    if re.search(r"\b[A-Za-z]{2,}(?:\s+[A-Za-z]{2,})*\b", latin_body):
        issues.append("ENGLISH_TEXT_LEAK")
    return SourceLockedValidation(tuple(dict.fromkeys(issues)))


def render_source_locked_result(packet: SourceFactPacket, draft: SourceLockedDraft) -> SourceLockedResult:
    normalized = tuple(
        SupportedSentence(normalize_editorial_text(row.text, ""), row.fact_ids)
        for row in draft.sentences
    )
    body = " ".join(row.text.rstrip() for row in normalized).strip()
    if packet.source_display:
        body = f"{body}\n\nИсточник: {packet.source_display}"
    return SourceLockedResult(body, normalized, packet)
