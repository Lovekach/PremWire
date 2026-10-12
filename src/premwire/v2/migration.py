from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .database import V2Database
from .models import OwnerActionType, PublicationStatus, SourceEventInput, StoryState
from .repositories import (
    MediaAssetRepository,
    OwnerActionRepository,
    PublicationRepository,
    ShadowComparisonRepository,
    SourceEventRepository,
    StoryRepository,
    StoryVersionRepository,
    TransitionRepository,
)
from .models import StoryVersionInput
from .schema import utc_iso


PROJECT_NAMESPACE = uuid.UUID("522ee550-1c44-5f49-9825-46e895eb8814")
LEGACY_TABLES = ("items", "events", "source_state", "runtime_state")


@dataclass(frozen=True, slots=True)
class MigrationReport:
    total_legacy_items: int
    imported_items: int
    already_imported_items: int
    failed_items: int
    stories: int
    source_events: int
    story_sources: int
    story_versions: int
    media_assets: int
    publications: int
    owner_actions: int
    shadow_comparisons: int
    migration_warnings: int
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def legacy_story_id(legacy_item_id: int) -> str:
    return str(uuid.uuid5(PROJECT_NAMESPACE, f"legacy-item:{legacy_item_id}"))


def table_fingerprint(db: sqlite3.Connection, table: str) -> dict[str, object]:
    if not table or '\x00' in table:
        raise ValueError('INVALID_TABLE_IDENTIFIER')
    identifier = '"' + table.replace('"', '""') + '"'
    rows = db.execute(f"SELECT * FROM {identifier} ORDER BY rowid").fetchall()
    serializable = [tuple(row) for row in rows]
    payload = json.dumps(
        serializable, ensure_ascii=False, default=str, separators=(",", ":")
    ).encode("utf-8")
    return {"count": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}


def legacy_fingerprints(db: sqlite3.Connection) -> dict[str, dict[str, object]]:
    return {table: table_fingerprint(db, table) for table in LEGACY_TABLES}


def _platform(source_id: str) -> str:
    folded = source_id.casefold()
    if folded.startswith("telegram"):
        return "telegram"
    if folded.startswith("x_"):
        return "x"
    return folded.split("_", 1)[0] or "unknown"


def _mapped_state(row: sqlite3.Row) -> tuple[StoryState, tuple[str, ...], str]:
    legacy_state = str(row["state"])
    if legacy_state == "published" and row["telegram_message_id"] is not None:
        return StoryState.PUBLISHED_TELEGRAM, (), "EXACT"
    if legacy_state == "cancelled_editor_deleted":
        return (
            StoryState.BLOCKED_OWNER_SUPPRESSED,
            ("OWNER_DELETION_IMPORTED",),
            "EXACT",
        )
    if legacy_state == "duplicate":
        return StoryState.DISCOVERED, ("LEGACY_DUPLICATE_LINKED",), "SAFE_FALLBACK"
    if legacy_state in {"queued", "scheduled", "baseline"}:
        return StoryState.DISCOVERED, (), "SAFE_INITIAL"
    warning = f"AMBIGUOUS_LEGACY_STATE:{legacy_state}"
    return StoryState.DISCOVERED, (warning,), "SAFE_FALLBACK"


