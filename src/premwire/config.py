from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Source


@dataclass(frozen=True, slots=True)
class QueueConfig:
    interval_minutes: int
    interval_min_minutes: int
    interval_max_minutes: int
    max_posts_per_hour: int
    max_generate_per_run: int
    stale_backlog_hours: int
    stale_backlog_min_hype_score: int
    publish_backlog_gradually: bool
    discard_backlog_automatically: bool
    daily_fresh_start: bool


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    poll_interval_seconds: int
    initial_sync: str


@dataclass(frozen=True, slots=True)
class FilterConfig:
    block_advertising: bool
    advertising_markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishingConfig:
    dry_run: bool
    channel: str
    chat: str
    owner: str
    auto_reaction: str
    auto_approve: bool
    delete_when_source_deleted: bool
    update_when_source_edited: bool


@dataclass(frozen=True, slots=True)
class XPublishingConfig:
    enabled: bool
    profile_directory: Path
    headless: bool
    max_chars: int
    max_per_run: int
    rate_limit_pause_minutes: int


@dataclass(frozen=True, slots=True)
class VkPublishingConfig:
    enabled: bool
    community_url: str
    screen_name: str
    group_id: int
    api_version: str
    max_per_run: int
    comments_enabled: bool


@dataclass(frozen=True, slots=True)
class DzenLifecycleConfig:
    enabled: bool


@dataclass(frozen=True, slots=True)
class DzenDigestConfig:
    enabled: bool
    activation_date: date
    publish_at: time
    poll_interval_seconds: int
    timezone_name: str
    expected_channel_slug: str
    expected_channel_title: str
    headless: bool
    minimum_items: int


@dataclass(frozen=True, slots=True)
class ModerationConfig:
    enabled: bool
    chat: str
    poll_timeout_seconds: int
    second_timeout_seconds: int
    third_timeout_seconds: int
    flood_window_seconds: int
    flood_max_messages: int
    repeat_window_seconds: int
    allowed_telegram_handles: tuple[str, ...]
    allowed_bot_usernames: tuple[str, ...]
    allowed_bot_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class EditorConfig:
    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: int
    max_source_chars: int
    max_output_chars: int
    temperature: float
    context_length: int = 4096
    keep_alive: str = "5m"
    faithful_rewrite_mode: bool = False
    simple_num_predict: int = 500
    simple_top_p: float | None = None
    simple_top_k: int | None = None
    simple_seed: int | None = None
    simple_presence_penalty: float | None = None


@dataclass(frozen=True, slots=True)
class StorageConfig:
    database: Path
    drafts_directory: Path
    logs_directory: Path


@dataclass(frozen=True, slots=True)
class PipelineV2Config:
    shadow_enabled: bool


@dataclass(frozen=True, slots=True)
class ProductionConfig:
    enabled: bool
    launch_cutoff: datetime
    freshness_minutes: int
    mode: str = "continuous"


@dataclass(frozen=True, slots=True)
class ImageGenWorkerConfig:
    enabled: bool
    fallback_minutes: int
    automation_interval_minutes: int
    output_directory: Path
    master_reference: Path
    allowed_reference_licenses: tuple[str, ...]
    default_template: str
    template_references: tuple[tuple[str, Path], ...]
    category_templates: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class AppConfig:
    timezone: tzinfo
    active_start: time
    active_end: time
    collection: CollectionConfig
    filters: FilterConfig
    queue: QueueConfig
    publishing: PublishingConfig
    x_publishing: XPublishingConfig
    vk_publishing: VkPublishingConfig
    dzen_lifecycle: DzenLifecycleConfig
    dzen_digest: DzenDigestConfig
    moderation: ModerationConfig
    editor: EditorConfig
    storage: StorageConfig
    pipeline_v2: PipelineV2Config
    production: ProductionConfig
    imagegen_worker: ImageGenWorkerConfig
    sources: tuple[Source, ...]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _parse_clock(value: str, field: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}; expected HH:MM") from exc


