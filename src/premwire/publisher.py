from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import AppConfig
from .content_filter import (
    routine_matchday_without_news,
    source_is_award_voting_promotion,
    source_has_live_broadcast_promotion,
    source_is_audience_commentary,
)
from .editorial_desk import (
    decision_from_row,
    fallback_decision,
    rank_candidates,
)
from .news_priority import is_immediate_news
from .storage import EventGateAction, Storage
from .telegram_api import TelegramApiError, TelegramBotApi
from .simple_editorial import sanitize_simple_publication_text
from .simple_factual_guard import check_simple_factual_safety
from .russian_editorial import strip_terminal_body_period
from .editorial_safety import source_problem, transient_ttl_minutes
from .simple_entity_hygiene import check_simple_entity_hygiene, check_source_local_output_quality


@dataclass(frozen=True, slots=True)
class PublishingSummary:
    published: int = 0
    updated: int = 0
    deleted: int = 0
    warnings: tuple[str, ...] = ()


def _assert_source_factual_safety(row, public_text: str) -> None:
    raw_source = str(row["source_text"] or "")
    problem = source_problem(raw_source)
    if problem:
        raise ValueError(f"SOURCE_POLICY_BLOCKED:{problem}")
    ttl = transient_ttl_minutes(raw_source)
    if ttl is not None and "source_published_at" in row.keys() and row["source_published_at"]:
        source_at = datetime.fromisoformat(str(row["source_published_at"])).astimezone(timezone.utc)
        if datetime.now(timezone.utc) - source_at > timedelta(minutes=ttl):
            raise ValueError("SOURCE_POLICY_BLOCKED:STALE_LIVE_UPDATE")
    source_text = str(row["clean_source_text"] or row["source_text"] or "")
    if source_has_live_broadcast_promotion(source_text):
        raise ValueError("SOURCE_POLICY_BLOCKED:live_broadcast_promotion")
    if source_is_award_voting_promotion(source_text):
        raise ValueError("SOURCE_POLICY_BLOCKED:award_voting_promotion")
    if source_is_audience_commentary(source_text):
        raise ValueError("SOURCE_POLICY_BLOCKED:audience_commentary_not_news")
    if routine_matchday_without_news(source_text):
        raise ValueError("SOURCE_POLICY_BLOCKED:routine_matchday_without_news")
    violations = (
        check_simple_factual_safety(source_text, public_text)
        + check_simple_entity_hygiene(public_text)
        + check_source_local_output_quality(source_text, public_text)
    )
    if violations:
        codes = ",".join(violation.code for violation in violations)
        raise ValueError(f"SIMPLE_EDITOR_FACTUAL_GUARD_FAILED:{codes}")


