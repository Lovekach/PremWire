from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence
from urllib.parse import urljoin, urlsplit

from .dzen_lifecycle import (
    DZEN_CACHE_PATH,
    DZEN_LOG_PATH,
    DZEN_PROFILE_PATH,
    DZEN_RUNTIME_ROOT,
    DZEN_TEMP_PATH,
)


DZEN_PUBLISH_RUNTIME_ROOT = DZEN_RUNTIME_ROOT
DZEN_PUBLISH_PROFILE_PATH = DZEN_PROFILE_PATH
DZEN_PUBLISH_TEMP_PATH = DZEN_TEMP_PATH
DZEN_PUBLISH_CACHE_PATH = DZEN_CACHE_PATH
DZEN_PUBLISH_LOG_PATH = DZEN_LOG_PATH
DZEN_AUTH_HOME_URL = "https://dzen.ru/"
DZEN_PUBLISH_STUDIO_URL = "https://dzen.ru/profile/editor/create"
DZEN_PROFILE_API_URL = "https://dzen.ru/api/v3/launcher/export"

_ARTICLE_URL = re.compile(r"^https://dzen\.ru/a/([A-Za-z0-9_-]+)(?:\?.*)?$")
_CHANNEL_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")


class DzenPublishState(str, Enum):
    """Stable states which a caller may persist in its own ledger."""

    PUBLISHED = "published"
    RECONCILED = "reconciled"
    AUTH_REQUIRED = "auth_required"
    TRANSPORT_ERROR = "transport_error"
    AMBIGUOUS = "ambiguous"
    CHANNEL_UNVERIFIED = "channel_unverified"


class DzenPublisherError(RuntimeError):
    state = DzenPublishState.TRANSPORT_ERROR


class DzenPublisherAuthRequired(DzenPublisherError):
    state = DzenPublishState.AUTH_REQUIRED


class DzenPublisherTransportError(DzenPublisherError):
    state = DzenPublishState.TRANSPORT_ERROR


class DzenPublisherAmbiguous(DzenPublisherError):
    """Delivery or identity cannot be proved; callers must not retry blindly."""

    state = DzenPublishState.AMBIGUOUS


class DzenPublisherChannelUnverified(DzenPublisherError):
    state = DzenPublishState.CHANNEL_UNVERIFIED


