from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone

from .collectors.base import CollectionBatch, CollectorError
from .collectors.telegram_public import TelegramPublicCollector
from .collectors.x_api import XApiCollector
from .collectors.x_rss import XFreeRssCollector
from .channel_audit import audit_deleted_editor_posts
from .config import AppConfig
from .content_filter import AdvertisementFilter, SourceQualityFilter
from .editorial import DraftPacket, build_editor_prompt
from .models import IngestResult
from .news_priority import stale_low_priority
from .publisher import process_publishing
from .scheduler import schedule_backlog
from .storage import Storage
from .text_editor import EditorError, OllamaTextEditor
from .x_mirror import process_x_mirroring
from .v2.shadow import ShadowComparisonService


@dataclass(frozen=True, slots=True)
class RunSummary:
    collected: int
    new: int
    updated: int
    duplicates: int
    unchanged: int
    scheduled: int
    draft_packets: int
    baselined: int
    rejected_advertising: int
    generated_drafts: int
    prepared_posts: int
    published_posts: int
    updated_posts: int
    deleted_posts: int
    x_published_posts: int
    x_replaced_posts: int
    x_deleted_posts: int
    warnings: tuple[str, ...]


def _remove_daily_drafts(drafts_directory, item_ids: tuple[int, ...]) -> int:
    """Delete generated draft packets belonging to a cleared queue item."""
    removed = 0
    safe_root = drafts_directory.resolve()
    candidates = [drafts_directory / f"item-{item_id}.json" for item_id in item_ids]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(safe_root):
                continue
            if resolved.is_file():
                resolved.unlink()
                removed += 1
        except OSError:
            # The database record is already cleared; retrying is unnecessary.
            continue
    return removed


def collect_once(
    config: AppConfig,
    env: dict[str, str],
    *,
    x_since_id: str | None = None,
    platform: str | None = None,
) -> tuple[CollectionBatch, ...]:
    if platform not in {None, "telegram", "x"}:
        raise ValueError(f"Unsupported collector platform: {platform}")
    x_token = env.get("X_BEARER_TOKEN", "").strip()
    x_collector = (
        XApiCollector(config.sources, x_token, since_id=x_since_id)
        if x_token
        else XFreeRssCollector(config.sources)
    )
    collectors = []
    if platform in {None, "telegram"}:
        collectors.append(TelegramPublicCollector(config.sources))
    if platform in {None, "x"}:
        collectors.append(x_collector)
    batches: list[CollectionBatch] = []
    for collector in collectors:
        try:
            batches.append(collector.collect())
        except CollectorError as exc:
            batches.append(
                CollectionBatch(
                    collector=collector.__class__.__name__,
                    items=(),
                    warnings=(str(exc),),
                )
            )
    return tuple(batches)


