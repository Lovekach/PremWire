from __future__ import annotations

import re
from dataclasses import dataclass

from .collectors.http import get_text
from .storage import Storage


@dataclass(frozen=True, slots=True)
class ChannelAuditResult:
    visible_posts: int
    deleted_item_ids: tuple[int, ...]


def visible_channel_post_ids(html: str, handle: str) -> tuple[int, ...]:
    escaped = re.escape(handle.strip().lstrip("@"))
    pattern = re.compile(rf'data-post=["\']{escaped}/(\d+)["\']', re.IGNORECASE)
    return tuple(sorted({int(value) for value in pattern.findall(html)}))


def audit_deleted_editor_posts(
    storage: Storage,
    channel: str,
) -> ChannelAuditResult:
    handle = channel.strip().lstrip("@")
    if not handle:
        return ChannelAuditResult(0, ())
    html = get_text(f"https://t.me/s/{handle}")
    visible = visible_channel_post_ids(html, handle)
    if not visible:
        return ChannelAuditResult(0, ())
    lower, upper = min(visible), max(visible)
    visible_set = set(visible)
    deleted: list[int] = []
    for row in storage.published_items():
        message_id = row["telegram_message_id"]
        if message_id is None:
            continue
        value = int(message_id)
        miss_key = f"channel_audit_missing:{handle.casefold()}:{value}"
        if value in visible_set:
            if storage.get_runtime_state(miss_key) not in {None, "0"}:
                storage.set_runtime_state(miss_key, "0")
            continue
        if value < lower:
            continue

        # A gap inside the visible range is conclusive. A missing newest post can
        # briefly be caused by Telegram page caching, so require two audits.
        conclusive_gap = value <= upper
        misses = 2 if conclusive_gap else int(storage.get_runtime_state(miss_key) or 0) + 1
        storage.set_runtime_state(miss_key, str(misses))
        if misses >= 2:
            item_id = int(row["id"])
            storage.mark_deleted_by_editor(item_id)
            deleted.append(item_id)
    return ChannelAuditResult(len(visible), tuple(deleted))