class LegacyMigrationService:
    """Additively imports legacy rows without updating any legacy table."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database = V2Database(self.database_path)

    def migrate_all(self) -> MigrationReport:
        self.database.initialise()
        with closing(self.database.connect()) as read_db:
            read_db.row_factory = sqlite3.Row
            items = read_db.execute("SELECT * FROM items ORDER BY id").fetchall()
            rows_by_id = {int(row["id"]): row for row in items}

        imported = 0
        existing = 0
        failures: list[str] = []
        warning_total = 0
        for row in items:
            item_id = int(row["id"])
            try:
                created, warnings = self._migrate_item(row, rows_by_id)
                imported += int(created)
                existing += int(not created)
                warning_total += len(warnings)
            except Exception as exc:
                failures.append(f"{item_id}:{type(exc).__name__}:{exc}")

        counts = self._v2_counts()
        return MigrationReport(
            total_legacy_items=len(items),
            imported_items=imported,
            already_imported_items=existing,
            failed_items=len(failures),
            stories=counts["stories"],
            source_events=counts["source_events"],
            story_sources=counts["story_sources"],
            story_versions=counts["story_versions"],
            media_assets=counts["media_assets"],
            publications=counts["publications"],
            owner_actions=counts["owner_actions"],
            shadow_comparisons=counts["shadow_comparisons"],
            migration_warnings=warning_total,
            failures=tuple(failures),
        )

    def _canonical_row(
        self, row: sqlite3.Row, rows_by_id: dict[int, sqlite3.Row]
    ) -> sqlite3.Row:
        seen: set[int] = set()
        current = row
        while current["duplicate_of"] is not None:
            current_id = int(current["id"])
            if current_id in seen:
                return row
            seen.add(current_id)
            parent = rows_by_id.get(int(current["duplicate_of"]))
            if parent is None:
                return row
            current = parent
        return current

    def _migrate_item(
        self, row: sqlite3.Row, rows_by_id: dict[int, sqlite3.Row]
    ) -> tuple[bool, tuple[str, ...]]:
        item_id = int(row["id"])
        canonical = self._canonical_row(row, rows_by_id)
        canonical_id = int(canonical["id"])
        story_id = legacy_story_id(canonical_id)
        mapped_state, warnings, mapping_result = _mapped_state(row)

        with self.database.transaction() as db:
            existing_shadow = db.execute(
                "SELECT 1 FROM shadow_comparisons WHERE legacy_item_id = ?",
                (item_id,),
            ).fetchone()
            if existing_shadow is not None:
                return False, ()

            story_was_created = self._ensure_story(
                db, story_id, canonical, mapped_state if item_id == canonical_id else None
            )
            source_event_id = self._ensure_source_event(db, row)
            sources = SourceEventRepository(db)
            if sources.story_id_for_event(source_event_id) is None:
                StoryRepository(db).link_source(
                    story_id,
                    source_event_id,
                    relation_type=(
                        "PRIMARY" if item_id == canonical_id else "DUPLICATE_OF"
                    ),
                    is_primary=item_id == canonical_id,
                )

            self._ensure_version(db, story_id, canonical)
            self._ensure_media(db, story_id, canonical)
            self._ensure_publications(db, story_id, canonical)
            self._ensure_owner_actions(db, story_id, row)

            ShadowComparisonRepository(db).create_or_get(
                legacy_item_id=item_id,
                story_id=story_id,
                legacy_state=str(row["state"]),
                mapped_v2_state=mapped_state,
                mapping_result=mapping_result,
                warning_codes=warnings,
            )
            return True, warnings

    def _ensure_story(
        self,
        db: sqlite3.Connection,
        story_id: str,
        canonical: sqlite3.Row,
        mapped_state: StoryState | None,
    ) -> bool:
        stories = StoryRepository(db)
        try:
            stories.get(story_id)
            return False
        except KeyError:
            initial_state, _, _ = _mapped_state(canonical)
            state = mapped_state or initial_state
            stories.create(
                story_id,
                state,
                dedupe_key=str(canonical["dedupe_hash"]),
                dedupe_algorithm_version=1,
                legacy_item_id=int(canonical["id"]),
                legacy_state=str(canonical["state"]),
                blocked_reason=(
                    "OWNER_SUPPRESSED"
                    if state is StoryState.BLOCKED_OWNER_SUPPRESSED
                    else None
                ),
            )
            TransitionRepository(db).add(
                story_id,
                None,
                state,
                reason_code="LEGACY_IMPORT",
                metadata={"legacy_item_id": int(canonical["id"])},
            )
            return True

    def _ensure_source_event(self, db: sqlite3.Connection, row: sqlite3.Row) -> int:
        legacy_events = db.execute(
            "SELECT id, event_type, created_at, payload_json FROM events "
            "WHERE item_id = ? ORDER BY id",
            (int(row["id"]),),
        ).fetchall()
        raw_payload = {
            "legacy_item_id": int(row["id"]),
            "legacy_events": [dict(event) for event in legacy_events],
        }
        event_id, _ = SourceEventRepository(db).create_or_get(
            SourceEventInput(
                platform=_platform(str(row["source_id"])),
                source_account=str(row["source_id"]),
                external_message_id=str(row["external_id"]),
                source_url=str(row["canonical_url"]),
                original_text=str(row["source_text"]),
                original_media_url=row["media_url"],
                published_at=str(row["source_published_at"]),
                collected_at=str(row["discovered_at"]),
                content_hash=str(row["content_hash"]),
                raw_payload_json=json.dumps(raw_payload, ensure_ascii=False),
                legacy_event_id=(
                    int(legacy_events[0]["id"]) if legacy_events else None
                ),
            )
        )
        return event_id

    def _ensure_version(
        self, db: sqlite3.Connection, story_id: str, canonical: sqlite3.Row
    ) -> None:
        exists = db.execute(
            "SELECT 1 FROM story_versions WHERE story_id = ? AND version = 1",
            (story_id,),
        ).fetchone()
        if exists is not None:
            return
        StoryVersionRepository(db).create(
            StoryVersionInput(
                story_id=story_id,
                version=1,
                source_text_snapshot=str(canonical["source_text"]),
                editorial_text=canonical["draft_text"],
                primary_title=canonical["card_headline"],
                secondary_title=canonical["card_secondary"],
                generated_by="legacy_import",
            )
        )

    def _ensure_media(
        self, db: sqlite3.Connection, story_id: str, canonical: sqlite3.Row
    ) -> None:
        if not canonical["image_path"] and not canonical["media_url"]:
            return
        exists = db.execute(
            "SELECT 1 FROM media_assets WHERE story_id = ? AND story_version = 1",
            (story_id,),
        ).fetchone()
        if exists is not None:
            return
        MediaAssetRepository(db).create(
            story_id=story_id,
            story_version=1,
            asset_type="LEGACY_IMAGE",
            source_url=canonical["media_url"],
            local_path=canonical["image_path"],
            provenance="legacy_import",
            validation_status="LEGACY_UNVERIFIED",
        )

    def _ensure_publications(
        self, db: sqlite3.Connection, story_id: str, canonical: sqlite3.Row
    ) -> None:
        publications = PublicationRepository(db)
        if canonical["telegram_message_id"] is not None:
            publications.create_or_get(
                story_id=story_id,
                content_version=1,
                channel="telegram",
                idempotency_key=f"telegram:{story_id}:1",
                status=PublicationStatus.PUBLISHED.value,
                remote_message_id=str(canonical["telegram_message_id"]),
                published_at=canonical["reviewed_at"],
            )
        if canonical["x_status"] or canonical["x_post_url"]:
            status = (
                PublicationStatus.PUBLISHED.value
                if str(canonical["x_status"]).casefold() == "published"
                else PublicationStatus.DISABLED.value
            )
            publications.create_or_get(
                story_id=story_id,
                content_version=1,
                channel="x",
                idempotency_key=f"x:{story_id}:1",
                status=status,
                remote_message_id=canonical["x_post_url"],
                published_at=canonical["x_published_at"],
            )

    def _ensure_owner_actions(
        self, db: sqlite3.Connection, story_id: str, row: sqlite3.Row
    ) -> None:
        if str(row["state"]) != "cancelled_editor_deleted":
            return
        existing = db.execute(
            "SELECT 1 FROM owner_actions WHERE story_id = ? "
            "AND action_type = 'SUPPRESS_REPUBLICATION'",
            (story_id,),
        ).fetchone()
        if existing is not None:
            return
        actions = OwnerActionRepository(db)
        actions.add(
            story_id,
            OwnerActionType.DELETED_BY_OWNER,
            reason="Imported from cancelled_editor_deleted",
            remote_message_id=(
                str(row["telegram_message_id"])
                if row["telegram_message_id"] is not None
                else None
            ),
        )
        actions.add(
            story_id,
            OwnerActionType.SUPPRESS_REPUBLICATION,
            reason="Legacy owner deletion permanently suppresses republication",
        )

    def _v2_counts(self) -> dict[str, int]:
        tables = (
            "stories",
            "source_events",
            "story_sources",
            "story_versions",
            "media_assets",
            "publications",
            "owner_actions",
            "shadow_comparisons",
        )
        with closing(self.database.connect()) as db:
            return {
                table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
