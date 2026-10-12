from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import statistics
import threading
import time

from .collectors.base import CollectionBatch
from .channel_audit import audit_deleted_editor_posts
from .config import AppConfig, load_config
from .content_filter import AdvertisementFilter, SourceQualityFilter
from .dzen_digest_service import run_dzen_digest_once
from .dzen_lifecycle import process_dzen_lifecycle
from .editorial_desk import decision_from_row, rank_candidates, validated_decision
from .env import load_env
from .models import IngestResult
from .process_lock import ProcessLock
from .publisher import process_publishing, process_visual_text_previews
from .runtime_paths import runtime_path
from .runner import collect_once
from .runtime_revision import source_fingerprint
from .storage import EventGateAction, Storage
from .simple_editorial import assess_simple_source
from .editorial_safety import transient_ttl_minutes
from .text_editor import EditorError, OllamaTextEditor
from .x_mirror import process_x_mirroring
from .vk_mirror import process_vk_mirroring


EXPECTED_LAUNCH_CUTOFF = datetime.fromisoformat("2026-08-12T10:44:00+03:00")
DELETE_AUDIT_INTERVAL_SECONDS = 30
DZEN_LIFECYCLE_INTERVAL_SECONDS = 30
X_POLL_MAX_BACKOFF_SECONDS = 15 * 60

_BREAKING_CONFIRMATION_RE = re.compile(
    r"\b(?:here\s+we\s+go|официально|official(?:ly)?|"
    r"нов(?:ый|ая)\s+игрок)\b",
    re.IGNORECASE,
)
_BREAKING_TRANSFER_ACTION_RE = re.compile(
    r"\b(?:переход(?:ит|ят|а|е|ом)?|переш[её]л|трансфер|нов(?:ый|ая)\s+игрок|"
    r"sign(?:ed|ing|s)?|join(?:ed|ing|s)?|transfer|deal)\b",
    re.IGNORECASE,
)


def is_breaking_fast_lane_source(source_text: str) -> bool:
    """Recognise only explicit completed/confirmed transfer news.

    This routing decision runs after the normal source checks, Qwen rewrite and
    published-story dedupe. It changes delivery latency only; it never changes
    the editorial text or upgrades transfer certainty.
    """

    return bool(
        _BREAKING_CONFIRMATION_RE.search(source_text)
        and _BREAKING_TRANSFER_ACTION_RE.search(source_text)
    )


@dataclass(frozen=True, slots=True)
class PollResult:
    collected: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    cutoff_skipped: int = 0
    filtered: int = 0
    duration_seconds: float = 0.0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerResult:
    editorial_ready: int = 0
    editorial_blocked: int = 0
    telegram_sent: int = 0
    twitter_sent: int = 0
    vk_sent: int = 0
    telegram_deletions_detected: int = 0
    stale_skipped: int = 0
    queue_depth: int = 0
    selected_item_id: int | None = None
    selected_category: str | None = None
    selected_importance: int | None = None
    selected_priority: float | None = None
    selected_mode: str | None = None
    selected_reason: str | None = None
    warnings: tuple[str, ...] = ()


def _cutoff(config: AppConfig) -> datetime:
    if not config.production.enabled:
        raise RuntimeError("TEXT_PRODUCTION_DISABLED")
    return config.production.launch_cutoff.astimezone(timezone.utc)


def validate_launch_preconditions(config: AppConfig, env: dict[str, str]) -> None:
    failures: list[str] = []
    if not config.production.enabled:
        failures.append("PRODUCTION_DISABLED")
    if config.production.launch_cutoff != EXPECTED_LAUNCH_CUTOFF:
        failures.append("LAUNCH_CUTOFF_MISMATCH")
    if config.publishing.dry_run:
        failures.append("PUBLISHING_DRY_RUN")
    if not config.publishing.auto_approve:
        failures.append("AUTO_APPROVAL_DISABLED")
    if config.queue.interval_minutes != 4 or config.queue.max_posts_per_hour != 20:
        failures.append("PUBLICATION_CADENCE_UNSAFE")
    if config.collection.poll_interval_seconds < 30:
        failures.append("POLL_INTERVAL_UNSAFE")
    if not config.editor.enabled:
        failures.append("EDITOR_DISABLED")
    if not env.get("TELEGRAM_BOT_TOKEN", "").strip():
        failures.append("TELEGRAM_TOKEN_MISSING")
    if not any(source.enabled for source in config.sources):
        failures.append("TRUSTED_SOURCE_ALLOWLIST_EMPTY")
    if failures:
        raise RuntimeError(";".join(failures))


