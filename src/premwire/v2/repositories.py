from __future__ import annotations

import json
import sqlite3
from typing import Any

from .models import OwnerActionType, SourceEventInput, StoryState, StoryVersionInput
from .schema import utc_iso


class SourceEventRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create_or_get(self, event: SourceEventInput) -> tuple[int, bool]:
        before = self.db.total_changes
        self.db.execute(
            """
            INSERT OR IGNORE INTO source_events(
                platform, source_account, external_message_id, source_url,
                original_text, original_media_url, published_at, collected_at,
                content_hash, raw_payload_json, legacy_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.platform,
                event.source_account,
                event.external_message_id,
                event.source_url,
                event.original_text,
                event.original_media_url,
                event.published_at,
                event.collected_at,
                event.content_hash,
                event.raw_payload_json,
                event.legacy_event_id,
                utc_iso(),
            ),
        )
        row = self.db.execute(
            """
            SELECT id FROM source_events
            WHERE platform = ? AND source_account = ? AND external_message_id = ?
            """,
            (event.platform, event.source_account, event.external_message_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("source_event insert could not be resolved")
        return int(row["id"]), self.db.total_changes > before

    def story_id_for_event(self, source_event_id: int) -> str | None:
        row = self.db.execute(
            "SELECT story_id FROM story_sources WHERE source_event_id = ?",
            (source_event_id,),
        ).fetchone()
        return None if row is None else str(row["story_id"])


class StoryRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create(
        self,
        story_id: str,
        initial_state: StoryState,
        *,
        dedupe_key: str | None = None,
        dedupe_algorithm_version: int = 1,
        legacy_item_id: int | None = None,
        legacy_state: str | None = None,
        blocked_reason: str | None = None,
    ) -> None:
        now = utc_iso()
        self.db.execute(
            """
            INSERT INTO stories(
                story_id, current_state, content_version, dedupe_key,
                dedupe_algorithm_version, legacy_item_id, legacy_state,
                blocked_reason, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                initial_state.value,
                dedupe_key,
                dedupe_algorithm_version,
                legacy_item_id,
                legacy_state,
                blocked_reason,
                now,
                now,
            ),
        )

    def get(self, story_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM stories WHERE story_id = ?", (story_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown story_id: {story_id}")
        return row

    def find_by_legacy_item(self, legacy_item_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM stories WHERE legacy_item_id = ?", (legacy_item_id,)
        ).fetchone()

    def link_source(
        self,
        story_id: str,
        source_event_id: int,
        *,
        relation_type: str,
        is_primary: bool,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO story_sources(
                story_id, source_event_id, relation_type, is_primary, linked_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (story_id, source_event_id, relation_type, int(is_primary), utc_iso()),
        )

    def set_state(
        self,
        story_id: str,
        state: StoryState,
        blocked_reason: str | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE stories
            SET current_state = ?, blocked_reason = ?, updated_at = ?
            WHERE story_id = ?
            """,
            (state.value, blocked_reason, utc_iso(), story_id),
        )

    def set_content_version(self, story_id: str, version: int) -> None:
        self.db.execute(
            "UPDATE stories SET content_version = MAX(content_version, ?), "
            "updated_at = ? WHERE story_id = ?",
            (version, utc_iso(), story_id),
        )


class StoryVersionRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create(self, value: StoryVersionInput) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO story_versions(
                story_id, version, source_text_snapshot, editorial_text,
                primary_title, secondary_title, structured_facts_json,
                facts_schema_version, generated_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.story_id,
                value.version,
                value.source_text_snapshot,
                value.editorial_text,
                value.primary_title,
                value.secondary_title,
                value.structured_facts_json,
                value.facts_schema_version,
                value.generated_by,
                utc_iso(),
            ),
        )
        StoryRepository(self.db).set_content_version(value.story_id, value.version)
        return int(cursor.lastrowid)


class MediaAssetRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create(
        self,
        *,
        story_id: str,
        story_version: int,
        asset_type: str,
        source_url: str | None,
        local_path: str | None,
        provenance: str,
        validation_status: str,
        validation_report_json: str = "{}",
        sha256: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO media_assets(
                story_id, story_version, asset_type, source_url, local_path,
                provenance, validation_status, validation_report_json, sha256,
                width, height, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                story_version,
                asset_type,
                source_url,
                local_path,
                provenance,
                validation_status,
                validation_report_json,
                sha256,
                width,
                height,
                utc_iso(),
            ),
        )
        return int(cursor.lastrowid)


class PublicationRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def get_by_key(self, key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM publications WHERE idempotency_key = ?", (key,)
        ).fetchone()

    def create_or_get(
        self,
        *,
        story_id: str,
        content_version: int,
        channel: str,
        idempotency_key: str,
        status: str,
        remote_message_id: str | None = None,
        published_at: str | None = None,
    ) -> tuple[sqlite3.Row, bool]:
        before = self.db.total_changes
        now = utc_iso()
        self.db.execute(
            """
            INSERT OR IGNORE INTO publications(
                story_id, content_version, channel, idempotency_key, status,
                remote_message_id, created_at, updated_at, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                content_version,
                channel,
                idempotency_key,
                status,
                remote_message_id,
                now,
                now,
                published_at,
            ),
        )
        row = self.get_by_key(idempotency_key)
        if row is None:
            raise RuntimeError("publication insert could not be resolved")
        return row, self.db.total_changes > before


class OwnerActionRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def add(
        self,
        story_id: str,
        action_type: OwnerActionType,
        *,
        reason: str | None = None,
        remote_message_id: str | None = None,
    ) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO owner_actions(
                story_id, action_type, reason, remote_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (story_id, action_type.value, reason, remote_message_id, utc_iso()),
        )
        return int(cursor.lastrowid)

    def is_suppressed(self, story_id: str) -> bool:
        row = self.db.execute(
            """
            SELECT action_type FROM owner_actions
            WHERE story_id = ?
              AND action_type IN ('SUPPRESS_REPUBLICATION', 'UNSUPPRESS_REPUBLICATION')
            ORDER BY id DESC LIMIT 1
            """,
            (story_id,),
        ).fetchone()
        return (
            row is not None
            and str(row["action_type"]) == OwnerActionType.SUPPRESS_REPUBLICATION.value
        )


class TransitionRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def add(
        self,
        story_id: str,
        from_state: StoryState | None,
        to_state: StoryState,
        *,
        reason_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO state_transitions(
                story_id, from_state, to_state, reason_code, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                None if from_state is None else from_state.value,
                to_state.value,
                reason_code,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                utc_iso(),
            ),
        )
        return int(cursor.lastrowid)


class ShadowComparisonRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create_or_get(
        self,
        *,
        legacy_item_id: int,
        story_id: str,
        legacy_state: str,
        mapped_v2_state: StoryState,
        mapping_result: str,
        warning_codes: tuple[str, ...],
    ) -> tuple[sqlite3.Row, bool]:
        before = self.db.total_changes
        self.db.execute(
            """
            INSERT OR IGNORE INTO shadow_comparisons(
                legacy_item_id, story_id, legacy_state, mapped_v2_state,
                mapping_result, warning_codes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_item_id,
                story_id,
                legacy_state,
                mapped_v2_state.value,
                mapping_result,
                json.dumps(warning_codes),
                utc_iso(),
            ),
        )
        row = self.db.execute(
            "SELECT * FROM shadow_comparisons WHERE legacy_item_id = ?",
            (legacy_item_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("shadow comparison insert could not be resolved")
        return row, self.db.total_changes > before
