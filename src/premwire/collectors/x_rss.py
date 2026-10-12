from __future__ import annotations

import re
import time
from collections import OrderedDict
from threading import Lock
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlencode
from defusedxml import ElementTree

from premwire.models import IncomingItem, Source

from .base import CollectionBatch, CollectorError
from .http import get_json, get_text


DEFAULT_X_RSS_MIRRORS = (
    "https://rss.xcancel.com/{handle}/rss",
    "https://nitter.net/{handle}/rss",
    "https://twiiit.com/{handle}/rss",
)

_STATUS_RE = re.compile(r"/status/(\d+)", re.IGNORECASE)
_PICTURE_LINK_RE = re.compile(r"(?:https?://)?pic\.twitter\.com/\S+", re.IGNORECASE)

# Collectors are recreated on every poll; keep bounded, process-local health
# per feed URL so a broken account never delays healthy accounts on that host.
_FEED_FAILURES: OrderedDict[str, tuple[int, float]] = OrderedDict()
_FEED_FAILURES_LOCK = Lock()


def _feed_retry_after(url: str) -> float:
    with _FEED_FAILURES_LOCK:
        state = _FEED_FAILURES.get(url)
        return max(0.0, state[1] - time.monotonic()) if state else 0.0


def _record_feed_result(url: str, *, success: bool) -> None:
    with _FEED_FAILURES_LOCK:
        if success:
            _FEED_FAILURES.pop(url, None)
            return
        failures = min(_FEED_FAILURES.get(url, (0, 0.0))[0] + 1, 5)
        delay = min(900, 60 * 2 ** (failures - 1))
        _FEED_FAILURES[url] = (failures, time.monotonic() + delay)
        _FEED_FAILURES.move_to_end(url)
        while len(_FEED_FAILURES) > 1024:
            _FEED_FAILURES.popitem(last=False)