def ingest_poll(
    config: AppConfig,
    env: dict[str, str],
    storage: Storage,
    *,
    platform: str | None = None,
    observer=None,
) -> PollResult:
    """Fast source-only poll; never invokes editorial or publishers."""
    started = time.perf_counter()
    cutoff = _cutoff(config)
    collect_kwargs: dict[str, object] = {
        "x_since_id": storage.get_runtime_state("x_since_id"),
    }
    if platform is not None:
        collect_kwargs["platform"] = platform
    batches = collect_once(config, env, **collect_kwargs)
    content_filter = AdvertisementFilter(config.filters)
    source_filter = SourceQualityFilter()
    counters = {value: 0 for value in IngestResult}
    collected = cutoff_skipped = filtered = 0
    warnings: list[str] = []
    for batch in batches:
        if observer is not None:
            observer(batch.items)
        warnings.extend(batch.warnings)
        source_ids = dict.fromkeys(item.source_id for item in batch.items)
        for source_id in source_ids:
            source_items = tuple(item for item in batch.items if item.source_id == source_id)
            needs_baseline = (
                config.collection.initial_sync == "baseline"
                and not storage.source_is_initialised(source_id)
            )
            for item in source_items:
                collected += 1
                outcome = storage.ingest(item)
                counters[outcome.result] += 1
                if storage.reject_unpublished_before_cutoff(outcome.item_id, cutoff):
                    cutoff_skipped += 1
                    continue
                if outcome.result in {IngestResult.NEW, IngestResult.UPDATED}:
                    decision = content_filter.check(item.text)
                    if decision.blocked:
                        storage.mark_filtered(
                            outcome.item_id,
                            decision.reason or "advertising",
                            decision.evidence,
                        )
                        filtered += 1
                    else:
                        source_decision = source_filter.check(item.text)
                        if source_decision.blocked:
                            storage.mark_low_information(
                                outcome.item_id,
                                source_decision.reason or "low_information",
                            )
                            filtered += 1
            if needs_baseline and source_items:
                storage.complete_source_baseline(source_id)
            if source_items and all(item.external_id.isdigit() for item in source_items):
                storage.reconcile_source_window(
                    source_id,
                    tuple(item.external_id for item in source_items),
                    delete_mirror=config.publishing.delete_when_source_deleted,
                )
        if batch.collector == "x":
            numeric = [int(item.external_id) for item in batch.items if item.external_id.isdigit()]
            if numeric:
                storage.set_runtime_state("x_since_id", str(max(numeric)))
    storage.collapse_story_duplicates()
    return PollResult(
        collected=collected,
        new=counters[IngestResult.NEW],
        updated=counters[IngestResult.UPDATED],
        duplicates=counters[IngestResult.DUPLICATE],
        cutoff_skipped=cutoff_skipped,
        filtered=filtered,
        duration_seconds=time.perf_counter() - started,
        warnings=tuple(warnings),
    )


def x_poll_backoff_seconds(base_interval: int, failure_streak: int) -> int:
    """Return a bounded delay for a fully unavailable best-effort X feed."""

    if failure_streak <= 0:
        return int(base_interval)
    return min(
        X_POLL_MAX_BACKOFF_SECONDS,
        int(base_interval) * (2 ** min(int(failure_streak), 5)),
    )


