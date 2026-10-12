from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import threading

from .config import AppConfig
from .simple_editorial import sanitize_simple_publication_text
from .storage import Storage
from .text_editor import EditorError, OllamaTextEditor
from .x_browser import (
    XAuthenticationRequired,
    XBrowserError,
    XBrowserSession,
    XDailyPostLimitReached,
    XDeliveryUnverified,
    XDuplicatePost,
    XEditUnavailable,
    XPostAlreadyDeleted,
)
from .x_text import compact_for_x


_X_MIRROR_LOCK = threading.Lock()
_EXACT_X_POST_URL = re.compile(
    r"^https://x\.com/[A-Za-z0-9_]+/status/[0-9]+(?:\?.*)?$"
)
_ENTITY_HASHTAG_RE = re.compile(r"(?<!\w)#[A-Za-z0-9]+\b")


@dataclass(frozen=True, slots=True)
class XMirrorSummary:
    published: int = 0
    replaced: int = 0
    deleted: int = 0
    warnings: tuple[str, ...] = ()


def process_x_mirroring(config: AppConfig, storage: Storage) -> XMirrorSummary:
    if not config.x_publishing.enabled:
        return XMirrorSummary()
    if not _X_MIRROR_LOCK.acquire(blocking=False):
        return XMirrorSummary()
    try:
        return _process_x_mirroring_locked(config, storage)
    finally:
        _X_MIRROR_LOCK.release()