def _normalise_whitespace(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return " ".join(folded.split())


def normalise_dzen_body(value: str) -> str:
    """Normalise rendering-only whitespace while retaining every factual token."""

    return _normalise_whitespace(value)


def render_dzen_plain_body(value: str) -> str:
    """Turn the deterministic digest markup into clean editor text.

    The Dzen composer is contenteditable rather than a Markdown editor.  Raw
    ``##`` and ``**`` markers would otherwise become visible publication
    artefacts, so strip only those presentation markers and preserve wording.
    """

    rendered: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            line = line[3:].strip()
        if line.startswith("- "):
            line = line[2:].strip()
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        rendered.append(line)
    return "\n".join(rendered).strip()


def dzen_body_hash(value: str) -> str:
    """Fingerprint the complete normalised article body, never an excerpt."""

    return hashlib.sha256(normalise_dzen_body(value).encode("utf-8")).hexdigest()


def dzen_digest_marker(digest_date: date) -> str:
    """Return the canonical date key used by the digest ledger."""

    return digest_date.isoformat()


@dataclass(frozen=True, slots=True)
class DzenArticleDraft:
    digest_date: date
    headline: str
    body: str

    def __post_init__(self) -> None:
        if not _normalise_whitespace(self.headline):
            raise ValueError("DZEN_HEADLINE_EMPTY")
        rendered_body = render_dzen_plain_body(self.body)
        if not normalise_dzen_body(rendered_body):
            raise ValueError("DZEN_BODY_EMPTY")
        object.__setattr__(self, "headline", _normalise_whitespace(self.headline))
        object.__setattr__(self, "body", rendered_body)

    @property
    def marker(self) -> str:
        return dzen_digest_marker(self.digest_date)

    @property
    def title(self) -> str:
        return self.headline

    @property
    def body_hash(self) -> str:
        return dzen_body_hash(self.body)


@dataclass(frozen=True, slots=True)
class DzenNativeArticle:
    url: str
    title: str
    body: str

    @property
    def article_id(self) -> str:
        match = _ARTICLE_URL.fullmatch(self.url)
        if not match:
            raise ValueError("DZEN_ARTICLE_URL_INVALID")
        return match.group(1)

    @property
    def body_hash(self) -> str:
        return dzen_body_hash(self.body)


@dataclass(frozen=True, slots=True)
class DzenPublishResult:
    state: DzenPublishState
    article_id: str
    article_url: str
    title: str
    body_hash: str


class DzenArticlePublisherPort(Protocol):
    """Minimal at-most-once native article publishing boundary."""

    def __enter__(self) -> "DzenArticlePublisherPort": ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...
    def verify_authenticated_channel(self) -> None: ...
    def list_recent_articles(self) -> tuple[DzenNativeArticle, ...]: ...
    def create_native_article(self, title: str, body: str) -> None: ...


def verify_dzen_channel_identity(
    current_url: str,
    *,
    expected_slug: str,
    expected_title: str,
    visible_identity_values: Sequence[str],
    create_control_visible: bool,
) -> None:
    """Prove management access to one exact channel or fail closed.

    A matching word somewhere in the page body is deliberately insufficient.
    The management URL, an exact visible identity field and an authoring control
    must all agree before the adapter is allowed to create anything.
    """

    if not _CHANNEL_SLUG.fullmatch(expected_slug):
        raise ValueError("DZEN_CHANNEL_SLUG_INVALID")
    parsed = urlsplit(current_url)
    expected_path = f"/profile/editor/{expected_slug}".casefold()
    actual_path = parsed.path.rstrip("/").casefold()
    path_matches = actual_path == expected_path or actual_path.startswith(
        expected_path + "/"
    )
    # Current Dzen routes the authenticated author workspace through a generic
    # /profile/editor/create entry point. Exact channel identity and an actual
    # authoring control are still required below, so accepting this management
    # route does not weaken channel ownership verification.
    path_matches = path_matches or actual_path == "/profile/editor/create"
    exact_title = _normalise_whitespace(expected_title).casefold()
    identity_matches = any(
        _normalise_whitespace(value).casefold() == exact_title
        for value in visible_identity_values
    )
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "dzen.ru"
        or not path_matches
        or not identity_matches
        or not create_control_visible
    ):
        reasons: list[str] = []
        if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != "dzen.ru":
            reasons.append("host")
        if not path_matches:
            reasons.append(f"path={parsed.path}")
        if not identity_matches:
            reasons.append("identity")
        if not create_control_visible:
            reasons.append("create_control")
        raise DzenPublisherChannelUnverified(
            "DZEN_PUBLISH_CHANNEL_IDENTITY_UNVERIFIED: " + ",".join(reasons)
        )


def reconcile_exact_native_article(
    draft: DzenArticleDraft,
    candidates: Sequence[DzenNativeArticle],
) -> DzenNativeArticle | None:
    """Resolve one exact article using its full title and complete body hash.

    A reused title with different content is a conflict rather than permission
    to create a second article. Invalid candidate URLs also fail closed.
    """

    exact_title = _normalise_whitespace(draft.title).casefold()
    matching_title: list[DzenNativeArticle] = []
    for candidate in candidates:
        try:
            candidate.article_id
        except ValueError as exc:
            raise DzenPublisherAmbiguous(
                "DZEN_RECONCILIATION_INVALID_ARTICLE_URL"
            ) from exc
        if _normalise_whitespace(candidate.title).casefold() == exact_title:
            matching_title.append(candidate)

    if len(matching_title) > 1:
        raise DzenPublisherAmbiguous("DZEN_RECONCILIATION_MULTIPLE_EXACT_TITLES")
    if not matching_title:
        return None

    candidate = matching_title[0]
    if candidate.body_hash != draft.body_hash:
        raise DzenPublisherAmbiguous("DZEN_RECONCILIATION_BODY_HASH_CONFLICT")
    return candidate