def run_once(config: AppConfig, env: dict[str, str]) -> RunSummary:
    if config.production.mode == "daily_digest":
        raise RuntimeError("CONTINUOUS_PRODUCTION_DISABLED: use daily-digest")
    storage = Storage(config.storage.database)
    storage.initialise()
    warnings: list[str] = []
    if storage.get_runtime_state("x_mirror_sync_mode") != "telegram_immediate_v1":
        cleared_x = storage.skip_pending_x_backlog(
            "historical X backlog cleared; only new Telegram publications are mirrored"
        )
        storage.set_runtime_state("x_mirror_sync_mode", "telegram_immediate_v1")
        if cleared_x:
            warnings.append(
                f"X historical backlog cleared: {cleared_x} item(s)")
    if bool(getattr(config.queue, "daily_fresh_start", True)):
        local_now = datetime.now(config.timezone)
        today = local_now.date().isoformat()
        if storage.get_runtime_state("daily_fresh_start_day") != today:
            cutoff = datetime.combine(local_now.date(), time.min, tzinfo=config.timezone)
            item_ids = storage.clear_unpublished_backlog(
                cutoff.astimezone(timezone.utc)
            )
            removed_files = _remove_daily_drafts(config.storage.drafts_directory, item_ids)
            storage.clear_runtime_state("publication_drain")
            storage.set_runtime_state("daily_fresh_start_day", today)
            if item_ids or removed_files:
                warnings.append(
                    "Daily fresh start cleared "
                    f"{len(item_ids)} unpublished item(s) and {removed_files} material file(s)"
                )
    drain_ids, _ = storage.publication_drain()
    cutoff_raw = storage.get_runtime_state("fresh_start_cutoff")
    fresh_start_cutoff = datetime.fromisoformat(cutoff_raw) if cutoff_raw else None
    batches = collect_once(
        config,
        env,
        x_since_id=storage.get_runtime_state("x_since_id"),
    )
    counters = {result: 0 for result in IngestResult}
    collected = 0
    baselined = 0
    rejected_advertising = 0
    content_filter = AdvertisementFilter(config.filters)
    source_quality_filter = SourceQualityFilter()
    for batch in batches:
        warnings.extend(batch.warnings)
        source_ids = dict.fromkeys(item.source_id for item in batch.items)
        for source_id in source_ids:
            source_items = [item for item in batch.items if item.source_id == source_id]
            needs_baseline = (
                config.collection.initial_sync == "baseline"
                and not storage.source_is_initialised(source_id)
            )
            for item in source_items:
                collected += 1
                outcome = storage.ingest(item)
                counters[outcome.result] += 1
                if fresh_start_cutoff is not None:
                    storage.reject_unpublished_before_cutoff(
                        outcome.item_id, fresh_start_cutoff
                    )
                if outcome.result in {IngestResult.NEW, IngestResult.UPDATED}:
                    decision = content_filter.check(item.text)
                    if decision.blocked:
                        storage.mark_filtered(
                            outcome.item_id,
                            decision.reason or "advertising",
                            decision.evidence,
                        )
                        rejected_advertising += 1
                    else:
                        source_decision = source_quality_filter.check(item.text)
                        if source_decision.blocked:
                            storage.mark_low_information(
                                outcome.item_id,
                                source_decision.reason or "low_information",
                            )
            if needs_baseline and source_items:
                baselined += storage.complete_source_baseline(source_id)
                warnings.append(
                    f"Initial history stored without publishing: {source_id}"
                )
            if source_items and all(item.external_id.isdigit() for item in source_items):
                storage.reconcile_source_window(
                    source_id,
                    tuple(item.external_id for item in source_items),
                    delete_mirror=config.publishing.delete_when_source_deleted,
                )
        if batch.collector == "x":
            numeric_ids = [
                int(item.external_id)
                for item in batch.items
                if item.external_id.isdigit()
            ]
            if numeric_ids:
                storage.set_runtime_state("x_since_id", str(max(numeric_ids)))

    try:
        audit = audit_deleted_editor_posts(storage, config.publishing.channel)
        if audit.deleted_item_ids:
            warnings.append(
                "Posts deleted by editor permanently suppressed: "
                + ", ".join(str(value) for value in audit.deleted_item_ids)
            )
    except CollectorError as exc:
        warnings.append(f"Own channel audit skipped: {exc}")

    # Material collected after the laptop was offline can be many hours old.
    # Keep only stories with enough deterministic hype/transfer signals.
    now = datetime.now(timezone.utc)
    rejected_stale: list[int] = []
    for row in storage.stale_backlog_candidates():
        rejected, score = stale_low_priority(
            str(row["source_text"]),
            datetime.fromisoformat(str(row["source_published_at"])),
            now,
            stale_after_hours=config.queue.stale_backlog_hours,
            minimum_score=config.queue.stale_backlog_min_hype_score,
        )
        if rejected:
            item_id = int(row["id"])
            storage.mark_low_priority_backlog(item_id, score)
            rejected_stale.append(item_id)
    if rejected_stale:
        warnings.append(
            f"Stale low-priority backlog skipped: {len(rejected_stale)} item(s)"
        )

    collapsed_stories = storage.collapse_story_duplicates()
    if collapsed_stories:
        warnings.append(
            "Repeated stories suppressed: "
            + ", ".join(f"{item_id}->{original_id}" for item_id, original_id in collapsed_stories)
        )

    queued = storage.queued_items()
    assignments = schedule_backlog(
        datetime.now(timezone.utc),
        config,
        [int(row["id"]) for row in queued],
        None,
    )
    for item_id, scheduled_for in assignments:
        storage.set_schedule(item_id, scheduled_for)

    # Publish already-approved due material before the potentially slow local
    # editor/card pipeline. A large overnight backlog must not hold a finished
    # post for the entire duration of Ollama processing.
    publishing_before = process_publishing(config, env, storage)
    warnings.extend(publishing_before.warnings)
    published_total = publishing_before.published
    updated_total = publishing_before.updated
    deleted_total = publishing_before.deleted
    x_published_total = x_replaced_total = x_deleted_total = 0

    def mirror_telegram_output(publishing_result) -> None:
        nonlocal x_published_total, x_replaced_total, x_deleted_total
        if not (
            publishing_result.published
            or publishing_result.updated
            or publishing_result.deleted
        ):
            return
        result = process_x_mirroring(config, storage)
        warnings.extend(result.warnings)
        x_published_total += result.published
        x_replaced_total += result.replaced
        x_deleted_total += result.deleted

    mirror_telegram_output(publishing_before)

    def publish_ready() -> None:
        nonlocal published_total, updated_total, deleted_total
        result = process_publishing(config, env, storage)
        warnings.extend(result.warnings)
        published_total += result.published
        updated_total += result.updated
        deleted_total += result.deleted
        mirror_telegram_output(result)

    source_names = {source.id: source.name for source in config.sources}
    packet_count = 0
    editable_rows = (
        storage.items_with_state("scheduled")
        + storage.items_with_state("needs_update")
    )
    for row in editable_rows:
        packet = DraftPacket(
            item_id=int(row["id"]),
            source_text=str(row["source_text"]),
            source_url=str(row["canonical_url"]),
            source_name=source_names.get(str(row["source_id"]), str(row["source_id"])),
            prompt=build_editor_prompt(
                source_names.get(str(row["source_id"]), str(row["source_id"])),
                str(row["source_text"]),
            ),
        )
        path = config.storage.drafts_directory / f"item-{packet.item_id}.json"
        if not path.exists():
            packet.save(config.storage.drafts_directory)
            packet_count += 1

    generated_drafts = 0
    prepared_posts = 0
    attempted_posts: set[int] = set()

    def prepare_and_publish(item_id: int) -> None:
        nonlocal prepared_posts
        attempted_posts.add(item_id)
        try:
            storage.prepare_text_only(item_id)
            if config.publishing.auto_approve:
                storage.approve_item(item_id)
            prepared_posts += 1
            publish_ready()
        except Exception as exc:
            warnings.append(f"Post item {item_id} failed: {exc}")

    if config.editor.enabled:
        editor = OllamaTextEditor(config.editor)
        try:
            ready, message = editor.verify()
        except EditorError as exc:
            ready = False
            message = str(exc)
        if not ready:
            warnings.append(f"Text editor skipped: {message}")
        else:
            for row in storage.scheduled_without_draft()[: config.queue.max_generate_per_run]:
                item_id = int(row["id"])
                source_id = str(row["source_id"])
                try:
                    draft = editor.rewrite(
                        source_names.get(source_id, source_id),
                        str(row["source_text"]),
                    )
                    storage.save_draft(item_id, draft)
                    generated_drafts += 1
                    prepare_and_publish(item_id)
                except EditorError as exc:
                    storage.mark_editor_failed(item_id, str(exc))
                    if item_id in drain_ids:
                        storage.finish_publication_drain_item(item_id)
                    warnings.append(f"Draft item {item_id} failed: {exc}")
                finally:
                    # Keep draining material that was already approved while
                    # the local model works through a large transfer backlog.
                    publish_ready()

    pending_rows = storage.text_only_candidates()
    for row in pending_rows:
        item_id = int(row["id"])
        if item_id not in attempted_posts:
            prepare_and_publish(item_id)

    publishing_after = process_publishing(config, env, storage)
    warnings.extend(publishing_after.warnings)
    mirror_telegram_output(publishing_after)
    x_mirror = process_x_mirroring(config, storage)
    warnings.extend(x_mirror.warnings)
    if config.pipeline_v2.shadow_enabled:
        try:
            shadow = ShadowComparisonService(config.storage.database).run()
            if shadow.imported or shadow.failed:
                warnings.append(
                    "V2 shadow sync: "
                    f"{shadow.imported} imported, {shadow.failed} failed"
                )
        except Exception as exc:
            # Shadow mode must never change the result of the legacy pipeline.
            warnings.append(f"V2 shadow sync skipped: {exc}")

    return RunSummary(
        collected=collected,
        new=counters[IngestResult.NEW],
        updated=counters[IngestResult.UPDATED],
        duplicates=counters[IngestResult.DUPLICATE],
        unchanged=counters[IngestResult.UNCHANGED],
        scheduled=len(assignments),
        draft_packets=packet_count,
        baselined=baselined,
        rejected_advertising=rejected_advertising,
        generated_drafts=generated_drafts,
        prepared_posts=prepared_posts,
        published_posts=published_total + publishing_after.published,
        updated_posts=updated_total + publishing_after.updated,
        deleted_posts=deleted_total + publishing_after.deleted,
        x_published_posts=x_published_total + x_mirror.published,
        x_replaced_posts=x_replaced_total + x_mirror.replaced,
        x_deleted_posts=x_deleted_total + x_mirror.deleted,
        warnings=tuple(warnings),
    )