def _load_timezone(value: str) -> tzinfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        fallback_offsets = {
            "Europe/Moscow": timezone(timedelta(hours=3), "Europe/Moscow"),
            "UTC": timezone.utc,
        }
        try:
            return fallback_offsets[value]
        except KeyError as fallback_exc:
            raise ValueError(f"Unknown timezone: {value!r}") from fallback_exc


def load_config(project_root: Path) -> AppConfig:
    settings = _read_json(project_root / "config" / "settings.json")
    source_data = _read_json(project_root / "config" / "sources.json")

    sources = tuple(Source(**raw) for raw in source_data.get("sources", []))
    ids = [source.id for source in sources]
    urls = [source.url.rstrip("/").casefold() for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Source IDs must be unique")
    if len(urls) != len(set(urls)):
        raise ValueError("Source URLs must be unique")

    interval = int(settings["queue"]["interval_minutes"])
    interval_min = int(settings["queue"].get("interval_min_minutes", interval))
    interval_max = int(settings["queue"].get("interval_max_minutes", interval))
    max_per_hour = int(settings["queue"]["max_posts_per_hour"])
    max_generate_per_run = int(settings["queue"].get("max_generate_per_run", max_per_hour))
    stale_backlog_hours = int(settings["queue"].get("stale_backlog_hours", 3))
    stale_backlog_min_hype_score = int(
        settings["queue"].get("stale_backlog_min_hype_score", 6)
    )
    if interval < 1:
        raise ValueError("queue.interval_minutes must be positive")
    if max_per_hour < 1:
        raise ValueError("queue.max_posts_per_hour must be positive")
    if max_generate_per_run < 1:
        raise ValueError("queue.max_generate_per_run must be positive")
    if stale_backlog_hours < 1 or stale_backlog_min_hype_score < 1:
        raise ValueError("queue stale backlog settings must be positive")
    if not 1 <= interval_min <= interval <= interval_max <= 60:
        raise ValueError(
            "queue intervals must satisfy 1 <= min <= interval <= max <= 60"
        )
    if interval * max_per_hour < 60:
        raise ValueError(
            "Queue settings allow more posts per hour than max_posts_per_hour"
        )
    initial_sync = str(settings["collection"]["initial_sync"])
    if initial_sync not in {"baseline", "queue"}:
        raise ValueError("collection.initial_sync must be 'baseline' or 'queue'")
    poll_interval = int(settings["collection"]["poll_interval_seconds"])
    if poll_interval < 30:
        raise ValueError("collection.poll_interval_seconds must be at least 30")

    production = settings.get("production", {})
    production_mode = str(production.get("mode", "continuous"))
    if production_mode not in {"continuous", "daily_digest"}:
        raise ValueError("production.mode must be continuous or daily_digest")
    production_enabled = bool(production.get("enabled", False))
    cutoff_raw = str(production.get("launch_cutoff", "1970-01-01T00:00:00+00:00"))
    try:
        production_cutoff = datetime.fromisoformat(cutoff_raw)
    except ValueError as exc:
        raise ValueError("production.launch_cutoff must be ISO-8601") from exc
    if production_cutoff.tzinfo is None:
        raise ValueError("production.launch_cutoff must include a timezone")
    freshness_minutes = int(production.get("freshness_minutes", 180))
    if freshness_minutes < 1:
        raise ValueError("production.freshness_minutes must be positive")

    imagegen_worker = settings.get("imagegen_worker", {})
    imagegen_fallback_minutes = int(imagegen_worker.get("fallback_minutes", 20))
    imagegen_interval_minutes = int(
        imagegen_worker.get("automation_interval_minutes", 5)
    )
    if imagegen_fallback_minutes < 10:
        raise ValueError("imagegen_worker.fallback_minutes must be at least 10")
    if not 1 <= imagegen_interval_minutes <= 60:
        raise ValueError(
            "imagegen_worker.automation_interval_minutes must be between 1 and 60"
        )

    moderation = settings.get("moderation", {})
    moderation_poll_timeout = int(moderation.get("poll_timeout_seconds", 20))
    second_timeout_seconds = int(moderation.get("second_timeout_seconds", 300))
    third_timeout_seconds = int(moderation.get("third_timeout_seconds", 3600))
    flood_window_seconds = int(moderation.get("flood_window_seconds", 20))
    flood_max_messages = int(moderation.get("flood_max_messages", 7))
    repeat_window_seconds = int(moderation.get("repeat_window_seconds", 120))
    if not 1 <= moderation_poll_timeout <= 50:
        raise ValueError("moderation.poll_timeout_seconds must be between 1 and 50")
    if second_timeout_seconds < 30 or third_timeout_seconds < 30:
        raise ValueError("moderation timeout values must be at least 30 seconds")
    if third_timeout_seconds < second_timeout_seconds:
        raise ValueError("moderation third timeout must not be shorter than second")
    if flood_window_seconds < 5 or flood_max_messages < 3:
        raise ValueError("moderation flood limits are too aggressive")
    if repeat_window_seconds < 10:
        raise ValueError("moderation.repeat_window_seconds must be at least 10")

    editor = settings.get("editor", {})
    editor_provider = str(editor.get("provider", "ollama")).casefold()
    if editor_provider != "ollama":
        raise ValueError("editor.provider must be 'ollama'")
    editor_timeout = int(editor.get("timeout_seconds", 240))
    if editor_timeout < 10:
        raise ValueError("editor.timeout_seconds must be at least 10")
    max_source_chars = int(editor.get("max_source_chars", 8000))
    max_output_chars = int(editor.get("max_output_chars", 1200))
    editor_context_length = int(editor.get("context_length", 4096))
    editor_keep_alive = str(editor.get("keep_alive", "5m")).strip()
    if max_source_chars < 100 or max_output_chars < 100:
        raise ValueError("editor text limits must be at least 100 characters")
    if not 1024 <= editor_context_length <= 32768:
        raise ValueError("editor.context_length must be between 1024 and 32768")
    if not re.fullmatch(r"[1-9]\d*[smh]", editor_keep_alive):
        raise ValueError("editor.keep_alive must be a positive duration such as '15m'")

    storage = settings["storage"]
    x_publishing = settings.get("x_publishing", {})
    vk_publishing = settings.get("vk_publishing", {})
    x_max_chars = int(x_publishing.get("max_chars", 275))
    x_max_per_run = int(x_publishing.get("max_per_run", 6))
    x_rate_limit_pause_minutes = int(
        x_publishing.get("rate_limit_pause_minutes", 180)
    )
    if not 100 <= x_max_chars <= 280:
        raise ValueError("x_publishing.max_chars must be between 100 and 280")
    if not 1 <= x_max_per_run <= 20:
        raise ValueError("x_publishing.max_per_run must be between 1 and 20")
    if not 30 <= x_rate_limit_pause_minutes <= 1_440:
        raise ValueError(
            "x_publishing.rate_limit_pause_minutes must be between 30 and 1440"
        )
    vk_max_per_run = int(vk_publishing.get("max_per_run", 6))
    vk_group_id = int(vk_publishing.get("group_id", 0))
    vk_api_version = str(vk_publishing.get("api_version", "5.199")).strip()
    if not 1 <= vk_max_per_run <= 20:
        raise ValueError("vk_publishing.max_per_run must be between 1 and 20")
    if vk_group_id < 0:
        raise ValueError("vk_publishing.group_id must be a positive numeric community id")
    if not re.fullmatch(r"\d+\.\d+", vk_api_version):
        raise ValueError("vk_publishing.api_version must look like '5.199'")
    dzen_lifecycle = settings.get("dzen_lifecycle", {})
    dzen_digest = settings.get("dzen_digest", {})
    dzen_digest_enabled = bool(dzen_digest.get("enabled", False))
    if bool(dzen_lifecycle.get("enabled", False)) and dzen_digest_enabled:
        raise ValueError("dzen_lifecycle and dzen_digest cannot both be enabled")
    try:
        dzen_activation_date = date.fromisoformat(
            str(dzen_digest.get("activation_date", "1970-01-01"))
        )
    except ValueError as exc:
        raise ValueError("dzen_digest.activation_date must be YYYY-MM-DD") from exc
    dzen_publish_at = _parse_clock(
        str(dzen_digest.get("publish_at", "08:00")),
        "Dzen digest publish time",
    )
    dzen_poll_interval = int(dzen_digest.get("poll_interval_seconds", 300))
    if not 60 <= dzen_poll_interval <= 3600:
        raise ValueError(
            "dzen_digest.poll_interval_seconds must be between 60 and 3600"
        )
    dzen_minimum_items = int(dzen_digest.get("minimum_items", 1))
    if not 1 <= dzen_minimum_items <= 100:
        raise ValueError("dzen_digest.minimum_items must be between 1 and 100")
    dzen_timezone_name = str(
        dzen_digest.get("timezone", settings["timezone"])
    ).strip()
    _load_timezone(dzen_timezone_name)
    dzen_channel_slug = str(
        dzen_digest.get("expected_channel_slug", "premwire")
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", dzen_channel_slug):
        raise ValueError("dzen_digest.expected_channel_slug is invalid")
    dzen_channel_title = str(
        dzen_digest.get("expected_channel_title", "PremWire")
    ).strip()
    if not dzen_channel_title:
        raise ValueError("dzen_digest.expected_channel_title is required")
    return AppConfig(
        timezone=_load_timezone(str(settings["timezone"])),
        active_start=_parse_clock(settings["active_window"]["start"], "active start"),
        active_end=_parse_clock(settings["active_window"]["end"], "active end"),
        collection=CollectionConfig(
            poll_interval_seconds=poll_interval,
            initial_sync=initial_sync,
        ),
        filters=FilterConfig(
            block_advertising=bool(settings["filters"]["block_advertising"]),
            advertising_markers=tuple(
                str(marker).casefold()
                for marker in settings["filters"]["advertising_markers"]
            ),
        ),
        queue=QueueConfig(
            interval_minutes=interval,
            interval_min_minutes=interval_min,
            interval_max_minutes=interval_max,
            max_posts_per_hour=max_per_hour,
            max_generate_per_run=max_generate_per_run,
            stale_backlog_hours=stale_backlog_hours,
            stale_backlog_min_hype_score=stale_backlog_min_hype_score,
            publish_backlog_gradually=bool(
                settings["queue"]["publish_backlog_gradually"]
            ),
            discard_backlog_automatically=bool(
                settings["queue"]["discard_backlog_automatically"]
            ),
            daily_fresh_start=bool(settings["queue"].get("daily_fresh_start", True)),
        ),
        publishing=PublishingConfig(**settings["publishing"]),
        x_publishing=XPublishingConfig(
            enabled=bool(x_publishing.get("enabled", False)),
            profile_directory=project_root
            / str(x_publishing.get("profile_directory", "data/x-browser-profile")),
            headless=bool(x_publishing.get("headless", True)),
            max_chars=x_max_chars,
            max_per_run=x_max_per_run,
            rate_limit_pause_minutes=x_rate_limit_pause_minutes,
        ),
        vk_publishing=VkPublishingConfig(
            enabled=bool(vk_publishing.get("enabled", False)),
            community_url=str(
                vk_publishing.get("community_url", "https://vk.ru/PremWire")
            ).strip(),
            screen_name=str(vk_publishing.get("screen_name", "PremWire")).strip(),
            group_id=vk_group_id,
            api_version=vk_api_version,
            max_per_run=vk_max_per_run,
            comments_enabled=bool(vk_publishing.get("comments_enabled", True)),
        ),
        dzen_lifecycle=DzenLifecycleConfig(
            enabled=bool(dzen_lifecycle.get("enabled", False))
        ),
        dzen_digest=DzenDigestConfig(
            enabled=dzen_digest_enabled,
            activation_date=dzen_activation_date,
            publish_at=dzen_publish_at,
            poll_interval_seconds=dzen_poll_interval,
            timezone_name=dzen_timezone_name,
            expected_channel_slug=dzen_channel_slug,
            expected_channel_title=dzen_channel_title,
            headless=bool(dzen_digest.get("headless", True)),
            minimum_items=dzen_minimum_items,
        ),
        moderation=ModerationConfig(
            enabled=bool(moderation.get("enabled", False)),
            chat=str(moderation.get("chat", settings["publishing"]["chat"])),
            poll_timeout_seconds=moderation_poll_timeout,
            second_timeout_seconds=second_timeout_seconds,
            third_timeout_seconds=third_timeout_seconds,
            flood_window_seconds=flood_window_seconds,
            flood_max_messages=flood_max_messages,
            repeat_window_seconds=repeat_window_seconds,
            allowed_telegram_handles=tuple(
                str(value).lstrip("@").casefold()
                for value in moderation.get("allowed_telegram_handles", ())
            ),
            allowed_bot_usernames=tuple(
                str(value).lstrip("@").casefold()
                for value in moderation.get("allowed_bot_usernames", ())
            ),
            allowed_bot_ids=tuple(
                int(value) for value in moderation.get("allowed_bot_ids", ())
            ),
        ),
        editor=EditorConfig(
            enabled=bool(editor.get("enabled", False)),
            provider=editor_provider,
            model=str(editor.get("model", "qwen3.5:4b")),
            base_url=str(editor.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
            timeout_seconds=editor_timeout,
            max_source_chars=max_source_chars,
            max_output_chars=max_output_chars,
            temperature=float(editor.get("temperature", 0.55)),
            context_length=editor_context_length,
            keep_alive=editor_keep_alive,
            faithful_rewrite_mode=bool(editor.get("faithful_rewrite_mode", False)),
            simple_num_predict=int(editor.get("simple_num_predict", 500)),
            simple_top_p=(
                float(editor["simple_top_p"])
                if editor.get("simple_top_p") is not None
                else None
            ),
            simple_top_k=(
                int(editor["simple_top_k"])
                if editor.get("simple_top_k") is not None
                else None
            ),
            simple_seed=(
                int(editor["simple_seed"])
                if editor.get("simple_seed") is not None
                else None
            ),
            simple_presence_penalty=(
                float(editor["simple_presence_penalty"])
                if editor.get("simple_presence_penalty") is not None
                else None
            ),
        ),
        storage=StorageConfig(
            database=project_root / storage["database"],
            drafts_directory=project_root / storage["drafts_directory"],
            logs_directory=project_root / storage["logs_directory"],
        ),
        pipeline_v2=PipelineV2Config(
            shadow_enabled=bool(
                settings.get("pipeline_v2", {}).get("shadow_enabled", False)
            )
        ),
        production=ProductionConfig(
            enabled=production_enabled,
            launch_cutoff=production_cutoff,
            freshness_minutes=freshness_minutes,
            mode=production_mode,
        ),
        imagegen_worker=ImageGenWorkerConfig(
            enabled=bool(imagegen_worker.get("enabled", False)),
            fallback_minutes=imagegen_fallback_minutes,
            automation_interval_minutes=imagegen_interval_minutes,
            output_directory=project_root
            / str(imagegen_worker.get("output_directory", "artifacts/live_visuals")),
            master_reference=project_root
            / str(imagegen_worker.get("master_reference", "")),
            allowed_reference_licenses=tuple(
                str(value).strip().casefold()
                for value in imagegen_worker.get(
                    "allowed_reference_licenses", ("CC0", "Public domain")
                )
                if str(value).strip()
            ),
            default_template=str(
                imagegen_worker.get("default_template", "TRANSFER")
            ).strip().upper(),
            template_references=tuple(
                (
                    str(template_id).strip().upper(),
                    project_root / str(reference_path),
                )
                for template_id, reference_path in dict(
                    imagegen_worker.get("template_references", {})
                ).items()
                if str(template_id).strip() and str(reference_path).strip()
            ),
            category_templates=tuple(
                (
                    str(category).strip().upper(),
                    str(template_id).strip().upper(),
                )
                for category, template_id in dict(
                    imagegen_worker.get("category_templates", {})
                ).items()
                if str(category).strip() and str(template_id).strip()
            ),
        ),
        sources=sources,
    )