def process_visual_text_previews(
    config: AppConfig,
    env: dict[str, str],
    storage: Storage,
) -> PublishingSummary:
    """Publish visual-bound text now and leave its message open for media upgrade."""

    if getattr(getattr(config, "production", None), "mode", "continuous") == "daily_digest":
        return PublishingSummary(warnings=("DAILY_MODE_NO_INDIVIDUAL_POSTS",))

    imagegen = getattr(config, "imagegen_worker", None)
    if not bool(getattr(imagegen, "enabled", False)) or config.publishing.dry_run:
        return PublishingSummary()
    if storage.publication_is_paused():
        return PublishingSummary(warnings=("CEO_PUBLICATION_PAUSE",))
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return PublishingSummary(
            warnings=("Visual text publishing skipped: bot token is missing",)
        )
    now = datetime.now(timezone.utc)
    row = storage.visual_text_preview_due(
        now,
        int(config.queue.interval_minutes),
        int(config.queue.max_posts_per_hour),
    )
    if row is None:
        return PublishingSummary()
    item_id = int(row["id"])
    event_gate = storage.apply_event_identity_gate(item_id, now=now)
    if event_gate.action is not EventGateAction.ALLOW:
        return PublishingSummary(
            warnings=(
                f"EVENT_{event_gate.action.value} item={item_id} "
                f"reason={event_gate.reason} related={event_gate.related_item_id}",
            )
        )
    duplicate_of = storage.suppress_if_recent_published_duplicate(item_id)
    if duplicate_of is not None:
        return PublishingSummary(
            warnings=(
                f"PUBLISHED_STORY_DUPLICATE_SUPPRESSED item={item_id} "
                f"duplicate_of={duplicate_of}",
            )
        )
    source_name = next(
        (
            source.name
            for source in getattr(config, "sources", ())
            if source.id == str(row["source_id"])
        ),
        str(row["source_id"]),
    )
    try:
        public_text = sanitize_simple_publication_text(
            str(row["draft_text"] or ""),
            source_name=source_name,
            max_chars=config.editor.max_output_chars,
        )
        public_text = strip_terminal_body_period(public_text)
        _assert_source_factual_safety(row, public_text)
    except ValueError as exc:
        storage.mark_editor_failed(item_id, str(exc))
        return PublishingSummary(
            warnings=(f"Visual text item {item_id} blocked pre-send: {exc}",)
        )
    api = TelegramBotApi(token)
    try:
        message_id = api.send_text(
            config.publishing.channel,
            html.escape(public_text),
        )
        storage.mark_visual_text_published(item_id, message_id, public_text)
    except (TelegramApiError, ValueError) as exc:
        storage.mark_publish_failed(item_id, str(exc))
        return PublishingSummary(
            warnings=(f"Visual text item {item_id} failed: {exc}",)
        )

    warnings: list[str] = []
    if getattr(getattr(config, "vk_publishing", None), "enabled", False):
        try:
            storage.queue_vk_mirror(item_id, public_text)
        except ValueError as exc:
            warnings.append(f"VK early mirror item {item_id} failed to queue: {exc}")
    auto_reaction = str(getattr(config.publishing, "auto_reaction", "")).strip()
    if auto_reaction:
        try:
            api.set_message_reaction(
                config.publishing.channel,
                message_id,
                auto_reaction,
            )
        except TelegramApiError as exc:
            warnings.append(f"Reaction for item {item_id} failed: {exc}")
    return PublishingSummary(published=1, warnings=tuple(warnings))


def _delete_already_completed(error: Exception) -> bool:
    """Treat terminal Telegram delete responses as an idempotent delete.

    A user can remove a channel post manually, or Telegram can refuse deletion
    after its retention window. In both cases the pipeline has reached its
    desired end state and must not retry the same request forever.
    """
    text = str(error).casefold()
    return "deletemessage" in text and any(
        phrase in text
        for phrase in (
            "message to delete not found",
            "message not found",
            "message can't be deleted",
        )
    )


