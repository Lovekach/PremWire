from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .collectors.base import CollectorError
from .collectors.x_rss import XFreeRssCollector
from .content_filter import AdvertisementFilter
from .models import IncomingItem, IngestResult, Source
from .storage import Storage


_X_LINK_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/"
    r"([A-Za-z0-9_]{1,15})/status/(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ManualXResult:
    handled: bool
    queued: int = 0
    already_known: int = 0
    filtered: int = 0
    failed: int = 0

    def response_text(self) -> str:
        if self.queued:
            return (
                f"Принято: {self.queued}. Ссылка добавлена в общую очередь PremWire."
            )
        if self.already_known and not self.failed:
            return "Эта новость уже есть в системе, повторно добавлять её не буду."
        if self.filtered and not self.failed:
            return "Ссылка считана, но материал остановлен рекламным фильтром."
        return "Не удалось считать публикацию X. Попробуй прислать ссылку ещё раз позже."


class ManualXInbox:
    def __init__(
        self,
        storage: Storage,
        sources: tuple[Source, ...],
        owner_username: str,
        content_filter: AdvertisementFilter,
        fetch_status: Callable[[Source, str], IncomingItem] | None = None,
    ) -> None:
        self.storage = storage
        self.owner_username = owner_username.strip().lstrip("@").casefold()
        self.content_filter = content_filter
        self.source_by_handle = {
            source.handle.casefold(): source
            for source in sources
            if source.platform == "x"
        }
        self.manual_source = next(
            (source for source in sources if source.id == "x_manual_inbox"),
            Source(
                id="x_manual_inbox",
                platform="manual_x",
                name="X: ручная ссылка",
                handle="manual_x_inbox",
                url="https://x.com/home",
            ),
        )
        collector = XFreeRssCollector(tuple(self.source_by_handle.values()))
        self.fetch_status = fetch_status or collector.fetch_status

    def handle(self, message: dict) -> ManualXResult | None:
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return None
        if not isinstance(sender, dict):
            return None
        username = str(sender.get("username", "")).casefold()
        if not self.owner_username or username != self.owner_username:
            return None
        text = str(message.get("text") or message.get("caption") or "")
        links = list(dict.fromkeys(_X_LINK_RE.findall(text)))
        if not links:
            return None

        queued = already_known = filtered = failed = 0
        for handle, post_id in links:
            source = self.source_by_handle.get(handle.casefold())
            if source is None:
                source = Source(
                    id=self.manual_source.id,
                    platform=self.manual_source.platform,
                    name=self.manual_source.name,
                    handle=handle,
                    url=f"https://x.com/{handle}",
                )
            try:
                item = self.fetch_status(source, post_id)
                outcome = self.storage.ingest(item)
                if outcome.result in {IngestResult.NEW, IngestResult.UPDATED}:
                    decision = self.content_filter.check(item.text)
                    if decision.blocked:
                        self.storage.mark_filtered(
                            outcome.item_id,
                            decision.reason or "advertising",
                            decision.evidence,
                        )
                        filtered += 1
                    else:
                        queued += 1
                else:
                    already_known += 1
            except (CollectorError, ValueError):
                failed += 1
        return ManualXResult(True, queued, already_known, filtered, failed)
