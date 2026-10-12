from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
import re

from premwire.models import IncomingItem, Source

from .base import CollectionBatch, CollectorError
from .http import get_text


_BACKGROUND_URL_RE = re.compile(
    r"background-image\s*:\s*url\(\s*(['\"]?)(https://[^)'\"]+)\1\s*\)",
    re.IGNORECASE,
)


class TelegramPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict[str, str | None]] = []
        self._post: dict[str, str | None] | None = None
        self._message_depth = 0
        self._text_depth: int | None = None
        self._text_parts: list[str] | None = None
        self._primary_text_parts: list[str] = []
        self._fallback_text_parts: list[str] = []

    @staticmethod
    def _classes(attrs: dict[str, str]) -> set[str]:
        return set(attrs.get("class", "").split())

    def handle_starttag(self, tag: str, attrs_raw: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_raw}
        classes = self._classes(attrs)

        if self._post is None and tag == "div" and "tgme_widget_message" in classes:
            data_post = attrs.get("data-post", "")
            if not data_post or "/" not in data_post:
                return
            self._post = {
                "data_post": data_post,
                "datetime": None,
                "media_url": None,
            }
            self._message_depth = 1
            self._text_depth = None
            self._text_parts = None
            self._primary_text_parts = []
            self._fallback_text_parts = []
            return

        if self._post is None:
            return

        if self._post.get("media_url") is None and (
            "tgme_widget_message_photo_wrap" in classes
            or "tgme_widget_message_video_thumb" in classes
        ):
            match = _BACKGROUND_URL_RE.search(attrs.get("style", ""))
            if match:
                self._post["media_url"] = match.group(2)

        if tag == "div":
            self._message_depth += 1
            if "tgme_widget_message_text" in classes:
                # Telegram renders the quoted message of a reply inside the
                # same outer post. Its js-message_reply_text must not become
                # part of this post's IncomingItem.
                if "js-message_reply_text" in classes:
                    return
                self._text_depth = self._message_depth
                if "js-message_text" in classes:
                    self._text_parts = self._primary_text_parts
                else:
                    # Compatibility with public/older markup which omits the
                    # js-message_text marker on the actual message.
                    self._text_parts = self._fallback_text_parts
        if (
            self._text_depth is not None
            and self._text_parts is not None
            and tag == "br"
        ):
            self._text_parts.append("\n")
        if tag == "time" and attrs.get("datetime"):
            self._post["datetime"] = attrs["datetime"]

    def handle_endtag(self, tag: str) -> None:
        if self._post is None or tag != "div":
            return
        if self._text_depth == self._message_depth:
            self._text_depth = None
            self._text_parts = None
        self._message_depth -= 1
        if self._message_depth == 0:
            parts = self._primary_text_parts or self._fallback_text_parts
            text = " ".join("".join(parts).split())
            if text:
                self._post["text"] = text
                self.posts.append(self._post)
            self._post = None
            self._text_parts = None
            self._primary_text_parts = []
            self._fallback_text_parts = []

    def handle_data(self, data: str) -> None:
        if (
            self._post is not None
            and self._text_depth is not None
            and self._text_parts is not None
        ):
            self._text_parts.append(data)


def parse_telegram_page(html: str, source: Source) -> tuple[IncomingItem, ...]:
    parser = TelegramPageParser()
    parser.feed(html)
    result: list[IncomingItem] = []
    for post in parser.posts:
        data_post = str(post["data_post"])
        _, external_id = data_post.rsplit("/", 1)
        published = post.get("datetime")
        if not published:
            continue
        result.append(
            IncomingItem(
                source_id=source.id,
                external_id=external_id,
                text=str(post["text"]),
                canonical_url=f"https://t.me/{data_post}",
                published_at=datetime.fromisoformat(str(published).replace("Z", "+00:00")),
                media_url=str(post.get("media_url") or "") or None,
            )
        )
    return tuple(result)


class TelegramPublicCollector:
    def __init__(self, sources: tuple[Source, ...]) -> None:
        self.sources = tuple(
            source
            for source in sources
            if source.platform == "telegram" and source.enabled
        )

    def collect(self) -> CollectionBatch:
        items: list[IncomingItem] = []
        warnings: list[str] = []
        if not self.sources:
            return CollectionBatch("telegram_public", (), ())

        # Public Telegram pages are independent network reads.  Fetching them
        # sequentially made one poll as slow as the sum of every source latency,
        # and a single unavailable channel discarded the healthy channels too.
        # Keep a small bounded pool and consume results in configured order so
        # ingestion remains deterministic while network waiting is concurrent.
        workers = min(8, len(self.sources))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="premwire-telegram",
        ) as pool:
            futures = {
                source.id: pool.submit(get_text, f"https://t.me/s/{source.handle}")
                for source in self.sources
            }
            for source in self.sources:
                try:
                    html = futures[source.id].result()
                    parsed = parse_telegram_page(html, source)
                except CollectorError as exc:
                    warnings.append(f"@{source.handle}: {exc}")
                    continue
                if not parsed:
                    warnings.append(f"No public posts parsed from @{source.handle}")
                items.extend(parsed)
        return CollectionBatch("telegram_public", tuple(items), tuple(warnings))