def publish_or_reconcile_dzen_article(
    browser: DzenArticlePublisherPort,
    draft: DzenArticleDraft,
    *,
    reconciliation_attempts: int = 3,
    reconciliation_delay_seconds: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> DzenPublishResult:
    """Create at most one article and return only after exact reconciliation.

    The caller owns the browser context and persistence. If submit may have
    happened but no exact URL can be proved, ``DzenPublisherAmbiguous`` is
    raised. Retrying that state without a later reconciliation is unsafe.
    """

    if reconciliation_attempts < 1:
        raise ValueError("DZEN_RECONCILIATION_ATTEMPTS_INVALID")
    if reconciliation_delay_seconds < 0:
        raise ValueError("DZEN_RECONCILIATION_DELAY_INVALID")

    browser.verify_authenticated_channel()
    existing = reconcile_exact_native_article(draft, browser.list_recent_articles())
    if existing is not None:
        return DzenPublishResult(
            DzenPublishState.RECONCILED,
            existing.article_id,
            existing.url,
            draft.title,
            draft.body_hash,
        )

    try:
        browser.create_native_article(draft.title, draft.body)
    except DzenPublisherAmbiguous as submit_error:
        # Submit was attempted. Reconcile once more, but never issue another
        # create command when delivery is uncertain.
        try:
            candidate = reconcile_exact_native_article(
                draft, browser.list_recent_articles()
            )
        except DzenPublisherError:
            raise submit_error
        if candidate is None:
            raise submit_error
        return DzenPublishResult(
            DzenPublishState.RECONCILED,
            candidate.article_id,
            candidate.url,
            draft.title,
            draft.body_hash,
        )

    for attempt in range(reconciliation_attempts):
        if attempt:
            sleeper(reconciliation_delay_seconds)
        candidate = reconcile_exact_native_article(
            draft, browser.list_recent_articles()
        )
        if candidate is not None:
            return DzenPublishResult(
                DzenPublishState.PUBLISHED,
                candidate.article_id,
                candidate.url,
                draft.title,
                draft.body_hash,
            )
    raise DzenPublisherAmbiguous("DZEN_PUBLISH_DELIVERY_AMBIGUOUS")


class PlaywrightDzenArticlePublisher:
    """Native Dzen article adapter using an isolated persistent profile."""

    def __init__(
        self,
        *,
        headless: bool = True,
        expected_channel_slug: str = "premwire",
        expected_channel_title: str = "PremWire",
        studio_url: str = DZEN_PUBLISH_STUDIO_URL,
        profile_path: Path = DZEN_PUBLISH_PROFILE_PATH,
    ) -> None:
        if not _CHANNEL_SLUG.fullmatch(expected_channel_slug):
            raise ValueError("DZEN_CHANNEL_SLUG_INVALID")
        self.headless = headless
        self.expected_channel_slug = expected_channel_slug
        self.expected_channel_title = expected_channel_title
        self.studio_url = studio_url
        self.profile_path = profile_path
        self._playwright = None
        self.context = None
        self.page = None

    def __enter__(self) -> "PlaywrightDzenArticlePublisher":
        for path in (
            self.profile_path,
            DZEN_PUBLISH_TEMP_PATH,
            DZEN_PUBLISH_CACHE_PATH,
            DZEN_PUBLISH_LOG_PATH,
        ):
            path.mkdir(parents=True, exist_ok=True)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - production dependency
            raise DzenPublisherTransportError(
                "DZEN_PUBLISH_PLAYWRIGHT_UNAVAILABLE"
            ) from exc
        self._playwright = sync_playwright().start()
        browser_env = dict(os.environ)
        browser_env.update(
            {
                "TEMP": str(DZEN_PUBLISH_TEMP_PATH),
                "TMP": str(DZEN_PUBLISH_TEMP_PATH),
                "XDG_CACHE_HOME": str(DZEN_PUBLISH_CACHE_PATH),
                "PLAYWRIGHT_BROWSERS_PATH": str(
                    DZEN_PUBLISH_CACHE_PATH / "playwright"
                ),
            }
        )
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_path),
                channel="chrome",
                headless=self.headless,
                chromium_sandbox=True,
                locale="ru-RU",
                viewport={"width": 1365, "height": 960},
                env=browser_env,
                args=["--disable-notifications", "--disable-background-mode"],
            )
            self.page = (
                self.context.pages[0]
                if self.context.pages
                else self.context.new_page()
            )
        except Exception as exc:
            try:
                self._playwright.stop()
            finally:
                self._playwright = None
            raise DzenPublisherTransportError(
                f"DZEN_PUBLISH_BROWSER_START_FAILED: {exc}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.context is not None:
                try:
                    self.context.close()
                except Exception:
                    pass
        finally:
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self.context = self.page = self._playwright = None

    def _require_page(self):
        if self.page is None:
            raise DzenPublisherTransportError("DZEN_PUBLISH_BROWSER_NOT_OPEN")
        return self.page

    @staticmethod
    def _auth_required(url: str, body: str) -> bool:
        folded_url = url.casefold()
        folded_body = body.casefold()
        return "passport.yandex" in folded_url or any(
            marker in folded_body
            for marker in ("войти в аккаунт", "log in", "sign in")
        )

    @staticmethod
    def _first_visible(page, selectors: Sequence[str]):
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 20)):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        return None

    def current_page_requires_auth(self) -> bool:
        """Inspect the current page for login state without navigating it."""

        page = self._require_page()
        try:
            body = page.locator("body").inner_text(timeout=10_000)
        except Exception as exc:
            raise DzenPublisherTransportError(
                f"DZEN_PUBLISH_CURRENT_PAGE_UNREADABLE: {exc}"
            ) from exc
        login_control = self._first_visible(
            page,
            (
                'button:has-text("Войти")',
                'a:has-text("Войти")',
                'button:has-text("Log in")',
                'a:has-text("Log in")',
                'button:has-text("Sign in")',
                'a:has-text("Sign in")',
            ),
        )
        return self._auth_required(page.url, body) or login_control is not None

    def current_account_channel_identity(self) -> tuple[str | None, str | None]:
        """Read the authenticated account's author channel without navigation."""

        page = self._require_page()
        try:
            response = page.request.get(
                DZEN_PROFILE_API_URL,
                headers={"Accept": "application/json", "Referer": DZEN_AUTH_HOME_URL},
                timeout=15_000,
            )
            if response.status != 200:
                return None, None
            payload = response.json()
        except Exception as exc:
            raise DzenPublisherTransportError(
                f"DZEN_PUBLISH_PROFILE_CHECK_FAILED: {exc}"
            ) from exc
        source = payload.get("current_user_source_info")
        if not isinstance(source, dict):
            return None, None
        source_id = source.get("id")
        title = source.get("title")
        return (
            str(source_id).strip() if source_id is not None else None,
            str(title).strip() if title is not None else None,
        )

    def _verify_current_studio_page(self) -> None:
        """Verify the already loaded page; this method never changes its URL."""

        page = self._require_page()
        try:
            body = page.locator("body").inner_text(timeout=10_000)
        except Exception as exc:
            raise DzenPublisherTransportError(
                f"DZEN_PUBLISH_CURRENT_PAGE_UNREADABLE: {exc}"
            ) from exc
        if self._auth_required(page.url, body):
            raise DzenPublisherAuthRequired("DZEN_PUBLISH_AUTH_REQUIRED")

        identity_values: list[str] = []
        source_id, api_title = self.current_account_channel_identity()
        if source_id and api_title:
            identity_values.append(api_title)
        identity_selectors = (
            '[data-testid="channel-name"]',
            '[data-testid="profile-name"]',
            '[class*="channelName"]',
            '[class*="channel-name"]',
            '[class*="profileName"]',
            '[class*="profile-name"]',
            f'a[href="/{self.expected_channel_slug}"]',
            f'a[href="/profile/editor/{self.expected_channel_slug}"]',
            f'[aria-label="{self.expected_channel_title}"]',
        )
        for selector in identity_selectors:
            matches = page.locator(selector)
            for index in range(min(matches.count(), 10)):
                try:
                    text = matches.nth(index).inner_text(timeout=1_500)
                except Exception:
                    continue
                if text:
                    identity_values.append(text)

        create_control = self._first_visible(
            page,
            (
                '[data-testid="add-publication-button"]',
                'button:has-text("Создать")',
                'a:has-text("Создать")',
                'button:has-text("Create")',
                'a:has-text("Create")',
            ),
        )
        try:
            verify_dzen_channel_identity(
                page.url,
                expected_slug=self.expected_channel_slug,
                expected_title=self.expected_channel_title,
                visible_identity_values=identity_values,
                create_control_visible=create_control is not None,
            )
        except DzenPublisherChannelUnverified:
            # Keep one local diagnostic frame for selector/auth maintenance.
            # It is never uploaded and is overwritten on the next failed check.
            try:
                page.screenshot(
                    path=str(DZEN_PUBLISH_LOG_PATH / "dzen-preflight.png"),
                    full_page=False,
                )
            except Exception:
                pass
            raise

    def _open_verified_studio(self) -> None:
        page = self._require_page()
        try:
            page.goto(self.studio_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)
        except Exception as exc:
            raise DzenPublisherTransportError(
                f"DZEN_PUBLISH_NAVIGATION_FAILED: {exc}"
            ) from exc
        self._verify_current_studio_page()

    def verify_authenticated_channel(self) -> None:
        self._open_verified_studio()

    def verify_current_authenticated_channel(self) -> None:
        """Verify the current tab without reloading it (used during login)."""

        self._verify_current_studio_page()

    def _read_public_article(self, url: str) -> DzenNativeArticle:
        if self.context is None:
            raise DzenPublisherTransportError("DZEN_PUBLISH_BROWSER_NOT_OPEN")
        article_page = self.context.new_page()
        try:
            article_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            article_page.wait_for_timeout(1_000)
            visible_url = article_page.url.split("?", 1)[0]
            if visible_url != url.split("?", 1)[0] or not _ARTICLE_URL.fullmatch(
                visible_url
            ):
                raise DzenPublisherAmbiguous(
                    "DZEN_RECONCILIATION_ARTICLE_ID_UNVERIFIED"
                )
            title_node = self._first_visible(article_page, ("article h1", "main h1", "h1"))
            if title_node is None:
                raise DzenPublisherAmbiguous(
                    "DZEN_RECONCILIATION_FULL_TITLE_UNAVAILABLE"
                )
            title = title_node.inner_text(timeout=5_000).strip()

            body_root = self._first_visible(
                article_page,
                (
                    '[itemprop="articleBody"]',
                    '[data-testid="article-content"]',
                    "main article",
                    "article",
                ),
            )
            if body_root is None:
                raise DzenPublisherAmbiguous(
                    "DZEN_RECONCILIATION_FULL_BODY_UNAVAILABLE"
                )
            blocks = body_root.locator("h2, h3, p, blockquote, li")
            body_parts: list[str] = []
            for index in range(blocks.count()):
                block = blocks.nth(index)
                try:
                    value = block.inner_text(timeout=2_000).strip()
                except Exception:
                    continue
                if value:
                    body_parts.append(value)
            body = "\n\n".join(body_parts)
            if not normalise_dzen_body(body):
                raise DzenPublisherAmbiguous(
                    "DZEN_RECONCILIATION_FULL_BODY_UNAVAILABLE"
                )
            return DzenNativeArticle(visible_url, title, body)
        except DzenPublisherError:
            raise
        except Exception as exc:
            raise DzenPublisherTransportError(
                f"DZEN_RECONCILIATION_ARTICLE_READ_FAILED: {exc}"
            ) from exc
        finally:
            try:
                article_page.close(run_before_unload=False)
            except Exception:
                pass

    def list_recent_articles(self) -> tuple[DzenNativeArticle, ...]:
        self._open_verified_studio()
        page = self._require_page()
        try:
            anchors = page.locator('a[href*="/a/"]')
            urls: list[str] = []
            seen: set[str] = set()
            for index in range(min(anchors.count(), 50)):
                href = anchors.nth(index).get_attribute("href") or ""
                url = urljoin("https://dzen.ru", href).split("?", 1)[0]
                if url in seen or not _ARTICLE_URL.fullmatch(url):
                    continue
                seen.add(url)
                urls.append(url)
            return tuple(self._read_public_article(url) for url in urls)
        except DzenPublisherError:
            raise
        except Exception as exc:
            raise DzenPublisherTransportError(
                f"DZEN_RECONCILIATION_LIST_FAILED: {exc}"
            ) from exc

    def create_native_article(self, title: str, body: str) -> None:
        if not _normalise_whitespace(title) or not normalise_dzen_body(body):
            raise ValueError("DZEN_PUBLISH_ARTICLE_EMPTY")
        self._open_verified_studio()
        page = self._require_page()
        submit_attempted = False
        try:
            create = self._first_visible(
                page,
                (
                    'button:has-text("Создать")',
                    'a:has-text("Создать")',
                    'button:has-text("Create")',
                    'a:has-text("Create")',
                ),
            )
            if create is None:
                raise DzenPublisherChannelUnverified(
                    "DZEN_PUBLISH_CREATE_CONTROL_NOT_FOUND"
                )
            create.click()
            page.wait_for_timeout(500)
            article_option = self._first_visible(
                page,
                (
                    'text="Статья"',
                    'text="Article"',
                ),
            )
            if article_option is not None:
                article_option.click()

            title_editor = self._first_visible(
                page,
                (
                    'textarea[placeholder*="заголов" i]',
                    'input[placeholder*="заголов" i]',
                    '[contenteditable="true"][data-placeholder*="заголов" i]',
                    'textarea[placeholder*="title" i]',
                    'input[placeholder*="title" i]',
                ),
            )
            body_editor = self._first_visible(
                page,
                (
                    '[data-testid="article-editor"] [contenteditable="true"]',
                    '[contenteditable="true"][data-placeholder*="текст" i]',
                    '[contenteditable="true"][data-placeholder*="text" i]',
                    'article [contenteditable="true"]',
                ),
            )
            if title_editor is None or body_editor is None:
                raise DzenPublisherTransportError(
                    "DZEN_PUBLISH_EDITOR_CONTROLS_NOT_FOUND"
                )
            title_editor.fill(title)
            body_editor.fill(body)

            publish = self._first_visible(
                page,
                (
                    'button:has-text("Опубликовать")',
                    'button:has-text("Publish")',
                ),
            )
            if publish is None:
                raise DzenPublisherTransportError(
                    "DZEN_PUBLISH_SUBMIT_CONTROL_NOT_FOUND"
                )
            submit_attempted = True
            publish.click(no_wait_after=True)
            page.wait_for_timeout(750)

            # Dzen may show a final settings dialog. It is still part of the
            # same single submit operation, not a second publication attempt.
            dialog = page.locator('[role="dialog"]').last
            if dialog.count() and dialog.is_visible():
                confirm = self._first_visible(
                    dialog,
                    (
                        'button:has-text("Опубликовать")',
                        'button:has-text("Publish")',
                    ),
                )
                if confirm is None:
                    raise DzenPublisherAmbiguous(
                        "DZEN_PUBLISH_CONFIRM_CONTROL_NOT_FOUND_AFTER_SUBMIT"
                    )
                confirm.click(no_wait_after=True)
                page.wait_for_timeout(1_000)
        except (DzenPublisherAuthRequired, DzenPublisherChannelUnverified):
            raise
        except DzenPublisherAmbiguous:
            raise
        except Exception as exc:
            if submit_attempted:
                raise DzenPublisherAmbiguous(
                    f"DZEN_PUBLISH_SUBMIT_OUTCOME_AMBIGUOUS: {exc}"
                ) from exc
            if isinstance(exc, DzenPublisherTransportError):
                raise
            raise DzenPublisherTransportError(
                f"DZEN_PUBLISH_COMPOSE_FAILED: {exc}"
            ) from exc


__all__ = [
    "DZEN_PUBLISH_CACHE_PATH",
    "DZEN_PUBLISH_PROFILE_PATH",
    "DZEN_PUBLISH_RUNTIME_ROOT",
    "DZEN_PUBLISH_STUDIO_URL",
    "DZEN_PUBLISH_TEMP_PATH",
    "DzenArticleDraft",
    "DzenArticlePublisherPort",
    "DzenNativeArticle",
    "DzenPublishResult",
    "DzenPublishState",
    "DzenPublisherAmbiguous",
    "DzenPublisherAuthRequired",
    "DzenPublisherChannelUnverified",
    "DzenPublisherError",
    "DzenPublisherTransportError",
    "PlaywrightDzenArticlePublisher",
    "dzen_body_hash",
    "dzen_digest_marker",
    "normalise_dzen_body",
    "render_dzen_plain_body",
    "publish_or_reconcile_dzen_article",
    "reconcile_exact_native_article",
    "verify_dzen_channel_identity",
]