def process_publishing(
    config: AppConfig,
    env: dict[str, str],
    storage: Storage,
) -> PublishingSummary:
    if config.publishing.dry_run:
        return PublishingSummary()
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return PublishingSummary(warnings=("Telegram publishing skipped: bot token is missing",))
    api = TelegramBotApi(token)
    warnings: list[str] = []
    published = updated = deleted = 0

    for row in storage.items_with_state("needs_delete"):
        item_id = int(row["id"])
        message_id = row["telegram_message_id"]
        if message_id is None:
            warnings.append(f"Delete item {item_id} skipped: Telegram message id is missing")
            continue
        try:
            api.delete_message(config.publishing.channel, int(message_id))
            storage.mark_source_delete_done(item_id)
            deleted += 1
        except (TelegramApiError, ValueError) as exc:
            if _delete_already_completed(exc):
                # The post was removed outside this run (usually manually).
                # Its desired end state has already been reached, so do not
                # retry the same deletion on every monitor cycle.
                storage.mark_source_delete_done(item_id)
                deleted += 1
                warnings.append(
                    f"Delete item {item_id} already completed in Telegram; state synchronized"
                )
                continue
            storage.mark_publish_failed(item_id, str(exc))
            warnings.append(f"Delete item {item_id} failed: {exc}")

    # CEO pause blocks every NEW/UPDATE outbound Telegram action while keeping
    # deletion/tombstone reconciliation above operational. Approved items stay
    # queued and retain their original timestamps so freshness expires normally.
    if storage.publication_is_paused() or getattr(getattr(config, "production", None), "mode", "continuous") == "daily_digest":
        return PublishingSummary(
            deleted=deleted,
            warnings=("CEO_PUBLICATION_PAUSE",),
        )

    now = datetime.now(timezone.utc)
    production = getattr(config, "production", None)
    production_enabled = bool(getattr(production, "enabled", False))
    fresh_after = None
    if production_enabled:
        cutoff = production.launch_cutoff.astimezone(timezone.utc)
        fresh_after = max(
            cutoff,
            now - timedelta(minutes=int(production.freshness_minutes)),
        )
        storage.suppress_unpublished_before_cutoff(cutoff)
        storage.suppress_stale_production_items(
            cutoff,
            now - timedelta(minutes=int(production.freshness_minutes)),
        )
        due = storage.approved_due(
            now,
            cutoff=cutoff,
            freshness_minutes=int(production.freshness_minutes),
        )
    else:
        due = storage.approved_due(now)
    guarded_due = []
    for row in due:
        event_gate = storage.apply_event_identity_gate(int(row["id"]), now=now)
        if event_gate.action is not EventGateAction.ALLOW:
            warnings.append(
                f"EVENT_{event_gate.action.value} item={int(row['id'])} "
                f"reason={event_gate.reason} related={event_gate.related_item_id}"
            )
            continue
        if str(row["state"]) == "approved":
            duplicate_of = storage.suppress_if_recent_published_duplicate(int(row["id"]))
            if duplicate_of is not None:
                warnings.append(
                    f"PUBLISHED_STORY_DUPLICATE_SUPPRESSED item={int(row['id'])} "
                    f"duplicate_of={duplicate_of}"
                )
                continue
        guarded_due.append(row)
    due = guarded_due
    update_rows = [row for row in due if str(row["state"]) == "approved_update"]
    new_rows = [row for row in due if str(row["state"]) == "approved"]
    ranked_new = ()
    if new_rows:
        for row in new_rows:
            if row["editorial_category"]:
                continue
            decision = fallback_decision(str(row["clean_source_text"] or row["source_text"] or ""))
            storage.save_editorial_decision(
                int(row["id"]),
                category=decision.category.value,
                importance=decision.importance,
                interest=decision.interest,
                novelty=decision.novelty,
                mode=decision.mode.value,
                reason=decision.reason,
            )
        recent_rows = storage.recent_published_editorial_rows(limit=10)
        recent_categories = tuple(
            decision_from_row(row).category for row in recent_rows[:5]
        )
        recent_story_texts = tuple(str(row["source_text"] or "") for row in recent_rows)
        ranked_new = rank_candidates(
            new_rows,
            now=now,
            freshness_minutes=(
                int(production.freshness_minutes) if production_enabled else 180
            ),
            recent_categories=recent_categories,
            recent_story_texts=recent_story_texts,
        )
        rows_by_id = {int(row["id"]): row for row in new_rows}
        new_rows = [rows_by_id[candidate.item_id] for candidate in ranked_new]
    immediate_rows = [
        row
        for row in new_rows
        if is_immediate_news(
            str(row["source_text"] or ""),
            str(row["draft_text"] or ""),
            str(row["card_headline"] or ""),
            category=str(row["card_category"] or ""),
        )
    ]
    # Continuous production applies one global story cadence. Urgent rows keep
    # top queue priority but never bypass the configured success interval.
    if production_enabled:
        immediate_rows = []
    immediate_ids = {int(row["id"]) for row in immediate_rows}
    regular_rows = [row for row in new_rows if int(row["id"]) not in immediate_ids]
    published_last_hour = storage.published_count_since(now - timedelta(hours=1))
    last_published_at = storage.last_published_at()
    drain_ids, drain_interval = storage.publication_drain()
    if production_enabled:
        drain_ids, drain_interval = set(), 0
    if drain_ids:
        regular_rows = [row for row in regular_rows if int(row["id"]) in drain_ids]
    minimum_gap = timedelta(
        minutes=(drain_interval if drain_ids else int(config.queue.interval_minutes))
    )
    gap_elapsed = (
        last_published_at is None
        or now - last_published_at >= minimum_gap
    )
    can_publish_new = (
        published_last_hour
        < (30 if drain_ids else int(config.queue.max_posts_per_hour))
        and gap_elapsed
    )
    selected_regular_id = (
        int(regular_rows[0]["id"])
        if can_publish_new and regular_rows
        else None
    )
    for candidate in ranked_new:
        selected = candidate.item_id == selected_regular_id
        storage.record_editorial_selection(
            candidate.item_id,
            priority=candidate.priority,
            diversity_adjustment=candidate.diversity_adjustment,
            selected=selected,
        )
        logging.getLogger("premwire.publisher").info(
            "EDITORIAL_DESK item_id=%d category=%s importance=%d priority=%.2f "
            "editorial_mode=%s diversity_adjustment=%.1f repetition_penalty=%.1f "
            "selected=%d",
            candidate.item_id,
            candidate.decision.category.value,
            candidate.decision.importance,
            candidate.priority,
            candidate.decision.mode.value,
            candidate.diversity_adjustment,
            candidate.repetition_penalty,
            int(selected),
        )
    # In non-production utility flows, Here we go / Breaking / Срочно /
    # Официально rows may remain immediate. Continuous production clears that
    # list above, so every new story still obeys the configured cadence/cap.
    selected_rows = update_rows + immediate_rows
    if can_publish_new:
        selected_rows += regular_rows[:1]
    for row in selected_rows:
        item_id = int(row["id"])
        state = str(row["state"])
        raw_caption = str(row["draft_text"] or "").strip()
        if not raw_caption:
            reason = "MISSING_EDITORIAL_DRAFT"
            storage.requeue_editorial_policy(item_id, reason)
            warnings.append(f"Publish item {item_id} blocked pre-send: {reason}")
            continue
        source_name = next(
            (source.name for source in getattr(config, "sources", ()) if source.id == str(row["source_id"])),
            str(row["source_id"]),
        )
        try:
            clean_caption = sanitize_simple_publication_text(
                raw_caption,
                source_name=source_name,
                max_chars=(config.editor.max_output_chars if hasattr(config, "editor") else 1200),
            )
            clean_caption = strip_terminal_body_period(clean_caption)
            _assert_source_factual_safety(row, clean_caption)
        except ValueError as exc:
            reason = str(exc)
            immutable_source_failure = reason.startswith("SOURCE_POLICY_BLOCKED:")
            already_retried = storage.publication_policy_was_requeued(item_id)
            if immutable_source_failure or already_retried:
                storage.reject_publication_policy(item_id, reason)
            else:
                storage.requeue_editorial_policy(item_id, reason)
            warnings.append(f"Publish item {item_id} blocked pre-send: {reason}")
            continue
        if clean_caption != raw_caption:
            storage.apply_publication_text_policy(item_id, clean_caption)
        caption = html.escape(clean_caption)
        try:
            if state == "approved_update":
                message_id = row["telegram_message_id"]
                if message_id is None:
                    raise ValueError("Telegram message id is missing")
                api.delete_message(config.publishing.channel, int(message_id))
                replacement_id = api.send_text(config.publishing.channel, caption)
                storage.mark_updated_with_message(item_id, replacement_id)
                if getattr(getattr(config, "x_publishing", None), "enabled", False):
                    storage.queue_x_mirror(item_id, clean_caption)
                if getattr(getattr(config, "vk_publishing", None), "enabled", False):
                    storage.queue_vk_mirror(item_id, clean_caption)
                updated += 1
            else:
                message_id = api.send_text(config.publishing.channel, caption)
                storage.mark_published(item_id, message_id)
                if getattr(getattr(config, "x_publishing", None), "enabled", False):
                    storage.queue_x_mirror(item_id, clean_caption)
                if getattr(getattr(config, "vk_publishing", None), "enabled", False):
                    storage.queue_vk_mirror(item_id, clean_caption)
                if item_id in drain_ids:
                    storage.finish_publication_drain_item(item_id)
                published += 1
                auto_reaction = str(
                    getattr(config.publishing, "auto_reaction", "")
                ).strip()
                if auto_reaction:
                    try:
                        api.set_message_reaction(
                            config.publishing.channel,
                            message_id,
                            auto_reaction,
                        )
                    except TelegramApiError as exc:
                        warnings.append(
                            f"Reaction for item {item_id} failed: {exc}"
                        )
        except (TelegramApiError, ValueError) as exc:
            storage.mark_publish_failed(item_id, str(exc))
            warnings.append(f"Publish item {item_id} failed: {exc}")
    return PublishingSummary(published, updated, deleted, tuple(warnings))
