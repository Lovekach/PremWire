from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from premwire.models import IncomingItem, Source

from .base import CollectionBatch, CollectorError
from .http import get_json


class XApiCollector:
    endpoint = "https://api.x.com/2/tweets/search/recent"

    def __init__(
        self,
        sources: tuple[Source, ...],
        bearer_token: str,
        since_id: str | None = None,
    ) -> None:
        self.sources = tuple(
            source for source in sources if source.platform == "x" and source.enabled
        )
        self.bearer_token = bearer_token.strip()
        self.since_id = (since_id or "").strip()

    def collect(self) -> CollectionBatch:
        if not self.bearer_token:
            return CollectionBatch(
                collector="x",
                items=(),
                warnings=("X_BEARER_TOKEN is not configured; X collection skipped",),
            )
        if not self.sources:
            return CollectionBatch("x", ())

        query = "(" + " OR ".join(
            f"from:{source.handle}" for source in self.sources
        ) + ") -is:retweet"
        parameters = {
            "query": query,
            "max_results": "100",
            "tweet.fields": "created_at,author_id,attachments",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "username",
            "media.fields": "media_key,type,url,preview_image_url,width,height",
        }
        if self.since_id.isdigit():
            parameters["since_id"] = self.since_id
        payload = get_json(
            f"{self.endpoint}?{urlencode(parameters)}",
            {"Authorization": f"Bearer {self.bearer_token}"},
        )
        if payload.get("errors") and not payload.get("data"):
            raise CollectorError("X API returned errors; response details withheld")

        users = {
            str(user["id"]): str(user["username"])
            for user in payload.get("includes", {}).get("users", [])
        }
        media = {
            str(item.get("media_key", "")): item
            for item in payload.get("includes", {}).get("media", [])
        }
        source_by_handle = {source.handle.casefold(): source for source in self.sources}
        items: list[IncomingItem] = []
        warnings: list[str] = []
        for post in payload.get("data", []):
            username = users.get(str(post.get("author_id", "")), "")
            source = source_by_handle.get(username.casefold())
            if not source:
                warnings.append(f"Unknown X author skipped: {username or post.get('author_id')}")
                continue
            post_text = str(post["text"])
            if source.include_keywords and not any(
                keyword.casefold() in post_text.casefold()
                for keyword in source.include_keywords
            ):
                continue
            post_id = str(post["id"])
            media_url = None
            for media_key in post.get("attachments", {}).get("media_keys", []):
                candidate = media.get(str(media_key), {})
                if str(candidate.get("type", "")).casefold() == "photo":
                    media_url = str(candidate.get("url", "")).strip() or None
                    if media_url:
                        break
            items.append(
                IncomingItem(
                    source_id=source.id,
                    external_id=post_id,
                    text=post_text,
                    canonical_url=f"https://x.com/{source.handle}/status/{post_id}",
                    published_at=datetime.fromisoformat(
                        str(post["created_at"]).replace("Z", "+00:00")
                    ),
                    media_url=media_url,
                )
            )
        return CollectionBatch("x", tuple(items), tuple(warnings))
