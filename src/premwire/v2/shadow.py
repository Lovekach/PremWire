from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .migration import LegacyMigrationService


@dataclass(frozen=True, slots=True)
class ShadowRunReport:
    pending: int
    imported: int
    failed: int
    warnings: int
    failures: tuple[str, ...]


class ShadowComparisonService:
    """Shadow-writes legacy rows to V2 without invoking pipeline side effects."""

    def __init__(self, database_path: Path | str) -> None:
        self.migration = LegacyMigrationService(database_path)

    def run(self) -> ShadowRunReport:
        self.migration.database.initialise()
        with closing(self.migration.database.connect()) as db:
            db.row_factory = sqlite3.Row
            all_rows = db.execute("SELECT * FROM items ORDER BY id").fetchall()
            pending_rows = db.execute(
                """
                SELECT i.* FROM items i
                LEFT JOIN shadow_comparisons s ON s.legacy_item_id = i.id
                WHERE s.id IS NULL
                ORDER BY i.id
                """
            ).fetchall()
        rows_by_id = {int(row["id"]): row for row in all_rows}

        imported = 0
        warning_count = 0
        failures: list[str] = []
        for row in pending_rows:
            try:
                created, warnings = self.migration._migrate_item(row, rows_by_id)
                imported += int(created)
                warning_count += len(warnings)
            except Exception as exc:
                failures.append(f"{int(row['id'])}:{type(exc).__name__}:{exc}")
        return ShadowRunReport(
            pending=len(pending_rows),
            imported=imported,
            failed=len(failures),
            warnings=warning_count,
            failures=tuple(failures),
        )
