from __future__ import annotations

import hashlib
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import AppConfig
from .dzen_digest import DigestSnapshot, build_daily_digest
from .dzen_lifecycle import DZEN_LOCK_PATH
from .dzen_publisher import (
    DZEN_AUTH_HOME_URL,
    DZEN_PUBLISH_STUDIO_URL,
    DzenArticleDraft,
    DzenArticlePublisherPort,
    DzenPublisherAmbiguous,
    DzenPublisherAuthRequired,
    DzenPublisherChannelUnverified,
    DzenPublisherError,
    DzenPublisherTransportError,
    PlaywrightDzenArticlePublisher,
    publish_or_reconcile_dzen_article,
    reconcile_exact_native_article,
)
from .process_lock import ProcessLock
from .storage import Storage


BrowserFactory = Callable[[], DzenArticlePublisherPort]


@dataclass(frozen=True, slots=True)
class DzenDigestRunResult:
    status: str
    digest_date: str | None = None
    source_count: int = 0
    story_count: int = 0
    article_url: str | None = None
    detail: str | None = None
    actionable: bool = False


def _default_browser_factory(config: AppConfig) -> BrowserFactory:
    return lambda: PlaywrightDzenArticlePublisher(
        headless=config.dzen_digest.headless,
        expected_channel_slug=config.dzen_digest.expected_channel_slug,
        expected_channel_title=config.dzen_digest.expected_channel_title,
    )


def _due_digest_date(config: AppConfig, now: datetime) -> date | None:
    local_now = now.astimezone(config.timezone)
    if local_now.timetz().replace(tzinfo=None) < config.dzen_digest.publish_at:
        return None
    candidate = local_now.date() - timedelta(days=1)
    if candidate < config.dzen_digest.activation_date:
        return None
    return candidate