def editorial_publish_once(
    config: AppConfig,
    env: dict[str, str],
    storage: Storage,
    *,
    editor: OllamaTextEditor | None = None,
    audit_deletions: bool = False,
) -> WorkerResult:
    """Prepare at most one fresh story, then independently drain live channels."""
    if getattr(config.production, "mode", "continuous") == "daily_digest":
        raise RuntimeError("CONTINUOUS_PRODUCTION_DISABLED: use daily-digest")
    cutoff = _cutoff(config)
    now = datetime.now(timezone.utc)
    expires_after = now - timedelta(minutes=config.production.freshness_minutes)
    cutoff_skipped = storage.suppress_unpublished_before_cutoff(cutoff)
    stale_skipped = storage.suppress_stale_production_items(cutoff, expires_after)
    warnings: list[str] = []
    imagegen = getattr(config, "imagegen_worker", None)
    imagegen_enabled = bool(getattr(imagegen, "enabled", False))
    if imagegen_enabled:
        released = storage.release_expired_visual_work(
            now - timedelta(minutes=int(imagegen.fallback_minutes))
        )
        if released:
            warnings.append(f"IMAGEGEN_TEXT_FALLBACK={released}")

        promoted_visual = storage.promote_waiting_visual_draft()
        if promoted_visual is not None:
            warnings.append(f"VISUAL_WAITING_PROMOTED item={promoted_visual}")
    else:
        retired_visual = storage.retire_visual_handoffs_for_text_only()
        if retired_visual:
            warnings.append(f"VISUAL_HANDOFF_RETIRED_TEXT_ONLY={retired_visual}")

    deleted = 0
    if audit_deletions:
        now_epoch = int(time.time())
        state_key = "text_production_delete_audit:last_epoch"
        previous = int(storage.get_runtime_state(state_key) or 0)
        if now_epoch - previous >= DELETE_AUDIT_INTERVAL_SECONDS:
            # Advance the durable throttle before the request so a transient
            # Telegram failure cannot make the five-second worker hammer it.
            storage.set_runtime_state(state_key, str(now_epoch))
            try:
                audit = audit_deleted_editor_posts(storage, config.publishing.channel)
                deleted = len(audit.deleted_item_ids)
                if deleted:
                    warnings.append(f"TELEGRAM_DELETE_DETECTED={deleted}")
            except Exception as exc:
                warnings.append(f"TELEGRAM_DELETE_AUDIT_SKIPPED={exc}")

    telegram = process_publishing(config, env, storage)
    warnings.extend(telegram.warnings)
    visual_text = process_visual_text_previews(config, env, storage)
    warnings.extend(visual_text.warnings)
    twitter = process_x_mirroring(config, storage)
    warnings.extend(twitter.warnings)
    vk = process_vk_mirroring(config, env, storage)
    warnings.extend(vk.warnings)

    ready = blocked = 0
    selected = None
    all_candidates = storage.production_editorial_candidates(cutoff, expires_after)
    active_visual = storage.active_visual_item() if imagegen_enabled else None
    if (
        active_visual is not None
        and str(active_visual["state"]) == "visual_delivery_unknown"
    ):
        candidates = []
    else:
        # Editorial/Qwen work is independent from the single slow ImageGen
        # delivery slot.  Non-breaking drafts wait in state=scheduled while
        # breaking stories still use the immediate text-only fast lane.
        candidates = all_candidates
    if candidates:
        candidate_ids = {int(row["id"]) for row in candidates}
        active_editor = editor or OllamaTextEditor(config.editor)
        for candidate in candidates:
            item_id = int(candidate["id"])
            event_gate = storage.apply_event_identity_gate(item_id, now=now)
            if event_gate.action is not EventGateAction.ALLOW:
                logging.getLogger("premwire.text_production").info(
                    "EVENT_IDENTITY item_id=%d action=%s reason=%s related_item_id=%s",
                    item_id,
                    event_gate.action.value,
                    event_gate.reason,
                    event_gate.related_item_id,
                )
                warnings.append(
                    f"EVENT_{event_gate.action.value} item={item_id} "
                    f"reason={event_gate.reason} related={event_gate.related_item_id}"
                )
                if event_gate.action in {EventGateAction.DEFER, EventGateAction.SKIP_PROMO}:
                    blocked += 1
                continue
            source_decision = assess_simple_source(str(candidate["source_text"]))
            ttl = transient_ttl_minutes(str(candidate["source_text"]))
            source_at = datetime.fromisoformat(str(
                candidate["source_published_at"] or candidate["discovered_at"]
            )).astimezone(timezone.utc)
            stale_live = ttl is not None and now - source_at > timedelta(minutes=ttl)
            if not source_decision.allowed or stale_live:
                if str(candidate["state"]) == "queued":
                    storage.set_schedule(item_id, now)
                reason = source_decision.reason or ("STALE_LIVE_UPDATE" if stale_live else "BROKEN_SOURCE")
                storage.mark_editor_failed(item_id, reason)
                logging.getLogger("premwire.text_production").info(
                    "EDITORIAL_DESK item_id=%d selected=0 defer_reason=%s",
                    item_id,
                    reason,
                )
                warnings.append(f"EDITORIAL_DEFERRED item={item_id} reason={reason}")
                blocked += 1
                continue
            if candidate["editorial_category"]:
                continue
            try:
                payload = active_editor.editorial_rank(source_decision.clean_text)
            except Exception as exc:
                payload = None
                warnings.append(f"EDITORIAL_DESK_FALLBACK item={item_id} reason={exc}")
            decision = validated_decision(source_decision.clean_text, payload)
            storage.save_editorial_decision(
                item_id,
                category=decision.category.value,
                importance=decision.importance,
                interest=decision.interest,
                novelty=decision.novelty,
                mode=decision.mode.value,
                reason=decision.reason,
            )

        candidates = [
            row
            for row in storage.production_editorial_candidates(cutoff, expires_after)
            if int(row["id"]) in candidate_ids
        ]
        recent_rows = storage.recent_published_editorial_rows(limit=10)
        recent_categories = tuple(
            decision_from_row(row).category for row in recent_rows[:5]
        )
        recent_story_texts = tuple(str(row["source_text"] or "") for row in recent_rows)
        ranked = rank_candidates(
            candidates,
            now=now,
            freshness_minutes=config.production.freshness_minutes,
            recent_categories=recent_categories,
            recent_story_texts=recent_story_texts,
        )
        selected = ranked[0] if ranked else None
        selected_id = selected.item_id if selected else None
        for candidate in ranked:
            is_selected = candidate.item_id == selected_id
            storage.record_editorial_selection(
                candidate.item_id,
                priority=candidate.priority,
                diversity_adjustment=candidate.diversity_adjustment,
                selected=is_selected,
            )
            logging.getLogger("premwire.text_production").info(
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
                int(is_selected),
            )

    if selected is not None:
        row = next(row for row in candidates if int(row["id"]) == selected.item_id)
        item_id = int(row["id"])
        if str(row["state"]) == "queued":
            storage.set_schedule(item_id, now)
        try:
            draft = active_editor.simple_rewrite(
                next(
                    (source.name for source in config.sources if source.id == str(row["source_id"])),
                    str(row["source_id"]),
                ),
                str(row["source_text"]),
                editorial_mode=selected.decision.mode.value,
            )
            storage.save_draft(item_id, draft)
            event_gate = storage.apply_event_identity_gate(item_id, now=now)
            if event_gate.action is not EventGateAction.ALLOW:
                warnings.append(
                    f"EVENT_{event_gate.action.value} item={item_id} "
                    f"reason={event_gate.reason} related={event_gate.related_item_id}"
                )
                if event_gate.action in {EventGateAction.DEFER, EventGateAction.SKIP_PROMO}:
                    blocked += 1
            else:
                duplicate_of = storage.suppress_if_recent_published_duplicate(item_id)
                if duplicate_of is not None:
                    warnings.append(
                        f"PUBLISHED_STORY_DUPLICATE_SUPPRESSED item={item_id} "
                        f"duplicate_of={duplicate_of}"
                    )
                elif imagegen_enabled:
                    if storage.visual_work_active():
                        warnings.append(f"VISUAL_WAITING item={item_id}")
                    else:
                        storage.prepare_visual_pending(item_id)
                    ready = 1
                else:
                    storage.prepare_text_only(item_id)
                    storage.approve_item(item_id)
                    ready = 1
        except EditorError as exc:
            storage.mark_editor_failed(item_id, str(exc))
            warnings.append(f"EDITORIAL_BLOCKED item={item_id} reason={exc}")
            blocked += 1
        except Exception as exc:
            storage.mark_editor_failed(item_id, str(exc))
            warnings.append(f"EDITORIAL_BLOCKED item={item_id} reason={exc}")
            blocked += 1
        finally:
            # The editor resets this trace for every attempt, including failures.
            # Keep bounded metadata in the existing rotating log, not credentials
            # or raw model responses. It must never change publication outcome.
            trace = getattr(active_editor, "last_simple_trace", None)
            if isinstance(trace, dict):
                logging.getLogger("premwire.text_production").info(
                    "EDITORIAL_TRACE item=%s trace=%s", item_id,
                    json.dumps(trace, ensure_ascii=False, default=str),
                )

    # A newly prepared story may use the current slot immediately. The
    # publisher enforces the global success-based ten-minute cadence.
    after = process_publishing(config, env, storage)
    warnings.extend(after.warnings)
    after_visual_text = process_visual_text_previews(config, env, storage)
    warnings.extend(after_visual_text.warnings)
    after_x = process_x_mirroring(config, storage)
    warnings.extend(after_x.warnings)
    after_vk = process_vk_mirroring(config, env, storage)
    warnings.extend(after_vk.warnings)
    depth = storage.production_queue_depth(cutoff, expires_after)
    if cutoff_skipped:
        warnings.append(f"CUTOFF_SKIPPED={cutoff_skipped}")
    return WorkerResult(
        editorial_ready=ready,
        editorial_blocked=blocked,
        telegram_sent=(
            telegram.published
            + visual_text.published
            + after.published
            + after_visual_text.published
        ),
        twitter_sent=twitter.published + after_x.published,
        vk_sent=vk.published + after_vk.published,
        telegram_deletions_detected=deleted,
        stale_skipped=stale_skipped,
        queue_depth=depth,
        selected_item_id=(selected.item_id if selected else None),
        selected_category=(selected.decision.category.value if selected else None),
        selected_importance=(selected.decision.importance if selected else None),
        selected_priority=(selected.priority if selected else None),
        selected_mode=(selected.decision.mode.value if selected else None),
        selected_reason=(selected.decision.reason if selected else None),
        warnings=tuple(warnings),
    )


