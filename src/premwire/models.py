from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class IngestResult(str, Enum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicate"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    platform: str
    name: str
    handle: str
    url: str
    enabled: bool = True
    include_keywords: tuple[str, ...] = ()
    media_reuse: str = "blocked"
    media_author: str = ""
    media_license: str = ""
    media_license_url: str = ""
    media_reuse_basis: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "include_keywords", tuple(self.include_keywords))
        reuse = self.media_reuse.strip().casefold()
        allowed = {"blocked", "owned", "permission_granted", "licensed"}
        if reuse not in allowed:
            raise ValueError(f"Unsupported media_reuse policy: {self.media_reuse!r}")
        if reuse != "blocked" and not self.media_reuse_basis.strip():
            raise ValueError("media_reuse_basis is required when source media is enabled")
        if reuse == "licensed" and not (
            self.media_author.strip()
            and self.media_license.strip()
            and self.media_license_url.strip()
        ):
            raise ValueError(
                "Licensed source media requires author, license and license URL"
            )
        object.__setattr__(self, "media_reuse", reuse)


@dataclass(frozen=True, slots=True)
class IncomingItem:
    source_id: str
    external_id: str
    text: str
    canonical_url: str
    published_at: datetime
    media_url: str | None = None
    source_collected_at: str = ''
    first_seen_at: str = ''
    last_seen_at: str = ''

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id is required")
        if not self.external_id.strip():
            raise ValueError("external_id is required")
        if not self.text.strip():
            raise ValueError("text is required")
        object.__setattr__(self, "published_at", as_utc(self.published_at))
        for key in ('source_collected_at', 'first_seen_at', 'last_seen_at'):
            value = getattr(self, key)
            if value:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is None:
                    raise ValueError('Aware collection timestamp required')
                object.__setattr__(self, key, stamp.astimezone(timezone.utc).isoformat())
        media_url = (self.media_url or "").strip()
        object.__setattr__(self, "media_url", media_url or None)


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    item_id: int
    result: IngestResult
    duplicate_of: int | None = None
