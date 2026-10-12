from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AppConfig


ACTIVE_STATES = (
    "queued",
    "scheduled",
    "needs_update",
    "needs_photo",
    "needs_headline_review",
    "needs_editor_review",
    "ready_for_review",
    "ready_update_review",
    "approved",
    "approved_update",
)


@dataclass(frozen=True, slots=True)
class BacklogItem:
    id: int
    state: str
    source_published_at: str
    discovered_at: str
    telegram_published_at: str | None
    age_minutes: int
    cutoff_eligible: bool
    freshness_eligible: bool
    first_run_action: str


@dataclass(frozen=True, slots=True)
class BacklogReport:
    status: str
    counts: dict[str, int]
    items: tuple[BacklogItem, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "counts": self.counts,
            "items": [asdict(item) for item in self.items],
        }


def inspect_backlog(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> BacklogReport:
    """Inspect the live SQLite backlog through a read-only connection."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = config.production.launch_cutoff.astimezone(timezone.utc)
    expires_after = current - timedelta(minutes=config.production.freshness_minutes)
    database = config.storage.database.resolve()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        rows = connection.execute(
            "SELECT id,state,source_published_at,discovered_at,"
            "telegram_published_at,telegram_message_id,draft_text "
            f"FROM items WHERE state IN ({placeholders}) ORDER BY id",
            ACTIVE_STATES,
        ).fetchall()
    finally:
        connection.close()

    diagnostics: list[BacklogItem] = []
    counts: dict[str, int] = {}
    risky = False
    for row in rows:
        state = str(row["state"])
        counts[state] = counts.get(state, 0) + 1
        source_raw = str(row["source_published_at"] or row["discovered_at"])
        source_time = datetime.fromisoformat(source_raw).astimezone(timezone.utc)
        cutoff_eligible = source_time > cutoff
        freshness_eligible = source_time > expires_after
        unpublished = row["telegram_message_id"] is None
        if unpublished and not cutoff_eligible:
            action = "SUPPRESS_CUTOFF"
        elif unpublished and not freshness_eligible:
            action = "SUPPRESS_STALE"
        elif state in {"queued", "scheduled", "approved", "approved_update"}:
            action = "ELIGIBLE_FOR_PIPELINE"
            risky = True
        else:
            action = "INERT_IN_TEXT_PRODUCTION"
        diagnostics.append(
            BacklogItem(
                id=int(row["id"]),
                state=state,
                source_published_at=str(row["source_published_at"]),
                discovered_at=str(row["discovered_at"]),
                telegram_published_at=(
                    str(row["telegram_published_at"])
                    if row["telegram_published_at"] is not None
                    else None
                ),
                age_minutes=max(0, int((current - source_time).total_seconds() // 60)),
                cutoff_eligible=cutoff_eligible,
                freshness_eligible=freshness_eligible,
                first_run_action=action,
            )
        )
    return BacklogReport("BACKLOG_RISK" if risky else "SAFE", counts, tuple(diagnostics))


__all__ = ["ACTIVE_STATES", "BacklogItem", "BacklogReport", "inspect_backlog"]