class TextProductionService:
    """Single-process, split poll/editorial production service."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.config = load_config(self.root)
        self.env = load_env(self.root / ".env")
        self.storage = Storage(self.config.storage.database)
        self.storage.initialise()
        self.stop_event = threading.Event()
        self.poll_locks = {
            "telegram": threading.Lock(),
            "x": threading.Lock(),
        }
        self.status_lock = threading.Lock()
        self.poll_samples: dict[str, list[float]] = {"telegram": [], "x": []}
        self.status_path = self.config.storage.logs_directory / "text-production-status.json"
        self.code_fingerprint = source_fingerprint(self.root)
        self.logger = logging.getLogger("premwire.text_production")
        if not self.logger.handlers:
            self.config.storage.logs_directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                self.config.storage.logs_directory / "text-production.log",
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def _write_status(self, payload: dict[str, object]) -> None:
        with self.status_lock:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            existing: dict[str, object] = {}
            if self.status_path.exists():
                try:
                    existing = json.loads(self.status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
            if payload.get("service") == "STARTING":
                # A restart begins a new runtime snapshot. Keeping fields from
                # an older architecture made ACTIVE status contradict itself
                # (for example text-only flags beside FAST_VISUAL_HANDOFF).
                existing = {}
            existing.update(payload)
            if payload.get("service") in {"STARTING", "ACTIVE"}:
                existing.pop("last_worker_error", None)
            temporary = self.status_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.status_path)

    def _poll_loop(self, platform: str) -> None:
        interval = int(self.config.collection.poll_interval_seconds)
        failure_streak = 0
        poll_lock = self.poll_locks[platform]
        while not self.stop_event.is_set():
            cycle_started = time.monotonic()
            delay = interval
            if not poll_lock.acquire(blocking=False):
                self.logger.warning("POLL_SKIPPED_OVERLAP platform=%s", platform)
            else:
                try:
                    self.logger.info("POLL_STARTED platform=%s", platform)
                    result = ingest_poll(
                        self.config,
                        self.env,
                        self.storage,
                        platform=platform,
                    )
                    samples = (
                        self.poll_samples[platform] + [result.duration_seconds]
                    )[-50:]
                    self.poll_samples[platform] = samples
                    self.logger.info(
                        "POLL_COMPLETED platform=%s duration=%.3f NEW_ITEMS_FOUND=%d "
                        "DUPLICATES_SKIPPED=%d CUTOFF_SKIPPED=%d",
                        platform,
                        result.duration_seconds,
                        result.new,
                        result.duplicates,
                        result.cutoff_skipped,
                    )
                    if platform == "x" and result.collected == 0 and result.warnings:
                        failure_streak += 1
                        delay = x_poll_backoff_seconds(interval, failure_streak)
                        self.logger.warning(
                            "X_POLL_BACKOFF failure_streak=%d delay_seconds=%d",
                            failure_streak,
                            delay,
                        )
                    else:
                        failure_streak = 0
                    status: dict[str, object] = {
                        "service": "ACTIVE",
                        f"last_{platform}_poll_at": datetime.now(timezone.utc).isoformat(),
                        f"last_{platform}_poll": asdict(result),
                        f"{platform}_poll_mean_seconds": statistics.mean(samples),
                        f"{platform}_poll_p95_seconds": sorted(samples)[
                            max(0, math.ceil(len(samples) * .95) - 1)
                        ],
                        f"{platform}_poll_interval_seconds": interval,
                        f"{platform}_poll_backoff_seconds": delay,
                        "overlap_protection": "ACTIVE_PER_COLLECTOR",
                    }
                    if platform == "telegram":
                        status.update({
                            "last_poll_at": status["last_telegram_poll_at"],
                            "last_poll": asdict(result),
                            "poll_mean_seconds": statistics.mean(samples),
                            "poll_p95_seconds": status["telegram_poll_p95_seconds"],
                            "poll_interval_seconds": interval,
                        })
                    self._write_status(status)
                except Exception as exc:
                    self.logger.exception("POLL_FAILED platform=%s %s", platform, exc)
                    self._write_status({f"last_{platform}_poll_error": str(exc)[:500]})
                finally:
                    poll_lock.release()
            remaining = delay - (time.monotonic() - cycle_started)
            self.stop_event.wait(max(0.0, remaining))

    def _dzen_loop(self) -> None:
        """Run passive-mirror mapping, reactions and deletion without blocking TG/X."""

        while not self.stop_event.is_set():
            try:
                result = process_dzen_lifecycle(self.storage)
                self._write_status({
                    "dzen_lifecycle": {
                        "last_run_at": datetime.now(timezone.utc).isoformat(),
                        "mapped": result.mapped,
                        "liked": result.liked,
                        "deleted": result.deleted,
                        "already_deleted": result.already_deleted,
                        "warnings": list(result.warnings),
                    }
                })
            except Exception as exc:
                # Dzen is an auxiliary lifecycle path. It must never stop the
                # Telegram/X production worker or passive @zen_sync_bot mirror.
                self.logger.exception("DZEN_DELETE_AUTOMATION_FAILED %s", exc)
                self._write_status({"dzen_delete_automation_error": str(exc)[:500]})
            self.stop_event.wait(DZEN_LIFECYCLE_INTERVAL_SECONDS)

    def _dzen_digest_loop(self) -> None:
        """Publish one native prior-day article without blocking other mirrors."""

        while not self.stop_event.is_set():
            try:
                result = run_dzen_digest_once(self.config, self.storage)
                payload = asdict(result)
                payload["last_run_at"] = datetime.now(timezone.utc).isoformat()
                self._write_status({"dzen_digest": payload})
                if result.status in {"PUBLISHED", "RECONCILED"}:
                    self.logger.info(
                        "DZEN_DAILY_DIGEST_%s date=%s sources=%d stories=%d url=%s",
                        result.status,
                        result.digest_date,
                        result.source_count,
                        result.story_count,
                        result.article_url,
                    )
                elif result.actionable:
                    self.logger.error(
                        "DZEN_DAILY_DIGEST_ACTION_REQUIRED status=%s detail=%s",
                        result.status,
                        result.detail,
                    )
            except Exception as exc:
                # Dzen is isolated from Telegram, X and VK.  A failed article
                # cycle is reported but never stops or delays text production.
                self.logger.exception("DZEN_DAILY_DIGEST_FAILED %s", exc)
                self._write_status({
                    "dzen_digest": {
                        "status": "ERROR",
                        "last_run_at": datetime.now(timezone.utc).isoformat(),
                        "detail": str(exc)[:500],
                        "actionable": True,
                    }
                })
            self.stop_event.wait(self.config.dzen_digest.poll_interval_seconds)

    def run(self) -> None:
        validate_launch_preconditions(self.config, self.env)
        editor = OllamaTextEditor(self.config.editor)
        editor_ready, editor_message = editor.verify()
        if not editor_ready:
            raise RuntimeError(f"EDITOR_NOT_READY: {editor_message}")
        self.logger.info(
            "PRODUCTION_CONFIG mode=%s model=%s temperature=%s "
            "context_length=%d keep_alive=%s",
            (
                "FAST_VISUAL_HANDOFF"
                if self.config.imagegen_worker.enabled
                else "TEXT_ONLY"
            ),
            self.config.editor.model,
            self.config.editor.temperature,
            self.config.editor.context_length,
            self.config.editor.keep_alive,
        )
        cutoff = _cutoff(self.config)
        suppressed = self.storage.suppress_unpublished_before_cutoff(cutoff)
        self.storage.clear_runtime_state("publication_drain")
        self._write_status({
            "service": "STARTING",
            "pid": __import__("os").getpid(),
            "code_fingerprint": self.code_fingerprint,
            "code_loaded_at": datetime.now(timezone.utc).isoformat(),
            "launch_cutoff": self.config.production.launch_cutoff.isoformat(),
            "backlog_items_suppressed": suppressed,
            "publishing_mode": (
                "FAST_VISUAL_HANDOFF"
                if self.config.imagegen_worker.enabled
                else "TEXT_ONLY"
            ),
            "imagegen_worker": (
                "FAST_RENDERER_ACTIVE" if self.config.imagegen_worker.enabled else "OFF"
            ),
            "text_model": self.config.editor.model,
            "text_model_temperature": self.config.editor.temperature,
            "text_model_context_length": self.config.editor.context_length,
            "text_model_keep_alive": self.config.editor.keep_alive,
            "telegram": "ACTIVE" if self.env.get("TELEGRAM_BOT_TOKEN", "").strip() else "BLOCKED_TOKEN_MISSING",
            "twitter_x": "ACTIVE" if self.config.x_publishing.profile_directory.exists() else "BLOCKED_PROFILE_MISSING",
            "dzen": (
                "DAILY_NATIVE_ARTICLE_AUTOMATIC"
                if self.config.dzen_digest.enabled
                else "DISABLED"
            ),
            "dzen_lifecycle": (
                "ACTIVE" if self.config.dzen_lifecycle.enabled else "DISABLED_TEMPORARILY"
            ),
            "dzen_digest": (
                {
                    "status": "STARTING",
                    "activation_date": self.config.dzen_digest.activation_date.isoformat(),
                    "publish_at": self.config.dzen_digest.publish_at.isoformat(timespec="minutes"),
                    "timezone": self.config.dzen_digest.timezone_name,
                    "mode": "AUTOMATIC_NATIVE_ARTICLE",
                }
                if self.config.dzen_digest.enabled
                else {"status": "DISABLED"}
            ),
            "ceo_publication_pause": self.storage.publication_is_paused(),
            "telegram_new_publish": (
                "BLOCKED_BY_CEO_PAUSE"
                if self.storage.publication_is_paused()
                else "ACTIVE"
            ),
            "twitter_x_new_publish": (
                "BLOCKED_BY_CEO_PAUSE"
                if self.storage.publication_is_paused()
                else "ACTIVE"
            ),
        })
        poll_threads = [
            threading.Thread(
                target=self._poll_loop,
                args=(platform,),
                name=f"premwire-ingestion-{platform}",
                daemon=True,
            )
            for platform in ("telegram", "x")
        ]
        for poll_thread in poll_threads:
            poll_thread.start()
        if self.config.dzen_lifecycle.enabled:
            dzen_thread = threading.Thread(
                target=self._dzen_loop,
                name="premwire-dzen-lifecycle",
                daemon=True,
            )
            dzen_thread.start()
        if self.config.dzen_digest.enabled:
            dzen_digest_thread = threading.Thread(
                target=self._dzen_digest_loop,
                name="premwire-dzen-daily-digest",
                daemon=True,
            )
            dzen_digest_thread.start()
        while not self.stop_event.is_set():
            try:
                result = editorial_publish_once(
                    self.config, self.env, self.storage, editor=editor,
                    audit_deletions=True,
                )
                last = self.storage.last_published_at()
                next_allowed = (
                    last + timedelta(minutes=self.config.queue.interval_minutes)
                    if last is not None else datetime.now(timezone.utc)
                )
                self.logger.info(
                    "EDITORIAL_READY=%d EDITORIAL_BLOCKED=%d QUEUE_DEPTH=%d "
                    "TELEGRAM_SENT=%d TWITTER_SENT=%d TELEGRAM_DELETIONS=%d "
                    "NEXT_PUBLICATION_ALLOWED_AT=%s",
                    result.editorial_ready, result.editorial_blocked, result.queue_depth,
                    result.telegram_sent, result.twitter_sent,
                    result.telegram_deletions_detected, next_allowed.isoformat(),
                )
                if not result.queue_depth:
                    self.logger.info("NOOP_NO_READY_POST")
                publication_paused = self.storage.publication_is_paused()
                self._write_status({
                    "service": "ACTIVE",
                    "last_worker_at": datetime.now(timezone.utc).isoformat(),
                    "last_worker": asdict(result),
                    "queue_depth": result.queue_depth,
                    "next_publication_allowed_at": next_allowed.isoformat(),
                    "ceo_publication_pause": publication_paused,
                    "telegram_new_publish": (
                        "BLOCKED_BY_CEO_PAUSE" if publication_paused else "ACTIVE"
                    ),
                    "twitter_x_new_publish": (
                        "BLOCKED_BY_CEO_PAUSE" if publication_paused else "ACTIVE"
                    ),
                })
            except Exception as exc:
                self.logger.exception("WORKER_FAILED %s", exc)
                self._write_status({"last_worker_error": str(exc)[:500]})
            self.stop_event.wait(5.0)


def run_text_production(root: Path) -> int:
    if load_config(root).production.mode == "daily_digest":
        raise RuntimeError("CONTINUOUS_PRODUCTION_DISABLED: use daily-digest")
    service = TextProductionService(root)
    lock = ProcessLock(runtime_path("locks", "premwire-text-production.lock"))
    if not lock.acquire():
        raise RuntimeError("PRODUCTION_LOCK_ALREADY_HELD")
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop_event.set()
    finally:
        lock.release()
    return 0


__all__ = [
    "PollResult", "TextProductionService", "WorkerResult", "editorial_publish_once",
    "ingest_poll", "run_text_production", "validate_launch_preconditions",
    "x_poll_backoff_seconds",
]