def _day_window_utc(config: AppConfig, digest_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(digest_date, time.min, tzinfo=config.timezone)
    local_end = datetime.combine(
        digest_date + timedelta(days=1), time.min, tzinfo=config.timezone
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_set_hash(source_items: list[tuple[int, int, str, str]]) -> str:
    value = "\n".join(
        f"{item_id}:{message_id}:{text_hash}"
        for item_id, message_id, _text, text_hash in source_items
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _draft_from_row(row) -> DzenArticleDraft:
    return DzenArticleDraft(
        date.fromisoformat(str(row["digest_date"])),
        str(row["title"]),
        str(row["body"]),
    )


def preflight_dzen_digest(
    config: AppConfig,
    *,
    browser_factory: BrowserFactory | None = None,
    lock_path: Path = DZEN_LOCK_PATH,
) -> DzenDigestRunResult:
    """Read-only proof that the persistent profile owns the exact channel."""

    if not config.dzen_digest.enabled:
        return DzenDigestRunResult("DISABLED")
    lock = ProcessLock(lock_path)
    if not lock.acquire():
        return DzenDigestRunResult("BROWSER_BUSY", detail="DZEN_BROWSER_LOCKED")
    factory = browser_factory or _default_browser_factory(config)
    try:
        with factory() as browser:
            browser.verify_authenticated_channel()
        return DzenDigestRunResult("READY")
    except DzenPublisherAuthRequired as exc:
        return DzenDigestRunResult(
            "AUTH_REQUIRED", detail=str(exc), actionable=True
        )
    except DzenPublisherChannelUnverified as exc:
        return DzenDigestRunResult(
            "CHANNEL_UNVERIFIED", detail=str(exc), actionable=True
        )
    except DzenPublisherError as exc:
        return DzenDigestRunResult("UNAVAILABLE", detail=str(exc))
    finally:
        lock.release()


def authenticate_dzen_digest(
    config: AppConfig,
    *,
    max_wait_seconds: int = 900,
    initial_wait_seconds: int = 20,
    lock_path: Path = DZEN_LOCK_PATH,
) -> DzenDigestRunResult:
    """Open the isolated browser once and persist the owner's Dzen login."""

    if max_wait_seconds < 30:
        raise ValueError("DZEN_AUTH_WAIT_TOO_SHORT")
    lock = ProcessLock(lock_path)
    if not lock.acquire():
        return DzenDigestRunResult("BROWSER_BUSY", detail="DZEN_BROWSER_LOCKED")
    try:
        with PlaywrightDzenArticlePublisher(
            headless=False,
            expected_channel_slug=config.dzen_digest.expected_channel_slug,
            expected_channel_title=config.dzen_digest.expected_channel_title,
        ) as browser:
            page = browser.page
            if page is None:
                return DzenDigestRunResult(
                    "UNAVAILABLE", detail="DZEN_AUTH_BROWSER_NOT_OPEN"
                )
            navigation_error: Exception | None = None
            for _attempt in range(3):
                if browser.context is not None:
                    live_pages = [
                        candidate
                        for candidate in browser.context.pages
                        if not candidate.is_closed()
                    ]
                    if live_pages:
                        page = live_pages[-1]
                        browser.page = page
                try:
                    page.goto(
                        DZEN_AUTH_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    navigation_error = None
                    break
                except Exception as exc:
                    navigation_error = exc
                    page.wait_for_timeout(1_000)
            if navigation_error is not None:
                return DzenDigestRunResult(
                    "UNAVAILABLE",
                    detail=f"DZEN_AUTH_NAVIGATION_FAILED: {navigation_error}",
                )
            deadline = time_module.monotonic() + max_wait_seconds
            page.wait_for_timeout(min(initial_wait_seconds, max_wait_seconds) * 1000)
            last_error = "DZEN_AUTH_NOT_COMPLETED"
            saw_interactive_auth = False
            studio_navigation_done = False
            while time_module.monotonic() < deadline:
                if browser.context is not None:
                    live_pages = [
                        candidate
                        for candidate in browser.context.pages
                        if not candidate.is_closed()
                    ]
                    if live_pages:
                        page = live_pages[-1]
                        browser.page = page
                if page.is_closed():
                    return DzenDigestRunResult(
                        "AUTH_WINDOW_CLOSED", detail=last_error, actionable=True
                    )
                try:
                    source_id, source_title = (
                        browser.current_account_channel_identity()
                    )
                    expected_title = (
                        config.dzen_digest.expected_channel_title.strip().casefold()
                    )
                    if (
                        source_id
                        and source_title
                        and source_title.strip().casefold() == expected_title
                    ):
                        if not studio_navigation_done:
                            page.goto(
                                DZEN_PUBLISH_STUDIO_URL,
                                wait_until="domcontentloaded",
                                timeout=60_000,
                            )
                            studio_navigation_done = True
                            page.wait_for_timeout(2_000)
                        browser.verify_current_authenticated_channel()
                        return DzenDigestRunResult("READY")

                    if browser.current_page_requires_auth():
                        saw_interactive_auth = True
                        last_error = "DZEN_AUTH_WAITING_FOR_OWNER"
                        page.wait_for_timeout(2_000)
                        continue

                    if source_title:
                        last_error = (
                            "DZEN_AUTH_WRONG_CHANNEL: "
                            f"expected={config.dzen_digest.expected_channel_title},"
                            f"actual={source_title}"
                        )
                    elif saw_interactive_auth:
                        last_error = "DZEN_AUTH_ACCOUNT_HAS_NO_CHANNEL"
                    else:
                        last_error = "DZEN_AUTH_WAITING_FOR_ACCOUNT_SWITCH"
                    page.wait_for_timeout(2_000)
                except (DzenPublisherAuthRequired, DzenPublisherChannelUnverified) as exc:
                    last_error = str(exc)
                    page.wait_for_timeout(2_000)
                except DzenPublisherTransportError as exc:
                    # Login/account switching can briefly replace the document
                    # and make its body unavailable. Keep the stable window
                    # alive; production preflight remains fail-closed.
                    last_error = str(exc)
                    if page.is_closed():
                        return DzenDigestRunResult(
                            "AUTH_WINDOW_CLOSED",
                            detail=last_error,
                            actionable=True,
                        )
                    page.wait_for_timeout(2_000)
                except DzenPublisherError as exc:
                    return DzenDigestRunResult("UNAVAILABLE", detail=str(exc))
            return DzenDigestRunResult(
                "AUTH_TIMEOUT", detail=last_error, actionable=True
            )
    finally:
        lock.release()


def run_dzen_digest_once(
    config: AppConfig,
    storage: Storage,
    *,
    now: datetime | None = None,
    browser_factory: BrowserFactory | None = None,
    lock_path: Path = DZEN_LOCK_PATH,
) -> DzenDigestRunResult:
    """Publish at most one native daily article, with crash-safe reconciliation."""

    if not config.dzen_digest.enabled:
        return DzenDigestRunResult("DISABLED")
    if storage.publication_is_paused():
        return DzenDigestRunResult("PAUSED_BY_OWNER")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("DZEN_DIGEST_NOW_MUST_BE_TIMEZONE_AWARE")
    digest_date = _due_digest_date(config, current)
    if digest_date is None:
        return DzenDigestRunResult("NOT_DUE")
    date_key = digest_date.isoformat()
    row = storage.get_dzen_daily_digest(date_key)

    if row is None:
        window_start, window_end = _day_window_utc(config, digest_date)
        source_rows = storage.dzen_digest_source_rows(window_start, window_end)
        if len(source_rows) < config.dzen_digest.minimum_items:
            return DzenDigestRunResult(
                "INSUFFICIENT_SOURCE",
                date_key,
                source_count=len(source_rows),
                detail=(
                    f"minimum={config.dzen_digest.minimum_items}; "
                    f"available={len(source_rows)}"
                ),
            )
        snapshots = tuple(DigestSnapshot.from_mapping(item) for item in source_rows)
        digest = build_daily_digest(
            snapshots,
            digest_date,
            timezone_name=config.dzen_digest.timezone_name,
        )
        draft = DzenArticleDraft(digest_date, digest.title, digest.body_text)
        source_items = [
            (
                int(source_row["item_id"]),
                int(source_row["telegram_message_id"]),
                str(source_row["published_text"]),
                _source_hash(str(source_row["published_text"])),
            )
            for source_row in source_rows
        ]
        row = storage.create_dzen_daily_digest(
            digest_date=date_key,
            timezone_name=config.dzen_digest.timezone_name,
            window_start_utc=window_start,
            window_end_utc=window_end,
            source_set_hash=_source_set_hash(source_items),
            story_count=len(digest.stories),
            title=draft.title,
            body=draft.body,
            body_hash=draft.body_hash,
            source_items=source_items,
        )

    state = str(row["state"])
    common = {
        "digest_date": date_key,
        "source_count": int(row["source_count"]),
        "story_count": int(row["story_count"]),
    }
    if state == "published":
        return DzenDigestRunResult(
            "ALREADY_PUBLISHED",
            article_url=str(row["dzen_article_url"] or "") or None,
            **common,
        )
    if state == "failed_retryable" and row["next_attempt_at"]:
        retry_at = datetime.fromisoformat(str(row["next_attempt_at"]))
        if retry_at > current.astimezone(timezone.utc):
            return DzenDigestRunResult(
                "RETRY_WAIT", detail=retry_at.isoformat(), **common
            )
    if state not in {
        "ready",
        "failed_retryable",
        "publishing",
        "publish_unknown",
    }:
        return DzenDigestRunResult(
            "STATE_BLOCKED", detail=f"state={state}", actionable=True, **common
        )

    lock = ProcessLock(lock_path)
    if not lock.acquire():
        return DzenDigestRunResult(
            "BROWSER_BUSY", detail="DZEN_BROWSER_LOCKED", **common
        )
    factory = browser_factory or _default_browser_factory(config)
    digest_id = int(row["id"])
    draft = _draft_from_row(row)
    try:
        # A prior submit may already have succeeded.  Unknown states are
        # reconciliation-only forever: absence is not permission to create.
        if state in {"publishing", "publish_unknown"}:
            try:
                with factory() as browser:
                    browser.verify_authenticated_channel()
                    article = reconcile_exact_native_article(
                        draft, browser.list_recent_articles()
                    )
            except DzenPublisherAuthRequired as exc:
                return DzenDigestRunResult(
                    "AUTH_REQUIRED", detail=str(exc), actionable=True, **common
                )
            except DzenPublisherChannelUnverified as exc:
                return DzenDigestRunResult(
                    "CHANNEL_UNVERIFIED", detail=str(exc), actionable=True, **common
                )
            except DzenPublisherError as exc:
                return DzenDigestRunResult(
                    "PUBLISH_UNKNOWN", detail=str(exc), actionable=True, **common
                )
            if article is None:
                return DzenDigestRunResult(
                    "PUBLISH_UNKNOWN",
                    detail="EXACT_ARTICLE_NOT_FOUND; create remains disabled",
                    actionable=True,
                    **common,
                )
            storage.mark_dzen_daily_digest_published(
                digest_id, article.url, article.article_id, published_at=current
            )
            return DzenDigestRunResult(
                "RECONCILED", article_url=article.url, **common
            )

        if not storage.claim_dzen_daily_digest(digest_id, now=current):
            return DzenDigestRunResult("CLAIM_SKIPPED", **common)
        try:
            with factory() as browser:
                result = publish_or_reconcile_dzen_article(browser, draft)
        except DzenPublisherAmbiguous as exc:
            storage.mark_dzen_daily_digest_unknown(digest_id, str(exc))
            return DzenDigestRunResult(
                "PUBLISH_UNKNOWN", detail=str(exc), actionable=True, **common
            )
        except (DzenPublisherAuthRequired, DzenPublisherChannelUnverified) as exc:
            storage.mark_dzen_daily_digest_retryable(
                digest_id, str(exc), retry_at=current + timedelta(minutes=30)
            )
            return DzenDigestRunResult(
                exc.state.value.upper(), detail=str(exc), actionable=True, **common
            )
        except DzenPublisherError as exc:
            storage.mark_dzen_daily_digest_retryable(
                digest_id, str(exc), retry_at=current + timedelta(minutes=10)
            )
            return DzenDigestRunResult(
                "RETRY_SCHEDULED", detail=str(exc), **common
            )
        storage.mark_dzen_daily_digest_published(
            digest_id,
            result.article_url,
            result.article_id,
            published_at=current,
        )
        return DzenDigestRunResult(
            "PUBLISHED", article_url=result.article_url, **common
        )
    finally:
        lock.release()


__all__ = [
    "authenticate_dzen_digest",
    "DzenDigestRunResult",
    "preflight_dzen_digest",
    "run_dzen_digest_once",
]
