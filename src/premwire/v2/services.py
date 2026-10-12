from __future__ import annotations

import json
import uuid

from .database import V2Database
from .models import (
    InvalidStateTransition,
    OwnerActionType,
    OwnerSuppressedError,
    PublicationStatus,
    SourceEventInput,
    StoryState,
)
from .repositories import (
    OwnerActionRepository,
    PublicationRepository,
    SourceEventRepository,
    StoryRepository,
    TransitionRepository,
)


BLOCKED_STATES = frozenset(
    state for state in StoryState if state.value.startswith("BLOCKED_")
)

ALLOWED_TRANSITIONS: dict[StoryState, frozenset[StoryState]] = {
    StoryState.DISCOVERED: frozenset(
        {
            StoryState.NORMALIZED,
            StoryState.BLOCKED_UNSUPPORTED_CATEGORY,
            StoryState.BLOCKED_AMBIGUOUS_CATEGORY,
            StoryState.BLOCKED_OWNER_SUPPRESSED,
        }
    ),
    StoryState.NORMALIZED: frozenset(
        {
            StoryState.DEDUPED,
            StoryState.BLOCKED_AMBIGUOUS_CATEGORY,
            StoryState.BLOCKED_OWNER_SUPPRESSED,
        }
    ),
    StoryState.DEDUPED: frozenset(
        {
            StoryState.FACTS_EXTRACTED,
            StoryState.BLOCKED_AMBIGUOUS_CATEGORY,
            StoryState.BLOCKED_OWNER_SUPPRESSED,
        }
    ),
    StoryState.FACTS_EXTRACTED: frozenset(
        {StoryState.FACTS_VALIDATED, StoryState.BLOCKED_FACTS_INVALID}
    ),
    StoryState.FACTS_VALIDATED: frozenset(
        {StoryState.COPY_READY, StoryState.BLOCKED_LLM_INVALID_OUTPUT}
    ),
    StoryState.COPY_READY: frozenset(
        {
            StoryState.MEDIA_SEARCHING,
            StoryState.BLOCKED_TEXT_OVERFLOW,
            StoryState.BLOCKED_OWNER_SUPPRESSED,
        }
    ),
    StoryState.MEDIA_SEARCHING: frozenset(
        {
            StoryState.MEDIA_READY,
            StoryState.BLOCKED_MEDIA_WRONG_SUBJECT,
            StoryState.BLOCKED_MEDIA_HAS_FOREIGN_TEXT,
            StoryState.BLOCKED_MEDIA_HAS_FOREIGN_LOGO,
            StoryState.BLOCKED_MEDIA_NO_VERIFIED_REPLACEMENT,
        }
    ),
    StoryState.MEDIA_READY: frozenset(
        {
            StoryState.RENDERED,
            StoryState.BLOCKED_TEXT_OVERFLOW,
            StoryState.BLOCKED_RENDER_INVALID,
        }
    ),
    StoryState.RENDERED: frozenset(
        {
            StoryState.QC_PASSED,
            StoryState.BLOCKED_MEDIA_WRONG_SUBJECT,
            StoryState.BLOCKED_MEDIA_HAS_FOREIGN_TEXT,
            StoryState.BLOCKED_MEDIA_HAS_FOREIGN_LOGO,
            StoryState.BLOCKED_TEXT_OVERFLOW,
            StoryState.BLOCKED_RENDER_INVALID,
        }
    ),
    StoryState.QC_PASSED: frozenset(
        {
            StoryState.PUBLISHING_TELEGRAM,
            StoryState.BLOCKED_TELEGRAM_PAYLOAD,
            StoryState.BLOCKED_OWNER_SUPPRESSED,
        }
    ),
    StoryState.PUBLISHING_TELEGRAM: frozenset(
        {
            StoryState.PUBLISHED_TELEGRAM,
            StoryState.FAILED_TELEGRAM_RETRYABLE,
            StoryState.FAILED_TELEGRAM_FINAL,
        }
    ),
    StoryState.FAILED_TELEGRAM_RETRYABLE: frozenset(
        {StoryState.PUBLISHING_TELEGRAM, StoryState.FAILED_TELEGRAM_FINAL}
    ),
    StoryState.BLOCKED_OWNER_SUPPRESSED: frozenset({StoryState.DISCOVERED}),
}


