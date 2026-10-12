from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit

from .base import CollectorError


USER_AGENT = "PremWireBot/0.1 (+https://t.me/PremWire)"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A source must not forward credentials or downgrade HTTPS implicitly.
        raise CollectorError('SOURCE_REDIRECT_REQUIRES_REVIEW')


def urlopen(request, timeout=20):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def get_text(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise CollectorError('SOURCE_URL_REQUIRES_HTTPS_WITHOUT_CREDENTIALS')
    request_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise CollectorError('SOURCE_RESPONSE_TOO_LARGE')
            return body.decode(charset, errors="replace")
    except HTTPError as exc:
        raise CollectorError(f"SOURCE_HTTP_{exc.code}") from None
    except URLError as exc:
        raise CollectorError('SOURCE_NETWORK_ERROR') from None
    except (TimeoutError, ConnectionError, OSError) as exc:
        # A stalled RSS mirror must be treated as an unavailable source, not
        # as a fatal error for the whole monitoring process.  XFreeRssCollector
        # can then try its next mirror while Telegram and publishing continue.
        raise CollectorError('SOURCE_TRANSPORT_ERROR') from None


def get_json(url: str, headers: dict[str, str], timeout: int = 20) -> dict:
    payload = get_text(url, headers={"Accept": "application/json", **headers}, timeout=timeout)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CollectorError('SOURCE_INVALID_JSON') from None
