from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StoryState(str, Enum):
    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    DEDUPED = "DEDUPED"
    FACTS_EXTRACTED = "FACTS_EXTRACTED"
    FACTS_VALIDATED = "FACTS_VALIDATED"
    COPY_READY = "COPY_READY"
    MEDIA_SEARCHING = "MEDIA_SEARCHING"
    MEDIA_READY = "MEDIA_READY"
    RENDERED = "RENDERED"
    QC_PASSED = "QC_PASSED"
    PUBLISHING_TELEGRAM = "PUBLISHING_TELEGRAM"
    PUBLISHED_TELEGRAM = "PUBLISHED_TELEGRAM"

    BLOCKED_UNSUPPORTED_CATEGORY = "BLOCKED_UNSUPPORTED_CATEGORY"
    BLOCKED_AMBIGUOUS_CATEGORY = "BLOCKED_AMBIGUOUS_CATEGORY"
    BLOCKED_FACTS_INVALID = "BLOCKED_FACTS_INVALID"
    BLOCKED_LLM_INVALID_OUTPUT = "BLOCKED_LLM_INVALID_OUTPUT"
    BLOCKED_MEDIA_WRONG_SUBJECT = "BLOCKED_MEDIA_WRONG_SUBJECT"
    BLOCKED_MEDIA_HAS_FOREIGN_TEXT = "BLOCKED_MEDIA_HAS_FOREIGN_TEXT"
    BLOCKED_MEDIA_HAS_FOREIGN_LOGO = "BLOCKED_MEDIA_HAS_FOREIGN_LOGO"
    BLOCKED_MEDIA_NO_VERIFIED_REPLACEMENT = "BLOCKED_MEDIA_NO_VERIFIED_REPLACEMENT"
    BLOCKED_TEXT_OVERFLOW = "BLOCKED_TEXT_OVERFLOW"
    BLOCKED_RENDER_INVALID = "BLOCKED_RENDER_INVALID"
    BLOCKED_TELEGRAM_PAYLOAD = "BLOCKED_TELEGRAM_PAYLOAD"
    BLOCKED_OWNER_SUPPRESSED = "BLOCKED_OWNER_SUPPRESSED"

    FAILED_TELEGRAM_RETRYABLE = "FAILED_TELEGRAM_RETRYABLE"
    FAILED_TELEGRAM_FINAL = "FAILED_TELEGRAM_FINAL"


class PublicationStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    DISABLED = "DISABLED"


class OwnerActionType(str, Enum):
    DELETE_REQUESTED = "DELETE_REQUESTED"
    DELETED_BY_OWNER = "DELETED_BY_OWNER"
    SUPPRESS_REPUBLICATION = "SUPPRESS_REPUBLICATION"
    UNSUPPRESS_REPUBLICATION = "UNSUPPRESS_REPUBLICATION"


class InvalidStateTransition(ValueError):
    """Raised when the V2 state machine rejects a transition."""


class OwnerSuppressedError(RuntimeError):
    """Raised when publication is requested for an owner-suppressed story."""


@dataclass(frozen=True, slots=True)
class SourceEventInput:
    platform: str
    source_account: str
    external_message_id: str
    source_url: str
    original_text: str
    original_media_url: str | None
    published_at: str
    collected_at: str
    content_hash: str
    raw_payload_json: str = "{}"
    legacy_event_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoryVersionInput:
    story_id: str
    version: int
    source_text_snapshot: str
    editorial_text: str | None = None
    primary_title: str | None = None
    secondary_title: str | None = None
    structured_facts_json: str | None = None
    facts_schema_version: int | None = None
    generated_by: str = "legacy_import"


class LlmTaskType(str, Enum):
    FACT_EXTRACTION = "FACT_EXTRACTION"
    EDITORIAL_COPY_RU = "EDITORIAL_COPY_RU"
    CARD_HEADLINE_RU = "CARD_HEADLINE_RU"
    TRANSLATION_EN = "TRANSLATION_EN"
    JSON_REPAIR = "JSON_REPAIR"