class StoryIngestService:
    def __init__(self, database: V2Database) -> None:
        self.database = database

    def ingest(
        self,
        event: SourceEventInput,
        *,
        story_id: str | None = None,
        dedupe_key: str | None = None,
        relation_type: str = "PRIMARY",
    ) -> tuple[str, int, bool]:
        with self.database.transaction() as db:
            events = SourceEventRepository(db)
            event_id, event_created = events.create_or_get(event)
            existing_story = events.story_id_for_event(event_id)
            if existing_story is not None:
                return existing_story, event_id, False

            stories = StoryRepository(db)
            target_story_id = story_id or str(uuid.uuid4())
            try:
                stories.get(target_story_id)
                story_created = False
            except KeyError:
                stories.create(
                    target_story_id,
                    StoryState.DISCOVERED,
                    dedupe_key=dedupe_key,
                )
                TransitionRepository(db).add(
                    target_story_id,
                    None,
                    StoryState.DISCOVERED,
                    reason_code="INGESTED",
                )
                story_created = True
            has_primary = db.execute(
                "SELECT 1 FROM story_sources WHERE story_id = ? AND is_primary = 1",
                (target_story_id,),
            ).fetchone()
            stories.link_source(
                target_story_id,
                event_id,
                relation_type=relation_type,
                is_primary=story_created or has_primary is None,
            )
            return target_story_id, event_id, event_created


class StoryStateService:
    def __init__(self, database: V2Database) -> None:
        self.database = database

    def transition(
        self,
        story_id: str,
        to_state: StoryState,
        *,
        reason_code: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        with self.database.transaction() as db:
            stories = StoryRepository(db)
            row = stories.get(story_id)
            current = StoryState(str(row["current_state"]))
            if current == to_state:
                return False
            allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
            if to_state not in allowed:
                raise InvalidStateTransition(
                    f"Transition {current.value} -> {to_state.value} is not allowed"
                )
            if (
                to_state
                in {StoryState.PUBLISHING_TELEGRAM, StoryState.PUBLISHED_TELEGRAM}
                and OwnerActionRepository(db).is_suppressed(story_id)
            ):
                raise OwnerSuppressedError(f"Story {story_id} is owner-suppressed")
            blocked_reason = reason_code if to_state in BLOCKED_STATES else None
            TransitionRepository(db).add(
                story_id,
                current,
                to_state,
                reason_code=reason_code,
                metadata=metadata,
            )
            stories.set_state(story_id, to_state, blocked_reason)
            return True


class PublicationIdempotencyService:
    def __init__(self, database: V2Database) -> None:
        self.database = database

    @staticmethod
    def key(channel: str, story_id: str, content_version: int) -> str:
        return f"{channel}:{story_id}:{content_version}"

    def reserve(
        self,
        story_id: str,
        content_version: int,
        channel: str,
        *,
        enabled: bool = True,
    ) -> tuple[int, bool, str]:
        channel_key = channel.casefold()
        with self.database.transaction() as db:
            if OwnerActionRepository(db).is_suppressed(story_id):
                raise OwnerSuppressedError(f"Story {story_id} is owner-suppressed")
            key = self.key(channel_key, story_id, content_version)
            row, created = PublicationRepository(db).create_or_get(
                story_id=story_id,
                content_version=content_version,
                channel=channel_key,
                idempotency_key=key,
                status=(
                    PublicationStatus.PENDING.value
                    if enabled
                    else PublicationStatus.DISABLED.value
                ),
            )
            return int(row["id"]), created, str(row["status"])


class OwnerActionService:
    def __init__(self, database: V2Database) -> None:
        self.database = database

    def record(
        self,
        story_id: str,
        action_type: OwnerActionType,
        *,
        reason: str | None = None,
        remote_message_id: str | None = None,
    ) -> int:
        with self.database.transaction() as db:
            action_id = OwnerActionRepository(db).add(
                story_id,
                action_type,
                reason=reason,
                remote_message_id=remote_message_id,
            )
            if action_type is OwnerActionType.SUPPRESS_REPUBLICATION:
                stories = StoryRepository(db)
                current = StoryState(str(stories.get(story_id)["current_state"]))
                if current is not StoryState.BLOCKED_OWNER_SUPPRESSED:
                    TransitionRepository(db).add(
                        story_id,
                        current,
                        StoryState.BLOCKED_OWNER_SUPPRESSED,
                        reason_code="OWNER_SUPPRESSED",
                    )
                    stories.set_state(
                        story_id,
                        StoryState.BLOCKED_OWNER_SUPPRESSED,
                        "OWNER_SUPPRESSED",
                    )
            elif action_type is OwnerActionType.UNSUPPRESS_REPUBLICATION:
                stories = StoryRepository(db)
                current = StoryState(str(stories.get(story_id)["current_state"]))
                if current is StoryState.BLOCKED_OWNER_SUPPRESSED:
                    TransitionRepository(db).add(
                        story_id,
                        current,
                        StoryState.DISCOVERED,
                        reason_code="OWNER_UNSUPPRESSED",
                    )
                    stories.set_state(story_id, StoryState.DISCOVERED)
            return action_id
