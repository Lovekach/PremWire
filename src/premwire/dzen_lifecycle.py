from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, Sequence

from .process_lock import ProcessLock
from .runtime_paths import runtime_path
from .storage import Storage


DZEN_RUNTIME_ROOT = runtime_path("dzen-browser")
DZEN_PROFILE_PATH = DZEN_RUNTIME_ROOT / "profile"
DZEN_TEMP_PATH = DZEN_RUNTIME_ROOT / "temp"
DZEN_CACHE_PATH = DZEN_RUNTIME_ROOT / "cache"
DZEN_LOG_PATH = DZEN_RUNTIME_ROOT / "logs"
DZEN_LOCK_PATH = DZEN_RUNTIME_ROOT / "dzen-browser.lock"
DZEN_STUDIO_URL = "https://dzen.ru/profile/editor/premwire"
DZEN_AUTH_STATE_KEY = "dzen_delete_automation_auth_required"
_ARTICLE_URL = re.compile(r"^https://dzen\.ru/a/([A-Za-z0-9_-]+)(?:\?.*)?$")


class DzenLifecycleError(RuntimeError):
    pass


class DzenAuthRequired(DzenLifecycleError):
    pass


class DzenAlreadyAbsent(DzenLifecycleError):
    pass


class DzenTransportError(DzenLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class DzenArticleCandidate:
    url: str
    title: str
    text: str
    published_at: datetime

    @property
    def article_id(self) -> str:
        match = _ARTICLE_URL.fullmatch(self.url)
        if not match:
            raise ValueError("DZEN_ARTICLE_URL_INVALID")
        return match.group(1)


@dataclass(frozen=True, slots=True)
class DzenLifecycleSummary:
    mapped: int = 0
    deleted: int = 0
    already_deleted: int = 0
    warnings: tuple[str, ...] = ()
    liked: int = 0


class DzenBrowserPort(Protocol):
    def __enter__(self) -> "DzenBrowserPort": ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...
    def list_recent_articles(self) -> tuple[DzenArticleCandidate, ...]: ...
    def like_exact(self, url: str, article_id: str) -> None: ...
    def delete_exact(self, url: str, article_id: str) -> None: ...


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    folded = re.sub(r"https?://\S+|@[A-Za-z0-9_]+", " ", folded)
    return " ".join("".join(char if char.isalnum() else " " for char in folded).split())


def dzen_text_fingerprint(value: str) -> str:
    return hashlib.sha256(_normalise(value).encode("utf-8")).hexdigest()


def select_exact_dzen_candidate(
    telegram_text: str,
    telegram_published_at: datetime,
    candidates: Sequence[DzenArticleCandidate],
    *,
    window: timedelta = timedelta(minutes=45),
) -> DzenArticleCandidate | None:
    """Return one deterministic mirror match or fail closed on ambiguity.

    Time alone is never enough. A candidate must contain the complete cleaned
    Telegram text, or its normalized title and body must reconstruct it exactly.
    """

    if telegram_published_at.tzinfo is None:
        raise ValueError("Telegram publication timestamp must include timezone")
    expected = _normalise(telegram_text)
    if not expected:
        return None
    start = telegram_published_at.astimezone(timezone.utc)
    end = telegram_published_at.astimezone(timezone.utc) + window
    matches: list[DzenArticleCandidate] = []
    for candidate in candidates:
        stamp = candidate.published_at.astimezone(timezone.utc)
        if not start <= stamp <= end:
            continue
        combined = _normalise(" ".join((candidate.title, candidate.text)))
        body = _normalise(candidate.text)
        if body == expected or combined == expected or expected in combined:
            matches.append(candidate)
    if len(matches) > 1:
        raise DzenLifecycleError("DZEN_ARTICLE_MAPPING_AMBIGUOUS")
    return matches[0] if matches else None


class PlaywrightDzenBrowser:
    """Delete-only Dzen adapter using a dedicated runtime-derived profile."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self.context = None
        self.page = None

    @staticmethod
    def prepare_runtime() -> None:
        for path in (
            DZEN_PROFILE_PATH, DZEN_TEMP_PATH, DZEN_CACHE_PATH, DZEN_LOG_PATH
        ):
            path.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "PlaywrightDzenBrowser":
        self.prepare_runtime()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - production dependency
            raise DzenLifecycleError("DZEN_PLAYWRIGHT_UNAVAILABLE") from exc
        self._playwright = sync_playwright().start()
        browser_env = dict(os.environ)
        browser_env.update({
            "TEMP": str(DZEN_TEMP_PATH),
            "TMP": str(DZEN_TEMP_PATH),
            "XDG_CACHE_HOME": str(DZEN_CACHE_PATH),
            "PLAYWRIGHT_BROWSERS_PATH": str(DZEN_CACHE_PATH / "playwright"),
        })
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(DZEN_PROFILE_PATH),
                channel="chrome",
                headless=self.headless,
                chromium_sandbox=True,
                locale="ru-RU",
                viewport={"width": 1280, "height": 900},
                env=browser_env,
                args=["--disable-notifications", "--disable-background-mode"],
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except Exception as exc:
            self._playwright.stop()
            raise DzenLifecycleError(f"DZEN_BROWSER_START_FAILED: {exc}") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self.context = self.page = self._playwright = None

    def _open_studio(self) -> None:
        if self.page is None:
            raise DzenLifecycleError("DZEN_BROWSER_NOT_OPEN")
        try:
            self.page.goto(
                DZEN_STUDIO_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as exc:
            raise DzenTransportError(f"DZEN_NAVIGATION_FAILED: {exc}") from exc
        self.page.wait_for_timeout(2_500)
        url = self.page.url.casefold()
        body = self.page.locator("body").inner_text(timeout=10_000).casefold()
        if "passport.yandex" in url or any(
            marker in body for marker in ("войти в аккаунт", "log in", "sign in")
        ):
            raise DzenAuthRequired("DZEN_AUTH_REQUIRED")
        if "premwire" not in body and "premwire" not in url:
            raise DzenLifecycleError("DZEN_CHANNEL_IDENTITY_UNVERIFIED")

    def verify_authenticated_channel(self) -> None:
        self._open_studio()

    def authentication_is_pending(self) -> bool:
        if self.page is None or self.page.is_closed():
            return True
        url = self.page.url.casefold()
        body = self.page.locator("body").inner_text(timeout=5_000).casefold()
        return "passport.yandex" in url or any(
            marker in body for marker in ("войти в аккаунт", "log in", "sign in")
        )

    def list_recent_articles(self) -> tuple[DzenArticleCandidate, ...]:
        self._open_studio()
        assert self.page is not None
        result: list[DzenArticleCandidate] = []
        anchors = self.page.locator('a[href*="/a/"]')
        seen: set[str] = set()
        for index in range(min(anchors.count(), 30)):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            url = href if href.startswith("http") else f"https://dzen.ru{href}"
            url = url.split("?", 1)[0]
            if url in seen or not _ARTICLE_URL.fullmatch(url):
                continue
            seen.add(url)
            card = anchor.locator("xpath=ancestor::*[self::article or self::div][1]")
            text = card.inner_text(timeout=5_000) if card.count() else anchor.inner_text()
            time_node = card.locator("time").first if card.count() else anchor.locator("time").first
            stamp_raw = time_node.get_attribute("datetime") if time_node.count() else None
            if not stamp_raw:
                continue
            try:
                stamp = datetime.fromisoformat(stamp_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            result.append(DzenArticleCandidate(url, anchor.inner_text().strip(), text, stamp))
        return tuple(result)

    @staticmethod
    def _like_control_state(control) -> tuple[bool, bool]:
        """Return (is_like_control, already_liked) from accessible UI state."""

        values: list[str] = []
        for name in ("aria-label", "title"):
            try:
                values.append(control.get_attribute(name) or "")
            except Exception:
                pass
        try:
            values.append(control.inner_text(timeout=1_000) or "")
        except Exception:
            pass
        signal = " ".join(values).casefold()
        pressed = (control.get_attribute("aria-pressed") or "").casefold() == "true"
        active = pressed or any(
            marker in signal
            for marker in ("unlike", "убрать лайк", "убрать отметку", "больше не нравится")
        )
        if "dislike" in signal or (
            "не нравится" in signal and "больше не нравится" not in signal
        ):
            return False, False
        is_like = active or "нравится" in signal or bool(
            re.search(r"(?:^|\s)like(?:\s|$)", signal)
        )
        return is_like, active

    def _find_like_control(self):
        assert self.page is not None
        controls = self.page.locator('button, [role="button"]')
        for index in range(min(controls.count(), 200)):
            control = controls.nth(index)
            is_like, active = self._like_control_state(control)
            if is_like:
                return control, active
        return None, False

    def like_exact(self, url: str, article_id: str) -> None:
        """Idempotently like only the exact Dzen article stored in the ledger."""

        match = _ARTICLE_URL.fullmatch(url)
        if not match or match.group(1) != article_id:
            raise DzenLifecycleError("DZEN_LIKE_EXACT_ID_MISMATCH")
        self._open_studio()
        assert self.page is not None
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            raise DzenTransportError(f"DZEN_NAVIGATION_FAILED: {exc}") from exc
        self.page.wait_for_timeout(1_500)
        visible = _ARTICLE_URL.fullmatch(self.page.url.split("?", 1)[0])
        if visible is None or visible.group(1) != article_id:
            raise DzenLifecycleError("DZEN_LIKE_ARTICLE_ID_UNVERIFIED")
        control, active = self._find_like_control()
        if control is None:
            raise DzenLifecycleError("DZEN_LIKE_CONTROL_NOT_FOUND")
        if active:
            return
        control.click(force=True, no_wait_after=True)
        self.page.wait_for_timeout(1_000)
        _, active = self._find_like_control()
        if not active:
            raise DzenLifecycleError("DZEN_LIKE_VERIFICATION_FAILED")

    def delete_exact(self, url: str, article_id: str) -> None:
        match = _ARTICLE_URL.fullmatch(url)
        if not match or match.group(1) != article_id:
            raise DzenLifecycleError("DZEN_DELETE_EXACT_ID_MISMATCH")
        self._open_studio()
        assert self.page is not None
        path = f"/a/{article_id}"
        anchor = self.page.locator(f'a[href^="{path}"]').first
        if not anchor.count():
            anchor = self.page.locator(f'a[href*="{path}"]').first
        if not anchor.count():
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            self.page.wait_for_timeout(1_500)
            body = self.page.locator("body").inner_text(timeout=5_000).casefold()
            explicitly_absent = any(
                marker in body
                for marker in (
                    "публикация удалена",
                    "страница не найдена",
                    "такой страницы нет",
                    "publication was deleted",
                    "page not found",
                )
            )
            if (response is not None and response.status in {404, 410}) or explicitly_absent:
                raise DzenAlreadyAbsent("DZEN_DELETE_ALREADY_APPLIED")
            raise DzenLifecycleError("DZEN_EXACT_ARTICLE_NOT_IN_MANAGEMENT_LIST")
        card = anchor.locator("xpath=ancestor::*[self::article or self::div][1]")
        card_text = card.inner_text(timeout=5_000).casefold()
        if "premwire" not in self.page.locator("body").inner_text(timeout=5_000).casefold():
            raise DzenLifecycleError("DZEN_CHANNEL_IDENTITY_UNVERIFIED")
        menu = card.locator(
            'button[aria-label*="меню" i], button[aria-label*="menu" i], '
            'button[aria-label*="действ" i]'
        ).first
        if not menu.count():
            raise DzenLifecycleError("DZEN_DELETE_CONTROL_NOT_FOUND")
        menu.click()
        delete = self.page.get_by_text("Удалить", exact=True)
        if not delete.count():
            delete = self.page.get_by_text("Delete", exact=True)
        if not delete.count():
            raise DzenLifecycleError("DZEN_DELETE_CONTROL_NOT_FOUND")
        delete.first.click()
        confirm = self.page.get_by_text("Удалить", exact=True).last
        if confirm.count():
            confirm.click()
        self.page.wait_for_timeout(2_000)
        if self.page.locator(f'a[href*="{path}"]').count():
            raise DzenLifecycleError("DZEN_DELETE_VERIFICATION_FAILED")


def process_dzen_lifecycle(
    storage: Storage,
    *,
    browser_factory=PlaywrightDzenBrowser,
    now: datetime | None = None,
) -> DzenLifecycleSummary:
    if storage.get_runtime_state(DZEN_AUTH_STATE_KEY) == "1":
        return DzenLifecycleSummary(warnings=("DZEN_AUTH_REQUIRED",))
    current = now or datetime.now(timezone.utc)
    mapping_rows = storage.dzen_reconciliation_queue(current, limit=1)
    delete_rows = storage.dzen_delete_queue(current, limit=1)
    like_rows = storage.dzen_like_queue(current, limit=3)
    if not mapping_rows and not delete_rows and not like_rows:
        return DzenLifecycleSummary()
    lock = ProcessLock(DZEN_LOCK_PATH)
    if not lock.acquire():
        return DzenLifecycleSummary(warnings=("DZEN_BROWSER_BUSY",))
    mapped = deleted = already_deleted = liked = 0
    warnings: list[str] = []
    try:
        with browser_factory(headless=True) as browser:
            candidates: tuple[DzenArticleCandidate, ...] | None = None
            for row in mapping_rows:
                item_id = int(row["id"])
                try:
                    candidates = candidates or browser.list_recent_articles()
                    stamp_raw = row["telegram_published_at"]
                    if not stamp_raw:
                        storage.mark_dzen_mapping_result(
                            item_id, "not_found", "DZEN_TELEGRAM_TIMESTAMP_MISSING"
                        )
                        continue
                    match = select_exact_dzen_candidate(
                        str(row["draft_text"] or ""),
                        datetime.fromisoformat(str(stamp_raw)),
                        candidates,
                    )
                    if match is None:
                        storage.mark_dzen_mapping_result(
                            item_id, "not_found", "DZEN_ARTICLE_MAPPING_NOT_FOUND"
                        )
                    else:
                        storage.mark_dzen_mapped(item_id, match.url, match.article_id)
                        mapped += 1
                        try:
                            browser.like_exact(match.url, match.article_id)
                            storage.mark_dzen_liked(item_id)
                            liked += 1
                        except DzenTransportError as exc:
                            storage.mark_dzen_like_failed(item_id, str(exc))
                            warnings.append(str(exc))
                            break
                        except DzenLifecycleError as exc:
                            storage.mark_dzen_like_failed(item_id, str(exc))
                            warnings.append(str(exc))
                except DzenAuthRequired as exc:
                    storage.mark_dzen_mapping_result(item_id, "auth_required", str(exc))
                    storage.set_runtime_state(DZEN_AUTH_STATE_KEY, "1")
                    warnings.append(str(exc))
                    break
                except DzenTransportError as exc:
                    warnings.append(str(exc))
                    break
                except DzenLifecycleError as exc:
                    status = "ambiguous" if "AMBIGUOUS" in str(exc) else "not_found"
                    storage.mark_dzen_mapping_result(item_id, status, str(exc))
                    warnings.append(str(exc))
            for row in like_rows:
                item_id = int(row["id"])
                try:
                    browser.like_exact(
                        str(row["dzen_article_url"]), str(row["dzen_article_id"])
                    )
                    storage.mark_dzen_liked(item_id)
                    liked += 1
                except DzenAuthRequired as exc:
                    storage.mark_dzen_like_failed(item_id, str(exc))
                    storage.set_runtime_state(DZEN_AUTH_STATE_KEY, "1")
                    warnings.append(str(exc))
                    break
                except DzenTransportError as exc:
                    storage.mark_dzen_like_failed(item_id, str(exc))
                    warnings.append(str(exc))
                    break
                except DzenLifecycleError as exc:
                    storage.mark_dzen_like_failed(item_id, str(exc))
                    warnings.append(str(exc))
            # Re-read: a tombstoned row can become delete-eligible after mapping.
            delete_rows = storage.dzen_delete_queue(current, limit=1)
            for row in delete_rows:
                item_id = int(row["id"])
                try:
                    browser.delete_exact(
                        str(row["dzen_article_url"]), str(row["dzen_article_id"])
                    )
                    storage.mark_dzen_deleted(item_id)
                    deleted += 1
                except DzenAlreadyAbsent:
                    storage.mark_dzen_deleted(item_id, already_absent=True)
                    already_deleted += 1
                except DzenAuthRequired as exc:
                    storage.mark_dzen_delete_auth_required(item_id)
                    storage.set_runtime_state(DZEN_AUTH_STATE_KEY, "1")
                    warnings.append(str(exc))
                    break
                except DzenTransportError as exc:
                    warnings.append(str(exc))
                    break
                except DzenLifecycleError as exc:
                    storage.mark_dzen_delete_failed(item_id, str(exc))
                    warnings.append(str(exc))
    finally:
        lock.release()
    return DzenLifecycleSummary(
        mapped=mapped,
        deleted=deleted,
        already_deleted=already_deleted,
        warnings=tuple(warnings),
        liked=liked,
    )


def run_auth_bootstrap(storage: Storage, *, max_wait_seconds: int = 900) -> int:
    PlaywrightDzenBrowser.prepare_runtime()
    lock = ProcessLock(DZEN_LOCK_PATH)
    if not lock.acquire():
        raise RuntimeError("DZEN_BROWSER_BUSY")
    try:
        with PlaywrightDzenBrowser(headless=False) as browser:
            assert browser.page is not None
            browser.page.goto(DZEN_STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
            print("DZEN_AUTH_WINDOW_OPEN. Complete Dzen login in the visible window.")
            deadline = time.monotonic() + max_wait_seconds
            while time.monotonic() < deadline:
                if browser.page.is_closed():
                    return 2
                if browser.authentication_is_pending():
                    browser.page.wait_for_timeout(2_000)
                    continue
                browser.verify_authenticated_channel()
                storage.clear_runtime_state(DZEN_AUTH_STATE_KEY)
                storage.reset_dzen_auth_required()
                print("DZEN_AUTH_READY")
                return 0
            return 3
    finally:
        lock.release()
    return 0


__all__ = [
    "DZEN_AUTH_STATE_KEY", "DZEN_CACHE_PATH", "DZEN_PROFILE_PATH", "DZEN_RUNTIME_ROOT",
    "DZEN_TEMP_PATH", "DzenAlreadyAbsent", "DzenArticleCandidate",
    "DzenAuthRequired", "DzenLifecycleError", "DzenLifecycleSummary",
    "DzenTransportError",
    "PlaywrightDzenBrowser", "dzen_text_fingerprint", "process_dzen_lifecycle",
    "run_auth_bootstrap", "select_exact_dzen_candidate",
]
