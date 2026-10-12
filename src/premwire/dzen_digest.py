from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from difflib import SequenceMatcher
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .editorial_desk import StoryCategory, classify_story, same_editorial_story
from .storage import EventIdentity, build_event_identity


MOSCOW_TIMEZONE = "Europe/Moscow"


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:
        # Windows installations without the optional ``tzdata`` wheel still
        # have a stable UTC+03:00 Moscow offset.  Keep the daily boundary
        # deterministic instead of disabling the whole digest worker.
        if name == MOSCOW_TIMEZONE:
            return timezone(timedelta(hours=3), name=MOSCOW_TIMEZONE)
        raise ValueError(f"unknown digest timezone: {name}") from exc


_SECTION_ORDER: tuple[StoryCategory, ...] = (
    StoryCategory.MATCH_RESULT,
    StoryCategory.TRANSFER_OFFICIAL,
    StoryCategory.TRANSFER_UPDATE,
    StoryCategory.MATCH_NEWS,
    StoryCategory.INJURY,
    StoryCategory.TEAM_NEWS,
    StoryCategory.MANAGER_NEWS,
    StoryCategory.CONTRACT,
    StoryCategory.TOURNAMENT_NEWS,
    StoryCategory.QUOTE,
    StoryCategory.GENERAL_FOOTBALL,
)

_SECTION_HEADINGS: dict[StoryCategory, str] = {
    StoryCategory.MATCH_RESULT: "Результаты матчей",
    StoryCategory.TRANSFER_OFFICIAL: "Официальные переходы",
    StoryCategory.TRANSFER_UPDATE: "Трансферные новости",
    StoryCategory.MATCH_NEWS: "События матчей",
    StoryCategory.INJURY: "Травмы и восстановление",
    StoryCategory.TEAM_NEWS: "Новости команд",
    StoryCategory.MANAGER_NEWS: "Тренеры",
    StoryCategory.CONTRACT: "Контракты",
    StoryCategory.TOURNAMENT_NEWS: "Турниры",
    StoryCategory.QUOTE: "Заявления",
    StoryCategory.GENERAL_FOOTBALL: "Другие события",
}

_SHORT_LABELS: dict[StoryCategory, str] = {
    category: heading.rstrip("ы") if category is StoryCategory.TEAM_NEWS else heading
    for category, heading in _SECTION_HEADINGS.items()
}

_COMPACT_CATEGORIES = frozenset(
    {
        StoryCategory.TEAM_NEWS,
        StoryCategory.QUOTE,
        StoryCategory.GENERAL_FOOTBALL,
    }
)

_TRANSFER_STAGE_RANK = {
    "TRANSFER_UPDATE": 0,
    "INTEREST": 1,
    "CONTACT": 2,
    "TALKS": 3,
    "BID": 4,
    "AGREEMENT": 5,
    "MEDICAL": 6,
    "OFFICIAL": 7,
}