class _PostHtmlParser(HTMLParser):
    def __init__(self, *, first_paragraph_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.first_paragraph_only = first_paragraph_only
        self.parts: list[str] = []
        self.image_urls: list[str] = []
        self._paragraph_depth = 0
        self._paragraph_seen = False
        self._paragraph_done = False

    def handle_starttag(
        self, tag: str, attrs_raw: list[tuple[str, str | None]]
    ) -> None:
        if tag == "p":
            if not self._paragraph_seen:
                self._paragraph_seen = True
                self._paragraph_depth = 1
            elif self._paragraph_depth:
                self._paragraph_depth += 1
        elif tag == "br" and self._accept_text():
            self.parts.append("\n")
        elif tag == "img":
            attrs = {key: value or "" for key, value in attrs_raw}
            src = attrs.get("src", "").strip()
            if src.startswith("https://"):
                self.image_urls.append(src)

    def handle_endtag(self, tag: str) -> None:
        if tag != "p" or not self._paragraph_depth:
            return
        self._paragraph_depth -= 1
        if self._paragraph_depth == 0:
            self.parts.append("\n")
            if self.first_paragraph_only:
                self._paragraph_done = True

    def handle_data(self, data: str) -> None:
        if self._accept_text():
            self.parts.append(data)

    def _accept_text(self) -> bool:
        if not self.first_paragraph_only:
            return True
        return self._paragraph_depth > 0 and not self._paragraph_done

    @property
    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _snowflake_datetime(post_id: str) -> datetime:
    try:
        milliseconds = (int(post_id) >> 22) + 1288834974657
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


def _published_at(value: str | None, post_id: str) -> datetime:
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return _snowflake_datetime(post_id)


def parse_x_rss(xml_text: str, source: Source) -> tuple[IncomingItem, ...]:
    stripped = xml_text.lstrip("\ufeff \t\r\n")
    if not stripped.startswith("<?xml") and not stripped.startswith("<rss"):
        raise CollectorError("X RSS mirror returned a non-RSS page")
    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError as exc:
        raise CollectorError("X RSS mirror returned invalid XML") from exc

    result: list[IncomingItem] = []
    for entry in root.findall("./channel/item"):
        guid = (entry.findtext("guid") or "").strip()
        link = (entry.findtext("link") or "").strip()
        match = _STATUS_RE.search(link) or _STATUS_RE.search(guid)
        post_id = match.group(1) if match else guid if guid.isdigit() else ""
        if not post_id:
            continue

        title = (entry.findtext("title") or "").strip()
        if title.casefold().startswith(("rt by @", "retweeted by @")):
            continue
        description = entry.findtext("description") or ""
        parser = _PostHtmlParser()
        parser.feed(description)
        text = parser.text or title
        if not text:
            continue
        result.append(
            IncomingItem(
                source_id=source.id,
                external_id=post_id,
                text=text,
                canonical_url=f"https://x.com/{source.handle}/status/{post_id}",
                published_at=_published_at(entry.findtext("pubDate"), post_id),
                media_url=parser.image_urls[0] if parser.image_urls else None,
            )
        )
    return tuple(result)


class XFreeRssCollector:
    """Best-effort, read-only X collector using public RSS frontends."""

    def __init__(
        self,
        sources: tuple[Source, ...],
        mirrors: tuple[str, ...] = DEFAULT_X_RSS_MIRRORS,
    ) -> None:
        self.sources = tuple(
            source for source in sources if source.platform == "x" and source.enabled
        )
        self.mirrors = tuple(mirrors)

    def _feed(self, source: Source) -> tuple[IncomingItem, ...]:
        errors: list[str] = []
        handle = quote(source.handle, safe="")
        for template in self.mirrors:
            url = template.format(handle=handle)
            retry_after = _feed_retry_after(url)
            if retry_after > 0:
                errors.append(f"{url}: retry in {int(retry_after) + 1}s (cooldown)")
                continue
            try:
                payload = get_text(
                    url,
                    headers={
                        "Accept": "application/rss+xml, application/xml, text/xml, */*"
                    },
                    timeout=8,
                )
                parsed = parse_x_rss(payload, source)
                if not parsed:
                    raise CollectorError("RSS feed contained no usable posts")
                _record_feed_result(url, success=True)
                return parsed
            except CollectorError as exc:
                _record_feed_result(url, success=False)
                errors.append(f"{url}: {exc}")
        raise CollectorError("; ".join(errors) or "No X RSS mirrors configured")

    def collect(self) -> CollectionBatch:
        items: list[IncomingItem] = []
        warnings: list[str] = []
        if not self.sources:
            return CollectionBatch("x", (), ())

        # Public RSS mirrors are best-effort and often slow or unavailable in
        # one region.  Polling every account sequentially allowed one bad
        # mirror to block Telegram collection and publishing for 10+ minutes.
        workers = min(6, len(self.sources))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="premwire-x") as pool:
            pending: list[tuple[Source, Future[tuple[IncomingItem, ...]]]] = [
                (source, pool.submit(self._feed, source)) for source in self.sources
            ]
            for source, future in pending:
                try:
                    source_items = future.result()
                    if source.include_keywords:
                        source_items = tuple(
                            item
                            for item in source_items
                            if any(
                                keyword.casefold() in item.text.casefold()
                                for keyword in source.include_keywords
                            )
                        )
                    items.extend(source_items)
                except CollectorError as exc:
                    warnings.append(f"X RSS unavailable for @{source.handle}: {exc}")
                except Exception as exc:
                    warnings.append(f"X RSS failed for @{source.handle}: {exc}")
        return CollectionBatch("x", tuple(items), tuple(warnings))

    def fetch_status(self, source: Source, post_id: str) -> IncomingItem:
        try:
            for item in self._feed(source):
                if item.external_id == post_id:
                    return item
        except CollectorError:
            pass

        canonical_url = f"https://x.com/{source.handle}/status/{post_id}"
        endpoint = "https://publish.twitter.com/oembed?" + urlencode(
            {"url": canonical_url, "omit_script": "true", "dnt": "true"}
        )
        payload = get_json(endpoint, {})
        parser = _PostHtmlParser(first_paragraph_only=True)
        parser.feed(str(payload.get("html", "")))
        text = _PICTURE_LINK_RE.sub("", parser.text).strip()
        if not text:
            raise CollectorError(f"X post {post_id} could not be read")
        return IncomingItem(
            source_id=source.id,
            external_id=post_id,
            text=text,
            canonical_url=canonical_url,
            published_at=_snowflake_datetime(post_id),
        )