def _process_x_mirroring_locked(config: AppConfig, storage: Storage) -> XMirrorSummary:
    publication_paused = storage.publication_is_paused()
    paused_until = storage.x_rate_limit_pause_until()
    max_actions = config.x_publishing.max_per_run
    rows = storage.x_mirror_queue(max(20, max_actions * 5))
    if getattr(getattr(config, "production", None), "mode", "continuous") == "daily_digest":
        rows = [row for row in rows if str(row["x_status"]) in {"needs_delete", "delete_failed"}]
    warnings: list[str] = []
    if paused_until is not None and not publication_paused:
        skipped, preserved = storage.skip_pending_x_publications(
            "daily X publishing limit is active"
        )
        suffix = ""
        if skipped or preserved:
            suffix = f"; skipped {skipped}, retained {preserved} priority mirror(s)"
        warnings.append(
                "X publishing is paused after its daily limit until "
                f"{paused_until.astimezone().strftime('%Y-%m-%d %H:%M')}{suffix}"
        )
        rows = [
            row
            for row in rows
            if str(row["x_status"]) in {"needs_delete", "delete_failed"}
        ]
    if not rows:
        return XMirrorSummary(warnings=tuple(warnings))
    if publication_paused:
        rows = [
            row
            for row in rows
            if str(row["x_status"]) in {"needs_delete", "delete_failed"}
        ]
        if not rows:
            return XMirrorSummary(warnings=("CEO_PUBLICATION_PAUSE",))

    published = replaced = deleted = 0
    configured_sources = getattr(config, "sources", ())
    sources_by_id = {
        str(source.id): source
        for source in configured_sources
        if getattr(source, "id", None)
    }
    pre_send_rows = []
    sanitized_by_item: dict[int, str] = {}
    for row in rows:
        status = str(row["x_status"])
        if status in {"needs_delete", "delete_failed"} or not configured_sources:
            pre_send_rows.append(row)
            continue
        configured_source = sources_by_id.get(str(row["source_id"]))
        source_name = (
            str(configured_source.name)
            if configured_source is not None
            else str(row["source_id"])
        )
        try:
            sanitized_by_item[int(row["id"])] = sanitize_simple_publication_text(
                str(row["draft_text"] or row["x_text"] or ""),
                source_name=source_name,
                max_chars=(
                    int(config.editor.max_output_chars)
                    if hasattr(config, "editor")
                    and hasattr(config.editor, "max_output_chars")
                    else 1200
                ),
            )
        except ValueError as exc:
            item_id = int(row["id"])
            storage.mark_x_skipped(
                item_id,
                str(exc),
            )
            warnings.append(f"X mirror item {item_id} blocked by SIMPLE_OUTPUT_SANITIZER")
            continue
        pre_send_rows.append(row)
    rows = pre_send_rows
    if not rows:
        return XMirrorSummary(warnings=tuple(warnings))
    try:
        with XBrowserSession(
            config.x_publishing.profile_directory,
            headless=config.x_publishing.headless,
        ) as browser:
            actions = 0
            for row in rows:
                if actions >= max_actions:
                    break
                item_id = int(row["id"])
                status = str(row["x_status"])
                post_url = str(row["x_post_url"] or "")
                try:
                    if status in {"needs_delete", "delete_failed"}:
                        if int(row["x_attempts"] or 0) >= 3:
                            storage.mark_x_skipped(
                                item_id,
                                "old X post could not be deleted automatically; remove it manually",
                            )
                            warnings.append(
                                f"X mirror item {item_id} skipped after repeated delete failures"
                            )
                            continue
                        if post_url:
                            try:
                                browser.delete(post_url)
                            except XPostAlreadyDeleted:
                                pass
                        storage.mark_x_deleted(item_id)
                        deleted += 1
                        actions += 1
                        continue

                    if int(row["x_attempts"] or 0) >= 3:
                        storage.mark_x_skipped(
                            item_id,
                            "X delivery failed after three channel-only attempts",
                        )
                        warnings.append(
                            f"X mirror item {item_id} skipped after repeated delivery failures"
                        )
                        continue

                    was_replacement = status in {"needs_replace", "replace_failed"}
                    raw_text = sanitized_by_item.get(
                        item_id,
                        str(row["x_text"] or row["draft_text"] or ""),
                    )
                    source_quote_url = None
                    quote_canary = storage.x_source_quote_canary_status()
                    configured_source = sources_by_id.get(str(row["source_id"]))
                    canonical_url = str(row["canonical_url"] or "").strip()
                    quote_story_is_fresh = False
                    if quote_canary:
                        try:
                            activated_at = datetime.fromisoformat(
                                str(quote_canary.get("activated_at") or "")
                            ).astimezone(timezone.utc)
                            telegram_published_at = datetime.fromisoformat(
                                str(row["telegram_published_at"] or "")
                            ).astimezone(timezone.utc)
                            quote_story_is_fresh = telegram_published_at >= activated_at
                        except ValueError:
                            quote_story_is_fresh = False
                    if (
                        not was_replacement
                        and quote_canary
                        and bool(quote_canary.get("active"))
                        and int(quote_canary.get("remaining") or 0) > 0
                        and quote_story_is_fresh
                        and configured_source is not None
                        and str(getattr(configured_source, "platform", "")).casefold() == "x"
                        and str(row["media_url"] or "").strip()
                        and _EXACT_X_POST_URL.fullmatch(canonical_url)
                    ):
                        source_quote_url = canonical_url
                    if hasattr(config, "editor"):
                        translated = OllamaTextEditor(config.editor).translate_for_x(
                            raw_text,
                            config.x_publishing.max_chars,
                        )
                    else:
                        # Keeps the lightweight test and standalone utility
                        # configuration backward-compatible.
                        translated = raw_text
                    quote_suffix = f"\n\n{source_quote_url}" if source_quote_url else ""
                    text = compact_for_x(
                        translated,
                        max(1, config.x_publishing.max_chars - len(quote_suffix)),
                        topic_text=(
                            f"{str(row['source_text'] or '')}\n{raw_text}"
                        ).strip(),
                    ) + quote_suffix
                    if hasattr(config, "editor") and not _ENTITY_HASHTAG_RE.search(text):
                        storage.mark_x_skipped(
                            item_id,
                            "X_HASHTAGS_UNRESOLVED: no factual person, club or competition",
                        )
                        warnings.append(
                            f"X mirror item {item_id} skipped: factual hashtags unresolved"
                        )
                        actions += 1
                        continue
                    if was_replacement and post_url:
                        try:
                            browser.edit(post_url, text)
                            storage.mark_x_published(item_id, post_url, text)
                            replaced += 1
                            actions += 1
                            continue
                        except XEditUnavailable:
                            # Editing is account-level functionality in X. If
                            # it is unavailable, retain the established
                            # replacement fallback instead of losing the fix.
                            browser.delete(post_url)
                            storage.mark_x_ready_to_republish(item_id)

                    media_path = None
                    stored_image = str(row["image_path"] or "").strip()
                    if stored_image and source_quote_url is None:
                        candidate_media = Path(stored_image)
                        if candidate_media.is_file():
                            media_path = candidate_media
                        else:
                            warnings.append(
                                f"X mirror item {item_id} image missing; publishing text-only"
                            )
                    url = (
                        browser.post(text, media_path=media_path)
                        if media_path is not None
                        else browser.post(text)
                    )
                    if not _EXACT_X_POST_URL.fullmatch(url):
                        raise XDeliveryUnverified(
                            "X_DELIVERY_UNVERIFIED: adapter returned no exact status URL"
                        )
                    storage.mark_x_published(item_id, url, text)
                    if source_quote_url is not None:
                        try:
                            storage.mark_x_source_quote_canary_published(
                                item_id,
                                source_url=source_quote_url,
                                x_post_url=url,
                            )
                        except ValueError as exc:
                            warnings.append(
                                f"X source quote canary item {item_id} state warning: {exc}"
                            )
                    actions += 1
                    if was_replacement:
                        replaced += 1
                    else:
                        published += 1
                except XAuthenticationRequired:
                    raise
                except XDailyPostLimitReached as exc:
                    # Keep this exact row pending and leave every later row
                    # untouched. X explicitly reports a time-based limit, so
                    # retrying in the next monitor cycle only opens drafts and
                    # makes the account look automated.
                    pause_minutes = getattr(
                        config.x_publishing, "rate_limit_pause_minutes", 180
                    )
                    from .models import utc_now

                    until = utc_now() + timedelta(minutes=int(pause_minutes))
                    storage.pause_x_publishing_until(until)
                    skipped, preserved = storage.skip_pending_x_publications(
                        "daily X publishing limit reached"
                    )
                    warnings.append(
                        "X daily post limit reached; queue paused until "
                        f"{until.astimezone().strftime('%Y-%m-%d %H:%M')}; "
                        f"skipped {skipped}, retained {preserved} priority mirror(s)"
                    )
                    break
                except XDuplicatePost as exc:
                    # A previous run may have posted successfully just before
                    # an X response timeout. Retrying identical text produces
                    # X's "you already said that" notice forever, so retire
                    # this mirror row and let the next queued news continue.
                    storage.mark_x_skipped(item_id, "already published on X")
                    warnings.append(f"X mirror item {item_id} skipped: {exc}")
                    actions += 1
                except XDeliveryUnverified as exc:
                    # The UI may have accepted the post. Retrying without an
                    # exact ID can create a duplicate, so fail closed and keep
                    # the row explicitly non-SENT.
                    storage.mark_x_skipped(item_id, str(exc))
                    warnings.append(f"X mirror item {item_id} unverified: {exc}")
                    actions += 1
                except EditorError as exc:
                    # The Telegram post has already been published. A local
                    # translation failure must not hold newer X mirrors.
                    storage.mark_x_skipped(item_id, f"English translation failed: {exc}")
                    warnings.append(f"X mirror item {item_id} skipped: {exc}")
                    actions += 1
                except XBrowserError as exc:
                    action = (
                        "delete"
                        if status in {"needs_delete", "delete_failed"}
                        else "replace"
                        if status in {"needs_replace", "replace_failed"}
                        else "publish"
                    )
                    storage.mark_x_failed(item_id, action, str(exc))
                    warnings.append(f"X mirror item {item_id} failed: {exc}")
                    actions += 1
                except Exception as exc:
                    storage.mark_x_skipped(item_id, f"unexpected publish error: {exc}")
                    warnings.append(
                        f"X mirror item {item_id} skipped unexpectedly: {exc}"
                    )
                    actions += 1

    except (XAuthenticationRequired, XBrowserError) as exc:
        warnings.append(str(exc))

    return XMirrorSummary(
        published=published,
        replaced=replaced,
        deleted=deleted,
        warnings=tuple(warnings),
    )
