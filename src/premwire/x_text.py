from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata

from .storage import build_event_identity, extract_hashtag_entities
from .text_editor import sanitize_publication_text


_SPACE_RE = re.compile(r"[ \t]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_LINK_CTA_RE = re.compile(
    r"\s*(?:подробнее|полный текст|читать полностью|источник)\s*"
    r"(?:доступен\s*)?(?:по\s*)?(?:ссылке|здесь)\s*:?.*$",
    re.IGNORECASE,
)
_HASHTAG_SAFE_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)
_COMPOUND_SURNAME_PARTICLES = frozenset(
    {"al", "da", "de", "del", "den", "der", "di", "dos", "du", "la", "le", "van", "von"}
)
_CLUB_TOKEN_CASE = {"ac": "AC", "afc": "AFC", "fc": "FC", "rb": "RB", "psg": "PSG"}
_TRANSFER_HASHTAG_STAGES = frozenset({"AGREEMENT", "MEDICAL", "OFFICIAL"})
_COMPETITION_HASHTAGS = (
    (re.compile(r"\bpremier league\b", re.IGNORECASE), "PremierLeague"),
    (re.compile(r"\bchampions league\b", re.IGNORECASE), "ChampionsLeague"),
    (re.compile(r"\beuropa league\b", re.IGNORECASE), "EuropaLeague"),
    (re.compile(r"\bconference league\b", re.IGNORECASE), "ConferenceLeague"),
    (re.compile(r"\bnations league\b", re.IGNORECASE), "NationsLeague"),
    (re.compile(r"\bworld cup\b", re.IGNORECASE), "WorldCup"),
    (re.compile(r"\bla liga\b", re.IGNORECASE), "LaLiga"),
    (re.compile(r"\bserie a\b", re.IGNORECASE), "SerieA"),
    (re.compile(r"\bbundesliga\b", re.IGNORECASE), "Bundesliga"),
    (re.compile(r"\bligue 1\b", re.IGNORECASE), "Ligue1"),
    (re.compile(r"\bchampionship\b", re.IGNORECASE), "Championship"),
)
_NAMED_ENTITY_HASHTAGS = (
    (re.compile(r"\bpeppa pig\b", re.IGNORECASE), "PeppaPig"),
)


