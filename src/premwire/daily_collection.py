"""Bounded evening collection; never starts a polling loop or updates old queues."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode

from .collectors.base import CollectorError
from .collectors.http import get_json, get_text
from .collectors.telegram_public import parse_telegram_page
from .collectors.x_rss import XFreeRssCollector
from .models import IncomingItem, Source


def telegram_day(source: Source, start: datetime, end: datetime, deadline: float):
    items, warnings = {}, []
    before = None
    for _ in range(40):
        if time.monotonic() >= deadline:
            warnings.append(f"{source.id}: day coverage incomplete (time budget)")
            break
        url = f"https://t.me/s/{source.handle}"
        if before is not None:
            url += f"?before={before}"
        try:
            html = get_text(url, timeout=int(min(10, max(1, deadline - time.monotonic()))))
            parsed = parse_telegram_page(html, source)
        except Exception as exc:
            # A later failed page must not discard already fetched publications.
            warnings.append(f"{source.id}: day coverage incomplete ({type(exc).__name__})")
            break
        for item in parsed:
            if start <= item.published_at < end:
                items[item.external_id] = item
        # Media-only messages also advance pagination.
        ids = [int(v) for v in re.findall(
            rf'data-post="{re.escape(source.handle)}/(\d+)"', html, re.I)]
        # An unrelated/quoted <time> elsewhere in the page must not stop the
        # backward scan before morning posts. Media-only posts advance by ID;
        # only actual parsed source publications prove the midnight boundary.
        dates = [i.published_at for i in parsed]
        if dates and min(dates) < start:
            break
        if not ids or (before is not None and min(ids) >= before):
            warnings.append(f"{source.id}: day coverage unverified (pagination ended)")
            break
        before = min(ids)
    else:
        warnings.append(f"{source.id}: day coverage incomplete (page cap)")
    return list(items.values()), warnings


def x_day(sources: tuple[Source, ...], token: str, start: datetime,
          end: datetime, deadline: float):
    if not sources:
        return [], []
    if not token:
        batch = XFreeRssCollector(sources).collect()
        return [i for i in batch.items if start <= i.published_at < end], [
            *batch.warnings, "X RSS: best-effort latest feeds, full-day coverage not guaranteed"]
    params = {
        "query": "(" + " OR ".join(f"from:{s.handle}" for s in sources) + ") -is:retweet",
        "max_results": "100", "tweet.fields": "created_at,author_id",
        "expansions": "author_id", "user.fields": "username",
        "start_time": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "end_time": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    by_handle = {s.handle.casefold(): s for s in sources}
    items, warnings, seen_tokens = {}, [], set()
    for _ in range(20):
        if time.monotonic() >= deadline:
            warnings.append("X API: day coverage incomplete (time budget)")
            break
        try:
            payload = get_json("https://api.x.com/2/tweets/search/recent?" + urlencode(params),
                               {"Authorization": f"Bearer {token}"}, timeout=10)
            if payload.get("errors") and not payload.get("data"):
                raise CollectorError("X API refused daily search")
        except Exception as exc:
            warnings.append(f"X API: day coverage incomplete ({type(exc).__name__})")
            break
        if payload.get("errors"):
            warnings.append("X API: partial response; coverage unverified")
        users = {str(u["id"]): u["username"] for u in payload.get("includes", {}).get("users", [])}
        for post in payload.get("data", []):
            source = by_handle.get(users.get(str(post.get("author_id")), "").casefold())
            if not source:
                if "X API: author mapping missing; coverage unverified" not in warnings:
                    warnings.append("X API: author mapping missing; coverage unverified")
                continue
            item = IncomingItem(source.id, str(post["id"]), str(post["text"]),
                                f"https://x.com/{source.handle}/status/{post['id']}",
                                datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")))
            if start <= item.published_at < end:
                items[item.external_id] = item
        cursor = payload.get("meta", {}).get("next_token")
        if not cursor:
            break
        if cursor in seen_tokens:
            warnings.append("X API: repeated pagination token")
            break
        seen_tokens.add(cursor)
        params["next_token"] = cursor
    else:
        warnings.append("X API: day coverage incomplete (page cap)")
    return list(items.values()), warnings


def collect_day(sources: tuple[Source, ...], env: dict[str, str],
                start: datetime, end: datetime, budget_seconds: int = 110, coverage=None):
    deadline = time.monotonic() + budget_seconds
    jobs = [(s.id, telegram_day, (s, start, end, deadline))
            for s in sources if s.enabled and s.platform == "telegram"]
    x_sources = tuple(s for s in sources if s.enabled and s.platform == "x")
    if x_sources:
        jobs.append(("x", x_day, (x_sources, env.get("X_BEARER_TOKEN", ""), start, end, deadline)))
    items, warnings = [], []
    for source in sources:
        if source.enabled and source.platform not in {"telegram", "x"}:
            warnings.append(f"{source.id}: platform {source.platform} not polled by daily source collector")
            if coverage is not None:
                coverage.append({"source_id": source.id, "platform": source.platform,
                                 "status": "NOT_POLLED", "count": 0})
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [(name, pool.submit(fn, *args)) for name, fn, args in jobs]
        for name, future in futures:
            try:
                found, notes = future.result()
                items.extend(found)
                warnings.extend(notes)
                if coverage is not None:
                    members = x_sources if name == "x" else tuple(s for s in sources if s.id == name)
                    for source in members:
                        source_items = [i for i in found if i.source_id == source.id]
                        unavailable = any(f"@{source.handle}:" in note and "unavailable" in note for note in notes)
                        status = ("UNAVAILABLE" if unavailable else "BEST_EFFORT_RSS" if name == "x" and not env.get("X_BEARER_TOKEN")
                                  else "UNVERIFIED" if notes else "MIDNIGHT_BOUNDARY_VERIFIED" if name != "x" else "API_PAGINATION_COMPLETE")
                        coverage.append({"source_id": source.id, "platform": source.platform, "status": status,
                                         "count": len(source_items),
                                         "earliest_retained": min((i.published_at for i in source_items), default=None),
                                         "latest_retained": max((i.published_at for i in source_items), default=None)})
            except Exception as exc:
                # No request URL, headers or API response may leak a credential.
                warnings.append(f"{name}: unavailable ({type(exc).__name__})")
                if coverage is not None:
                    coverage.append({"source_id": name, "status": "UNAVAILABLE", "count": 0})
    return items, warnings
