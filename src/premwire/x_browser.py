from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import time
from typing import Any

from .runtime_paths import runtime_path


os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", str(runtime_path("browser", "playwright"))
)


class XBrowserError(RuntimeError):
    pass


class XAuthenticationRequired(XBrowserError):
    pass


class XDuplicatePost(XBrowserError):
    """X reports that this exact text has already been published."""

    pass


class XDailyPostLimitReached(XBrowserError):
    """X has temporarily stopped this account from publishing new posts."""

    pass


class XEditUnavailable(XBrowserError):
    """The current X account or post does not expose post editing."""

    pass


class XDeliveryUnverified(XBrowserError):
    """The compose action completed without an exact X post identifier."""

    pass


class XPostAlreadyDeleted(XBrowserError):
    """The exact ledger-mapped X post no longer exists."""

    pass


def _tweet_id(payload: Any) -> str | None:
    try:
        result = payload["data"]["create_tweet"]["tweet_results"]["result"]
        value = result.get("rest_id")
        return str(value) if value else None
    except (KeyError, TypeError, AttributeError):
        return None


class XBrowserSession:
    """Publish through X's web interface using a dedicated local profile."""

    def __init__(self, profile_directory: Path, *, headless: bool) -> None:
        self.profile_directory = profile_directory
        self.headless = headless
        self._playwright = None
        self.context = None
        self.page = None

    def __enter__(self) -> "XBrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise XBrowserError(
                "Playwright is not installed in the PremWire runtime"
            ) from exc
        self.profile_directory.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_directory),
                channel="chrome",
                headless=self.headless,
                chromium_sandbox=True,
                locale="ru-RU",
                viewport={"width": 1280, "height": 900},
                ignore_default_args=[
                    "--password-store=basic",
                    "--use-mock-keychain",
                ],
                args=[
                    "--disable-notifications",
                    "--disable-background-mode",
                ],
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except Exception as exc:
            self._playwright.stop()
            raise XBrowserError(f"X browser could not start: {exc}") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.context is not None:
                for page in list(self.context.pages):
                    try:
                        page.close(run_before_unload=False)
                    except Exception:
                        pass
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
            self.page = None
            self.context = None
            self._playwright = None

    def _require_page(self):
        if self.page is None:
            raise XBrowserError("X browser session is not open")
        return self.page

    def is_authenticated(self) -> bool:
        page = self._require_page()
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
        for _ in range(12):
            page.wait_for_timeout(2_500)
            if (
                page.locator('[data-testid="SideNav_NewTweet_Button"]').count()
                or page.locator('[data-testid="AppTabBar_Profile_Link"]').count()
                or page.locator('a[href="/compose/post"]').count()
            ):
                return True
        return False

    def _dismiss_composer_suggestions(self) -> None:
        """Close X autocomplete popups, especially hashtag suggestions.

        X often keeps a hashtag typeahead menu focused after text insertion.
        In that state a normal click may only select a suggestion instead of
        submitting the post. Escape is harmless when there is no popup, so use
        it twice and then move focus to the page before the forced submit.
        """
        page = self._require_page()
        page.keyboard.press("Escape")
        page.wait_for_timeout(180)
        page.keyboard.press("Escape")
        page.wait_for_timeout(180)

    def _enabled_submit_button(self, *, timeout_ms: int = 30_000):
        """Return X's visible enabled submit control, never a disabled sibling."""

        page = self._require_page()
        deadline = time.monotonic() + (timeout_ms / 1000)
        selectors = ('[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]')
        while time.monotonic() < deadline:
            for selector in selectors:
                buttons = page.locator(selector)
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if (
                        button.is_visible()
                        and button.get_attribute("aria-disabled") != "true"
                        and button.get_attribute("disabled") is None
                    ):
                        return button
            page.wait_for_timeout(200)
        raise XBrowserError("X publish button is disabled")

    def _resolve_exact_recent_post(self, text: str, created_after: datetime) -> str | None:
        """Resolve one just-created own post by exact text, never fuzzy matching."""

        page = self._require_page()
        profile_link = page.locator('[data-testid="AppTabBar_Profile_Link"]')
        href = profile_link.get_attribute("href") or ""
        if not re.fullmatch(r"/[A-Za-z0-9_]+", href):
            return None
        page.goto(f"https://x.com{href}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_500)
        threshold = created_after.astimezone(timezone.utc) - timedelta(minutes=1)
        matches: set[str] = set()
        articles = page.locator("article")
        for index in range(min(articles.count(), 12)):
            article = articles.nth(index)
            tweet_text = article.locator('[data-testid="tweetText"]').first
            if not tweet_text.count() or tweet_text.inner_text(timeout=5_000) != text:
                continue
            time_node = article.locator("time").first
            stamp = time_node.get_attribute("datetime") if time_node.count() else None
            if not stamp:
                continue
            try:
                created = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created < threshold:
                continue
            anchors = article.locator(f'a[href^="{href}/status/"]')
            for anchor_index in range(anchors.count()):
                status_href = anchors.nth(anchor_index).get_attribute("href") or ""
                if re.fullmatch(rf"{re.escape(href)}/status/[0-9]+", status_href):
                    matches.add(f"https://x.com{status_href}")
        if len(matches) == 1:
            return matches.pop()
        if len(matches) > 1:
            raise XDeliveryUnverified(
                "X_DELIVERY_UNVERIFIED: multiple exact recent status IDs found"
            )
        return None

    def post(self, text: str, media_path: Path | None = None) -> str:
        page = self._require_page()
        try:
            page.goto(
                "https://x.com/compose/post",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as exc:
            raise XBrowserError(f"X compose page could not be opened: {exc}") from exc
        editor = page.locator('[data-testid="tweetTextarea_0"]').first
        try:
            editor.wait_for(state="visible", timeout=45_000)
        except Exception as exc:
            raise XAuthenticationRequired(
                "X session is not authenticated; run premwire.cmd x-login"
            ) from exc
        editor.fill(text)
        if media_path is not None:
            selected = media_path.resolve()
            if not selected.is_file():
                raise XBrowserError(f"X media file does not exist: {selected}")
            uploader = page.locator('input[type="file"]').first
            try:
                uploader.set_input_files(str(selected), timeout=20_000)
                page.wait_for_timeout(2_000)
            except Exception as exc:
                raise XBrowserError(f"X media could not be attached: {exc}") from exc
        # Clicking a generic role=option is unsafe: X may replace the entire
        # composer with an unrelated suggestion. Escape closes autocomplete
        # without mutating the exact prepared publication text.
        self._dismiss_composer_suggestions()
        button = self._enabled_submit_button()
        post_id: str | None = None
        resolved_url: str | None = None
        submit_attempted = False
        publish_started = datetime.now(timezone.utc)

        def capture_post_id(response) -> None:
            """Record the GraphQL response when X exposes it, without relying on it."""
            nonlocal post_id
            if post_id or "CreateTweet" not in response.url:
                return
            try:
                if response.ok:
                    post_id = _tweet_id(response.json())
            except Exception:
                # Publishing is validated from the closed compose window below.
                pass

        page.on("response", capture_post_id)
        try:
            button.wait_for(state="visible", timeout=30_000)
            button.scroll_into_view_if_needed()
            # Do not wrap the click in expect_response. X changes its GraphQL
            # operation name frequently; the old wait left a successfully
            # filled compose window open and turned it into a draft. A click
            # followed by checking that the composer closes is reliable across
            # those frontend changes.
            submit_attempted = True
            button.click(force=True, no_wait_after=True)
            page.wait_for_timeout(1_250)
            if editor.is_visible():
                # Some Russian X sessions retain focus in the hashtag field
                # after a mouse click. Ctrl+Enter invokes the same final
                # publish action and is harmless if the first click succeeded.
                page.keyboard.press("Control+Enter")
            try:
                editor.wait_for(state="hidden", timeout=15_000)
            except Exception as exc:
                # X sometimes keeps the compose overlay visible after the
                # CreateTweet request has succeeded. The captured exact ID is
                # authoritative; do not turn that success into a retry.
                if post_id:
                    pass
                else:
                    try:
                        body_text = page.locator("body").inner_text(timeout=2_000).casefold()
                    except Exception:
                        body_text = ""
                    if any(
                        marker in body_text
                        for marker in (
                            "суточный лимит постов",
                            "daily limit of posts",
                            "daily post limit",
                            "daily publishing limit",
                            "you have reached your daily",
                        )
                    ):
                        raise XDailyPostLimitReached(
                            "X daily post limit reached"
                        ) from exc
                    if (
                        "вы это уже говорили" in body_text
                        or "you already said that" in body_text
                        or "duplicate post" in body_text
                    ):
                        raise XDuplicatePost(
                            "X reports that this text was already published"
                        ) from exc
                    resolved_url = self._resolve_exact_recent_post(text, publish_started)
                    if resolved_url is None:
                        raise XDeliveryUnverified(
                            "X_DELIVERY_UNVERIFIED: composer remained open and no exact post ID was captured"
                        ) from exc
        except XBrowserError:
            raise
        except Exception as exc:
            if submit_attempted:
                try:
                    resolved_url = self._resolve_exact_recent_post(text, publish_started)
                except XBrowserError:
                    raise
                if resolved_url:
                    return resolved_url
                raise XDeliveryUnverified(
                    "X_DELIVERY_UNVERIFIED: submit was attempted without an exact post ID/URL"
                ) from exc
            raise XBrowserError(f"X publish command failed: {exc}") from exc
        finally:
            try:
                page.remove_listener("response", capture_post_id)
            except Exception:
                pass

        if resolved_url:
            return resolved_url
        profile_link = page.locator('[data-testid="AppTabBar_Profile_Link"]')
        href = profile_link.get_attribute("href") or ""
        if post_id and href.startswith("/"):
            return f"https://x.com{href}/status/{post_id}"
        raise XDeliveryUnverified(
            "X_DELIVERY_UNVERIFIED: compose completed without an exact post ID/URL"
        )

    def delete(self, post_url: str) -> None:
        page = self._require_page()
        page.goto(post_url, wait_until="domcontentloaded", timeout=60_000)
        article = page.locator("article").first
        try:
            article.wait_for(state="visible", timeout=20_000)
            article.locator('[data-testid="caret"]').click()
            delete_item = page.get_by_text("Удалить", exact=True)
            if delete_item.count() == 0:
                delete_item = page.get_by_text("Delete", exact=True)
            delete_item.first.click()
            page.locator('[data-testid="confirmationSheetConfirm"]').click()
            page.wait_for_url(lambda url: "/status/" not in url, timeout=30_000)
        except XPostAlreadyDeleted:
            raise
        except Exception as exc:
            exact_path = re.sub(r"^https://x\.com", "", post_url).split("?", 1)[0]
            try:
                body_text = page.locator("body").inner_text(timeout=2_000).casefold()
            except Exception:
                body_text = ""
            if any(
                marker in body_text
                for marker in (
                    "this page doesn’t exist",
                    "this page doesn't exist",
                    "страница не существует",
                    "ничего нет",
                )
            ) or (
                page.locator("article").count() == 0
                and page.locator(f'a[href="{exact_path}"]').count() == 0
            ):
                raise XPostAlreadyDeleted("X_DELETE_ALREADY_APPLIED") from exc
            raise XBrowserError(f"X post could not be deleted: {exc}") from exc

    def edit(self, post_url: str, text: str) -> None:
        """Edit a post in place when X makes the edit option available."""
        page = self._require_page()
        page.goto(post_url, wait_until="domcontentloaded", timeout=60_000)
        article = page.locator("article").first
        try:
            article.wait_for(state="visible", timeout=20_000)
            article.locator('[data-testid="caret"]').click()
            edit_item = page.get_by_text("Редактировать пост", exact=True)
            if edit_item.count() == 0:
                edit_item = page.get_by_text("Edit post", exact=True)
            if edit_item.count() == 0:
                raise XEditUnavailable("X post editing is unavailable for this account")
            edit_item.first.click()
            editor = page.locator('[data-testid="tweetTextarea_0"]').first
            editor.wait_for(state="visible", timeout=20_000)
            editor.fill(text)
            self._dismiss_composer_suggestions()
            button = self._enabled_submit_button(timeout_ms=20_000)
            button.click(force=True, no_wait_after=True)
            page.wait_for_timeout(1_000)
            if editor.is_visible():
                page.keyboard.press("Control+Enter")
            editor.wait_for(state="hidden", timeout=15_000)
        except XEditUnavailable:
            raise
        except Exception as exc:
            raise XBrowserError(f"X post could not be edited: {exc}") from exc

def interactive_login(profile_directory: Path) -> None:
    with XBrowserSession(profile_directory, headless=False) as session:
        session.page.goto("https://x.com/home", wait_until="domcontentloaded")
        print("Войдите в аккаунт X в открывшемся окне.")
        input("После входа откройте главную ленту и нажмите Enter здесь: ")
        if not session.is_authenticated():
            raise XAuthenticationRequired("Вход в X не подтверждён")
        print("Авторизация X сохранена. Окно Chrome закрыто автоматически.")