_MONTHS_GENITIVE = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_NUMBER_RE = re.compile(
    r"(?<![\w])(?:[€£$]\s*)?\d+(?:[.,]\d+)?"
    r"(?:\s*[:\-–—]\s*\d+(?:[.,]\d+)?)?"
    r"(?:\s*(?:%|млн|миллион\w*|млрд|миллиард\w*|тыс\w*|"
    r"евро|фунт\w*|доллар\w*|лет|год\w*|месяц\w*|недел\w*|"
    r"дн(?:я|ей)|минут\w*|матч\w*|гол\w*|очк\w*|трофе\w*))?",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+(?:[-'][A-Za-zА-Яа-яЁё0-9]+)*")
_CAPITALISED_WORD_RE = re.compile(
    r"(?<![\w-])(?:[А-ЯЁ][а-яё]{1,}|[A-Z][a-z]{1,}|[А-ЯЁA-Z]{2,})(?![\w-])"
)
_FIRST_SENTENCE_RE = re.compile(r"^(.+?[.!?])(?:\s|$)", re.DOTALL)
_SPACE_RE = re.compile(r"\s+")

_GENERIC_CAPITALISED_WORDS = frozenset(
    {
        "итоги",
        "коротко",
        "официально",
        "результаты",
        "события",
        "трансферные",
        "новости",
        "другие",
    }
)

_FACT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("negation", re.compile(r"\b(?:не|нет|not|no)\b", re.IGNORECASE)),
    ("rejected", re.compile(r"\b(?:отказ\w*|отклонил\w*|rejected?)\b", re.IGNORECASE)),
    ("interest", re.compile(r"\b(?:интерес\w*|interest(?:ed)?)\b", re.IGNORECASE)),
    ("talks", re.compile(r"\b(?:переговор\w*|talks?|negotiat\w*)\b", re.IGNORECASE)),
    ("bid", re.compile(r"\b(?:предложени\w*|заявк\w*|bid|offer)\b", re.IGNORECASE)),
    ("agreement", re.compile(r"\b(?:согласова\w*|договорил\w*|agreement|agreed)\b", re.IGNORECASE)),
    ("official", re.compile(r"\b(?:официальн\w*|official(?:ly)?)\b", re.IGNORECASE)),
    ("loan", re.compile(r"\b(?:аренд\w*|loan)\b", re.IGNORECASE)),
    ("injury", re.compile(r"\b(?:травм\w*|повреждени\w*|injur\w*)\b", re.IGNORECASE)),
    ("win", re.compile(r"\b(?:побед\w*|обыграл\w*|выиграл\w*|won|victory)\b", re.IGNORECASE)),
    ("loss", re.compile(r"\b(?:поражени\w*|проиграл\w*|lost|defeat\w*)\b", re.IGNORECASE)),
)

_CONTENT_STOPWORDS = frozenset(
    {
        "его",
        "ее",
        "её",
        "для",
        "как",
        "что",
        "это",
        "тот",
        "при",
        "после",
        "между",
        "уже",
        "еще",
        "ещё",
        "the",
        "and",
        "for",
        "from",
        "with",
        "has",
        "have",
        "was",
        "were",
    }
)


def _normalise_display_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


def _normalise_comparison_text(value: str) -> str:
    return " ".join(word.casefold() for word in _WORD_RE.findall(value))


def _content_tokens(value: str) -> frozenset[str]:
    return frozenset(
        word.casefold()
        for word in _WORD_RE.findall(value)
        if len(word) >= 3 and word.casefold() not in _CONTENT_STOPWORDS
    )


def _mapping_value(row: Mapping[str, object], *names: str) -> object | None:
    keys = set(row.keys())
    for name in names:
        if name in keys:
            return row[name]
    return None


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("published_at is required")
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    return result.astimezone(timezone.utc)


def _normalise_category(value: object, text: str) -> StoryCategory:
    if isinstance(value, StoryCategory):
        return value
    raw = str(value or "").strip().upper()
    try:
        return StoryCategory(raw)
    except ValueError:
        return classify_story(text)


@dataclass(frozen=True, slots=True)
class DigestSnapshot:
    """Immutable input copied from an already published ledger row."""

    item_id: int
    published_text: str
    editorial_category: StoryCategory
    published_at: datetime
    editorial_importance: int = 50

    def __post_init__(self) -> None:
        if self.item_id <= 0:
            raise ValueError("item_id must be positive")
        text = _normalise_display_text(self.published_text)
        if not text:
            raise ValueError("published_text is required")
        object.__setattr__(self, "published_text", text)
        object.__setattr__(self, "published_at", _coerce_datetime(self.published_at))
        object.__setattr__(
            self,
            "editorial_importance",
            max(0, min(100, int(self.editorial_importance))),
        )
        object.__setattr__(
            self,
            "editorial_category",
            _normalise_category(self.editorial_category, text),
        )

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> "DigestSnapshot":
        text = str(_mapping_value(row, "published_text") or "")
        importance = _mapping_value(row, "editorial_importance")
        return cls(
            item_id=int(_mapping_value(row, "item_id", "id") or 0),
            published_text=text,
            editorial_category=_normalise_category(
                _mapping_value(row, "editorial_category"), text
            ),
            published_at=_coerce_datetime(
                _mapping_value(row, "published_at", "telegram_published_at")
            ),
            editorial_importance=int(importance if importance is not None else 50),
        )


@dataclass(frozen=True, slots=True)
class DigestStory:
    story_key: str
    category: StoryCategory
    importance: int
    published_at: datetime
    snapshot_ids: tuple[int, ...]
    rendered_snapshot_ids: tuple[int, ...]
    paragraphs: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def factual_tokens(self) -> frozenset[str]:
        tokens: set[str] = set()
        for paragraph in self.paragraphs:
            tokens.update(extract_factual_tokens(paragraph))
        return frozenset(tokens)


@dataclass(frozen=True, slots=True)
class DigestSection:
    heading: str
    category: StoryCategory | None
    stories: tuple[DigestStory, ...]
    compact: bool = False


@dataclass(frozen=True, slots=True)
class DzenDigest:
    digest_date: date
    timezone_name: str
    title: str
    lead: str
    sections: tuple[DigestSection, ...]
    source_snapshot_ids: tuple[int, ...]

    @property
    def stories(self) -> tuple[DigestStory, ...]:
        return tuple(story for section in self.sections for story in section.stories)

    @property
    def body_text(self) -> str:
        parts = [self.lead]
        for section in self.sections:
            parts.extend((f"## {section.heading}", _render_section(section)))
        return "\n\n".join(part for part in parts if part)

    @property
    def article_text(self) -> str:
        return f"{self.title}\n\n{self.body_text}"


@dataclass(frozen=True, slots=True)
class DigestValidation:
    valid: bool
    source_coverage: float
    factual_token_coverage: float
    missing_snapshot_ids: tuple[int, ...] = ()
    duplicate_snapshot_ids: tuple[int, ...] = ()
    missing_factual_tokens: tuple[str, ...] = ()
    unexpected_factual_tokens: tuple[str, ...] = ()
    unrendered_story_keys: tuple[str, ...] = ()
    misplaced_story_keys: tuple[str, ...] = ()


def extract_factual_tokens(text: str) -> frozenset[str]:
    """Extract conservative tokens that must survive deterministic assembly.

    The extractor deliberately favours recall. It covers numbers, capitalised
    name/club words, canonical entities recognised by the production event
    identity, and meaning-changing status markers.
    """

    tokens: set[str] = set()
    for match in _NUMBER_RE.finditer(text):
        number = re.sub(r"\s+", " ", match.group(0).casefold()).strip()
        number = re.sub(r"\s*([:\-–—])\s*", r"\1", number)
        number = re.sub(r"([€£$])\s+", r"\1", number)
        tokens.add(f"number:{number}")

    for match in _CAPITALISED_WORD_RE.finditer(text):
        word = match.group(0).casefold()
        if word not in _GENERIC_CAPITALISED_WORDS:
            tokens.add(f"proper:{word}")

    identity = build_event_identity(text)
    if identity is not None:
        tokens.add(f"event:{identity.event_type.casefold()}")
        if identity.player:
            tokens.add(f"player:{identity.player}")
        if identity.from_club:
            tokens.add(f"club:{identity.from_club}")
        if identity.to_club:
            tokens.add(f"club:{identity.to_club}")
        tokens.update(f"club:{club}" for club in identity.clubs)

    for marker, pattern in _FACT_MARKERS:
        if pattern.search(text):
            tokens.add(f"marker:{marker}")
    return frozenset(tokens)


def _identity_stage_rank(identity: EventIdentity | None) -> int:
    if identity is None:
        return -1
    if identity.event_type == "TRANSFER":
        return _TRANSFER_STAGE_RANK.get(identity.stage, -1)
    if identity.event_type == "MATCH":
        return 2 if identity.stage == "FINAL" else 1
    if identity.event_type == "LINEUP":
        return 1
    return 0


def _same_event(first: DigestSnapshot, second: DigestSnapshot) -> bool:
    first_normalised = _normalise_comparison_text(first.published_text)
    second_normalised = _normalise_comparison_text(second.published_text)
    if first_normalised == second_normalised:
        return True

    first_identity = build_event_identity(first.published_text)
    second_identity = build_event_identity(second.published_text)
    if first_identity is not None and second_identity is not None:
        if first_identity.event_type != second_identity.event_type:
            return False
        if first_identity.event_type == "TRANSFER":
            if not first_identity.player or first_identity.player != second_identity.player:
                return False
            if (
                first_identity.to_club
                and second_identity.to_club
                and first_identity.to_club != second_identity.to_club
            ):
                return False
            if (
                first_identity.clubs
                and second_identity.clubs
                and set(first_identity.clubs).isdisjoint(second_identity.clubs)
            ):
                return False
            return True
        if first_identity.event_type == "MATCH":
            return bool(
                first_identity.clubs
                and first_identity.clubs == second_identity.clubs
            )
        if first_identity.event_type == "LINEUP":
            return bool(
                first_identity.player == second_identity.player
                and not set(first_identity.clubs).isdisjoint(second_identity.clubs)
            )

    if first_identity is not None or second_identity is not None:
        return False

    similarity = SequenceMatcher(None, first_normalised, second_normalised).ratio()
    return similarity >= 0.45 and same_editorial_story(
        first.published_text, second.published_text
    )


def _cluster_snapshots(snapshots: Sequence[DigestSnapshot]) -> tuple[tuple[DigestSnapshot, ...], ...]:
    clusters: list[list[DigestSnapshot]] = []
    for snapshot in sorted(snapshots, key=lambda item: (item.published_at, item.item_id)):
        for cluster in clusters:
            if any(_same_event(snapshot, member) for member in cluster):
                cluster.append(snapshot)
                break
        else:
            clusters.append([snapshot])
    return tuple(tuple(cluster) for cluster in clusters)


def _representative(cluster: Sequence[DigestSnapshot]) -> DigestSnapshot:
    return max(
        cluster,
        key=lambda snapshot: (
            _identity_stage_rank(build_event_identity(snapshot.published_text)),
            snapshot.editorial_importance,
            snapshot.published_at,
            snapshot.item_id,
        ),
    )


def _is_redundant_paragraph(text: str, selected: Sequence[str]) -> bool:
    if not selected:
        return False
    normalised = _normalise_comparison_text(text)
    facts = extract_factual_tokens(text)
    words = _content_tokens(text)
    selected_facts: set[str] = set()
    selected_words: set[str] = set()
    for paragraph in selected:
        selected_facts.update(extract_factual_tokens(paragraph))
        selected_words.update(_content_tokens(paragraph))
        other = _normalise_comparison_text(paragraph)
        if normalised == other:
            return True
        if facts.issubset(selected_facts) and SequenceMatcher(None, normalised, other).ratio() >= 0.86:
            return True
    if not facts.issubset(selected_facts):
        return False
    if not words:
        return True
    overlap = len(words.intersection(selected_words)) / len(words)
    return overlap >= 0.88 or len(words.difference(selected_words)) <= 1


def _story_from_cluster(cluster: Sequence[DigestSnapshot]) -> DigestStory:
    representative = _representative(cluster)
    ordered = [representative]
    ordered.extend(
        sorted(
            (item for item in cluster if item.item_id != representative.item_id),
            key=lambda item: (item.published_at, item.item_id),
            reverse=True,
        )
    )
    paragraphs: list[str] = []
    rendered_ids: list[int] = []
    for snapshot in ordered:
        if _is_redundant_paragraph(snapshot.published_text, paragraphs):
            continue
        paragraphs.append(snapshot.published_text)
        rendered_ids.append(snapshot.item_id)

    identity = build_event_identity(representative.published_text)
    if identity is not None:
        identity_key = ":".join(
            part
            for part in (
                identity.event_type,
                identity.player or "",
                identity.from_club or "",
                identity.to_club or "",
                ",".join(identity.clubs),
            )
            if part
        )
        story_key = f"event-{hashlib.sha256(identity_key.encode('utf-8')).hexdigest()[:16]}"
    else:
        story_key = "story-" + hashlib.sha256(
            _normalise_comparison_text(representative.published_text).encode("utf-8")
        ).hexdigest()[:16]
    return DigestStory(
        story_key=story_key,
        category=representative.editorial_category,
        importance=max(item.editorial_importance for item in cluster),
        published_at=max(item.published_at for item in cluster),
        snapshot_ids=tuple(sorted(item.item_id for item in cluster)),
        rendered_snapshot_ids=tuple(rendered_ids),
        paragraphs=tuple(paragraphs),
    )


def _is_compact_story(story: DigestStory, max_chars: int, importance_below: int) -> bool:
    return bool(
        story.category in _COMPACT_CATEGORIES
        and story.importance < importance_below
        and len(story.text) <= max_chars
        and len(story.paragraphs) == 1
    )


def _format_digest_date(value: date) -> str:
    return f"{value.day} {_MONTHS_GENITIVE[value.month]} {value.year} года"


def _title_for(stories: Sequence[DigestStory], digest_date: date) -> str:
    top_story = max(
        stories,
        key=lambda story: (story.importance, story.published_at, story.story_key),
    )
    paragraph = top_story.paragraphs[0]
    match = _FIRST_SENTENCE_RE.match(paragraph)
    first_sentence = (match.group(1) if match else paragraph).strip().rstrip(".!?")
    if 20 <= len(first_sentence) <= 92:
        return (
            f"Итоги футбольного дня за {digest_date.day} "
            f"{_MONTHS_GENITIVE[digest_date.month]}: {first_sentence}"
        )
    return f"Итоги футбольного дня — {_format_digest_date(digest_date)}"


def _lead_for(story_count: int, digest_date: date) -> str:
    if story_count % 10 == 1 and story_count % 100 != 11:
        noun = "сюжет"
    elif story_count % 10 in {2, 3, 4} and story_count % 100 not in {12, 13, 14}:
        noun = "сюжета"
    else:
        noun = "сюжетов"
    return (
        f"В итоговую подборку за {_format_digest_date(digest_date)} вошло "
        f"{story_count} {noun}. Все факты ниже ранее прошли редакционный "
        "контроль PremWire."
    )


def _render_section(section: DigestSection) -> str:
    if section.compact:
        lines = []
        for story in section.stories:
            label = _SHORT_LABELS[story.category]
            lines.append(f"- **{label}.** {story.text}")
        return "\n".join(lines)
    return "\n\n".join(story.text for story in section.stories)


def _coerce_snapshots(
    snapshots: Iterable[DigestSnapshot | Mapping[str, object]],
) -> tuple[DigestSnapshot, ...]:
    result: list[DigestSnapshot] = []
    seen: set[int] = set()
    for value in snapshots:
        snapshot = value if isinstance(value, DigestSnapshot) else DigestSnapshot.from_mapping(value)
        if snapshot.item_id in seen:
            raise ValueError(f"duplicate snapshot item_id: {snapshot.item_id}")
        seen.add(snapshot.item_id)
        result.append(snapshot)
    return tuple(result)


def snapshots_for_day(
    snapshots: Iterable[DigestSnapshot | Mapping[str, object]],
    digest_date: date,
    *,
    timezone_name: str = MOSCOW_TIMEZONE,
) -> tuple[DigestSnapshot, ...]:
    zone = _load_timezone(timezone_name)
    coerced = _coerce_snapshots(snapshots)
    return tuple(
        snapshot
        for snapshot in coerced
        if snapshot.published_at.astimezone(zone).date() == digest_date
    )


def build_daily_digest(
    snapshots: Iterable[DigestSnapshot | Mapping[str, object]],
    digest_date: date,
    *,
    timezone_name: str = MOSCOW_TIMEZONE,
    compact_max_chars: int = 240,
    compact_importance_below: int = 55,
) -> DzenDigest:
    """Build a deterministic native-style article from published snapshots."""

    if compact_max_chars < 1:
        raise ValueError("compact_max_chars must be positive")
    if not 0 <= compact_importance_below <= 101:
        raise ValueError("compact_importance_below must be between 0 and 101")
    selected = snapshots_for_day(
        snapshots,
        digest_date,
        timezone_name=timezone_name,
    )
    if not selected:
        raise ValueError(f"no published snapshots for {digest_date.isoformat()}")

    stories = tuple(_story_from_cluster(cluster) for cluster in _cluster_snapshots(selected))
    grouped: dict[StoryCategory, list[DigestStory]] = defaultdict(list)
    compact: list[DigestStory] = []
    for story in stories:
        if _is_compact_story(story, compact_max_chars, compact_importance_below):
            compact.append(story)
        else:
            grouped[story.category].append(story)

    def ordered_stories(values: Sequence[DigestStory]) -> tuple[DigestStory, ...]:
        return tuple(
            sorted(
                values,
                key=lambda story: (
                    -story.importance,
                    -story.published_at.timestamp(),
                    story.story_key,
                ),
            )
        )

    sections: list[DigestSection] = []
    for category in _SECTION_ORDER:
        if grouped[category]:
            sections.append(
                DigestSection(
                    heading=_SECTION_HEADINGS[category],
                    category=category,
                    stories=ordered_stories(grouped[category]),
                )
            )
    if compact:
        sections.append(
            DigestSection(
                heading="Коротко",
                category=None,
                stories=ordered_stories(compact),
                compact=True,
            )
        )

    ordered_all = tuple(story for section in sections for story in section.stories)
    digest = DzenDigest(
        digest_date=digest_date,
        timezone_name=timezone_name,
        title=_title_for(ordered_all, digest_date),
        lead=_lead_for(len(ordered_all), digest_date),
        sections=tuple(sections),
        source_snapshot_ids=tuple(sorted(snapshot.item_id for snapshot in selected)),
    )
    validation = validate_digest(digest, selected)
    if not validation.valid:
        raise ValueError(f"digest validation failed: {validation}")
    return digest


def validate_digest(
    digest: DzenDigest,
    snapshots: Iterable[DigestSnapshot | Mapping[str, object]],
) -> DigestValidation:
    """Verify source coverage and conservative fact-token preservation."""

    selected = snapshots_for_day(
        snapshots,
        digest.digest_date,
        timezone_name=digest.timezone_name,
    )
    expected_ids = {snapshot.item_id for snapshot in selected}
    represented_ids: list[int] = [
        item_id for story in digest.stories for item_id in story.snapshot_ids
    ]
    represented_set = set(represented_ids)
    missing_ids = tuple(sorted(expected_ids.difference(represented_set)))
    duplicate_ids = tuple(
        sorted(item_id for item_id in represented_set if represented_ids.count(item_id) > 1)
    )

    source_tokens: set[str] = set()
    for snapshot in selected:
        source_tokens.update(extract_factual_tokens(snapshot.published_text))
    output_tokens: set[str] = set()
    for story in digest.stories:
        output_tokens.update(story.factual_tokens)
    missing_tokens = tuple(sorted(source_tokens.difference(output_tokens)))
    unexpected_tokens = tuple(sorted(output_tokens.difference(source_tokens)))

    article = digest.article_text
    unrendered = tuple(
        story.story_key for story in digest.stories if story.text not in article
    )
    misplaced: list[str] = []
    for section in digest.sections:
        if section.compact:
            if section.category is not None:
                misplaced.extend(story.story_key for story in section.stories)
            continue
        if section.category is None:
            misplaced.extend(story.story_key for story in section.stories)
            continue
        misplaced.extend(
            story.story_key
            for story in section.stories
            if story.category is not section.category
        )

    source_coverage = 1.0 if not expected_ids else len(
        expected_ids.intersection(represented_set)
    ) / len(expected_ids)
    factual_coverage = 1.0 if not source_tokens else len(
        source_tokens.intersection(output_tokens)
    ) / len(source_tokens)
    valid = not (
        missing_ids
        or duplicate_ids
        or missing_tokens
        or unexpected_tokens
        or unrendered
        or misplaced
        or represented_set.difference(expected_ids)
    )
    return DigestValidation(
        valid=valid,
        source_coverage=round(source_coverage, 6),
        factual_token_coverage=round(factual_coverage, 6),
        missing_snapshot_ids=missing_ids,
        duplicate_snapshot_ids=duplicate_ids,
        missing_factual_tokens=missing_tokens,
        unexpected_factual_tokens=unexpected_tokens,
        unrendered_story_keys=unrendered,
        misplaced_story_keys=tuple(sorted(misplaced)),
    )


__all__ = [
    "DigestSection",
    "DigestSnapshot",
    "DigestStory",
    "DigestValidation",
    "DzenDigest",
    "MOSCOW_TIMEZONE",
    "build_daily_digest",
    "extract_factual_tokens",
    "snapshots_for_day",
    "validate_digest",
]