def _clean(value: str) -> str:
    value = sanitize_publication_text(value)
    value = _LINK_CTA_RE.sub("", value)
    lines = [_SPACE_RE.sub(" ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _trim_at_word(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    shortened = value[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    if not shortened:
        shortened = value[:limit].rstrip()
    return shortened.rstrip(".!?") + "…"


def _compact_body(cleaned: str, limit: int) -> str:
    if len(cleaned) <= limit:
        return cleaned

    paragraphs = [part.strip() for part in cleaned.split("\n") if part.strip()]
    sentences: list[str] = []
    for paragraph in paragraphs:
        sentences.extend(
            sentence.strip()
            for sentence in _SENTENCE_RE.split(paragraph)
            if sentence.strip()
        )

    selected: list[str] = []
    for sentence in sentences:
        candidate = " ".join(selected + [sentence])
        if len(candidate) <= limit:
            selected.append(sentence)
            continue
        break
    if selected:
        return " ".join(selected)
    return _trim_at_word(cleaned.replace("\n", " "), limit)


def _hashtag_token(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = folded.translate(
        str.maketrans({"ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ø": "o", "Ø": "O"})
    )
    return _HASHTAG_SAFE_RE.sub("", folded)


def _ascii_entity_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return folded.translate(
        str.maketrans({"ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ø": "o", "Ø": "O"})
    )


def _is_english_copy(value: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", value))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", value))
    return latin >= 8 and latin > cyrillic


def _surname_hashtag(player: str) -> str:
    parts = [_hashtag_token(part) for part in player.split()]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    selected = [parts[-1]]
    cursor = len(parts) - 2
    while cursor >= 0 and parts[cursor].casefold() in _COMPOUND_SURNAME_PARTICLES:
        selected.insert(0, parts[cursor])
        cursor -= 1
    return "".join(part[:1].upper() + part[1:] for part in selected)


def _club_hashtag(club_id: str) -> str:
    parts = [_hashtag_token(part) for part in club_id.split("_")]
    return "".join(
        _CLUB_TOKEN_CASE.get(part.casefold(), part[:1].upper() + part[1:])
        for part in parts
        if part
    )


def _surname_comparison_key(player: str) -> str:
    parts = [_hashtag_token(part).casefold() for part in player.split()]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    surname = parts[-1].replace("y", "i")
    for suffix in ("ami", "yami", "ogo", "emu", "omu", "om", "ym", "im", "a", "u", "e"):
        if surname.endswith(suffix) and len(surname) - len(suffix) >= 5:
            return surname[: -len(suffix)]
    return surname


def _same_player_spelling(source_player: str, canonical_player: str) -> bool:
    source = _surname_comparison_key(source_player)
    canonical = _surname_comparison_key(canonical_player)
    if not source or not canonical:
        return False
    if source == canonical:
        return True
    similarity = SequenceMatcher(None, source, canonical).ratio()
    source_parts = [_hashtag_token(part).casefold() for part in source_player.split()]
    canonical_parts = [_hashtag_token(part).casefold() for part in canonical_player.split()]
    same_given_name = (
        len(source_parts) > 1
        and len(canonical_parts) > 1
        and source_parts[0] == canonical_parts[0]
    )
    return similarity >= 0.84 or (same_given_name and similarity >= 0.75)


def _canonical_player_for_hashtag(
    identity_player: str,
    destination_club: str,
    canonical_text: str | None,
) -> str:
    """Prefer a verified Latin spelling from the final X copy.

    Event identity intentionally transliterates Russian text for dedupe.  That
    representation can retain a Russian case ending (Ришарлисона ->
    risharlisona), so it is unsuitable as display spelling.  The final English
    X copy is accepted only when it describes the same transfer destination and
    its player spelling is a close match to the source identity.
    """

    if not canonical_text:
        return identity_player
    canonical_identity = build_event_identity(canonical_text)
    if (
        canonical_identity is None
        or canonical_identity.event_type != "TRANSFER"
        or not canonical_identity.player
        or destination_club not in canonical_identity.clubs
        or not _same_player_spelling(identity_player, canonical_identity.player)
    ):
        return identity_player
    return canonical_identity.player


def transfer_hashtags(
    topic_text: str,
    *,
    canonical_text: str | None = None,
) -> tuple[str, ...]:
    """Return factual player/destination tags only for a committed transfer stage."""

    identity = build_event_identity(topic_text)
    if (
        identity is None
        or identity.event_type != "TRANSFER"
        or identity.stage not in _TRANSFER_HASHTAG_STAGES
        or not identity.player
        or not identity.to_club
    ):
        return ()
    player = _canonical_player_for_hashtag(
        identity.player,
        identity.to_club,
        canonical_text,
    )
    surname = _surname_hashtag(player)
    club = _club_hashtag(identity.to_club)
    if not surname or not club:
        return ()
    return f"#{surname}", f"#{club}"


def _competition_hashtags(text: str) -> tuple[str, ...]:
    return tuple(
        f"#{tag}"
        for pattern, tag in _COMPETITION_HASHTAGS
        if pattern.search(text)
    )


def relevant_hashtags(
    topic_text: str,
    *,
    canonical_text: str | None = None,
    limit: int = 2,
) -> tuple[str, ...]:
    """Return up to two factual entity tags for every taggable football post.

    Committed transfers retain the strict surname/destination contract. Other
    stories use only people, clubs and competitions explicitly present in the
    approved X copy or its source topic. Generic discovery tags are excluded.
    """

    if limit < 1:
        return ()
    committed = transfer_hashtags(topic_text, canonical_text=canonical_text)
    if committed:
        return committed[:limit]

    preferred_text = canonical_text or topic_text
    texts = (
        (_ascii_entity_text(preferred_text.strip()),)
        if preferred_text and preferred_text.strip() and _is_english_copy(preferred_text)
        else ()
    )
    people: list[str] = []
    clubs: list[str] = []
    competitions: list[str] = []
    named_entities: list[str] = []
    for value in texts:
        found_people, found_clubs = extract_hashtag_entities(value)
        for person in found_people:
            tag = _surname_hashtag(person)
            if tag and tag.casefold() not in {item.casefold() for item in people}:
                people.append(tag)
        for club_id in found_clubs:
            tag = _club_hashtag(club_id)
            if tag and tag.casefold() not in {item.casefold() for item in clubs}:
                clubs.append(tag)
        for tag in _competition_hashtags(value):
            clean = tag.removeprefix("#")
            if clean.casefold() not in {item.casefold() for item in competitions}:
                competitions.append(clean)
        for pattern, tag in _NAMED_ENTITY_HASHTAGS:
            if pattern.search(value) and tag.casefold() not in {
                item.casefold() for item in named_entities
            }:
                named_entities.append(tag)

    # The approved English copy is authoritative for display spelling, while
    # the source topic can safely recover a club/competition that translation
    # misspelled or abbreviated. Do not take person spellings from this path.
    if (
        texts
        and (people or clubs)
        and topic_text
        and topic_text.strip() != preferred_text.strip()
    ):
        _, source_clubs = extract_hashtag_entities(topic_text)
        for club_id in source_clubs:
            tag = _club_hashtag(club_id)
            if tag and tag.casefold() not in {item.casefold() for item in clubs}:
                clubs.append(tag)
        for tag in _competition_hashtags(_ascii_entity_text(topic_text)):
            clean = tag.removeprefix("#")
            if clean.casefold() not in {item.casefold() for item in competitions}:
                competitions.append(clean)

    selected: list[str] = []
    if people:
        selected.append(people[0])
    if clubs:
        selected.append(clubs[0])
    for pool in (named_entities, clubs[1:], competitions, people[1:]):
        for tag in pool:
            if len(selected) >= limit:
                break
            if tag.casefold() not in {item.casefold() for item in selected}:
                selected.append(tag)
        if len(selected) >= limit:
            break
    return tuple(f"#{tag}" for tag in selected[:limit])


def compact_for_x(
    text: str,
    max_chars: int = 275,
    *,
    topic_text: str | None = None,
) -> str:
    """Create a factual X version with source-derived entity hashtags."""

    cleaned = _clean(text)
    tags = relevant_hashtags(topic_text or cleaned, canonical_text=cleaned)
    suffix = f"\n\n{' '.join(tags)}" if tags else ""
    body_limit = max(1, max_chars - len(suffix))
    body = _compact_body(cleaned, body_limit)[:body_limit]
    return (body + suffix)[:max_chars]
