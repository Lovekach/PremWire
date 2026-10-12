from __future__ import annotations

from datetime import datetime, timedelta

from .config import AppConfig


def _window_bounds(moment: datetime, config: AppConfig) -> tuple[datetime, datetime]:
    local = moment.astimezone(config.timezone)
    start = local.replace(
        hour=config.active_start.hour,
        minute=config.active_start.minute,
        second=0,
        microsecond=0,
    )
    end = local.replace(
        hour=config.active_end.hour,
        minute=config.active_end.minute,
        second=0,
        microsecond=0,
    )
    return start, end


def next_slot(
    now: datetime,
    config: AppConfig,
    previous_slot: datetime | None = None,
    interval_minutes: int | None = None,
) -> datetime:
    local_now = now.astimezone(config.timezone)
    start, end = _window_bounds(local_now, config)
    interval = timedelta(
        minutes=(
            config.queue.interval_minutes
            if interval_minutes is None
            else interval_minutes
        )
    )

    candidate = local_now
    if previous_slot is not None:
        previous_local = previous_slot.astimezone(config.timezone)
        candidate = max(candidate, previous_local + interval)

    candidate_start, candidate_end = _window_bounds(candidate, config)
    if candidate < candidate_start:
        return candidate_start
    if candidate >= candidate_end:
        return (candidate_start + timedelta(days=1)).replace(
            hour=config.active_start.hour,
            minute=config.active_start.minute,
            second=0,
            microsecond=0,
        )
    return candidate


def schedule_backlog(
    now: datetime,
    config: AppConfig,
    item_ids: list[int],
    previous_slot: datetime | None = None,
) -> list[tuple[int, datetime]]:
    result: list[tuple[int, datetime]] = []
    last = previous_slot
    for item_id in item_ids:
        if last is None:
            interval_minutes = config.queue.interval_minutes
        else:
            minimum = config.queue.interval_min_minutes
            span = config.queue.interval_max_minutes - minimum + 1
            interval_minutes = minimum + ((item_id * 7) % span)
        slot = next_slot(now, config, last, interval_minutes)
        result.append((item_id, slot))
        last = slot
    return result
