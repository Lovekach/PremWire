from __future__ import annotations

from datetime import datetime, timedelta, timezone


_CONFIRMED_TRANSFER = (
    "here we go", "official", "officially", "signed", "signing", "joins",
    "completed", "medical", "agreement reached", "deal agreed",
    "официально", "подписал", "подписала", "перешёл", "перешел",
    "сделка заверш", "соглашение достигнуто", "медосмотр",
)
_TRANSFER = (
    "transfer", "bid", "offer", "talks", "negotiations", "deal", "contract",
    "interest", "interested", "loan", "fee", "release clause",
    "трансфер", "переход", "предложение", "переговор", "контракт", "сделк",
    "интерес", "аренд", "отступн",
)
_HYPE = (
    "breaking", "exclusive", "final", "champion", "record", "million", "€", "£",
    "срочно", "эксклюзив", "финал", "чемпион", "рекорд", "млн",
)
_TOP_ENTITIES = (
    "arsenal", "chelsea", "liverpool", "manchester city", "manchester united",
    "real madrid", "barcelona", "bayern", "psg", "juventus", "inter", "milan",
    "арсенал", "челси", "ливерпуль", "манчестер", "реал", "барселон",
    "бавари", "псж", "ювентус", "милан",
)

_IMMEDIATE_MARKERS = (
    "here we go",
    "breaking",
    "срочно",
    "официально",
    "officially",
    "official",
)


def is_immediate_news(*texts: str, category: str = "") -> bool:
    """Return True only for a breaking or officially confirmed story.

    Ordinary transfer rumours remain in the regular priority queue. These
    markers are intentionally narrow because an immediate post bypasses both
    the normal interval and the hourly cap.
    """
    if category.casefold() in {"breaking", "official"}:
        return True
    combined = " ".join(value.casefold() for value in texts if value)
    return any(marker in combined for marker in _IMMEDIATE_MARKERS)


def hype_score(text: str) -> int:
    """Small deterministic gate for stale offline news; not a fact classifier."""
    folded = text.casefold()
    score = 0
    confirmed = any(marker in folded for marker in _CONFIRMED_TRANSFER)
    transfer = any(marker in folded for marker in _TRANSFER)
    if confirmed and transfer:
        score += 7
    elif transfer:
        score += 4
    if any(marker in folded for marker in _HYPE):
        score += 2
    if any(marker in folded for marker in _TOP_ENTITIES):
        score += 1
    if any(char.isdigit() for char in folded) and transfer:
        score += 1
    return score


def stale_low_priority(
    text: str,
    published_at: datetime,
    now: datetime,
    *,
    stale_after_hours: int,
    minimum_score: int,
) -> tuple[bool, int]:
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age = now.astimezone(timezone.utc) - published_at.astimezone(timezone.utc)
    score = hype_score(text)
    return age > timedelta(hours=stale_after_hours) and score < minimum_score, score
