"""Credential-safe HTTP transport: no redirects, no non-HTTP schemes."""
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('REDIRECT_BLOCKED')


def urlopen(request, timeout=20, context=None):
    address = urlsplit(request.full_url if hasattr(request, 'full_url') else request)
    if (address.scheme not in {'http', 'https'} or not address.hostname
            or address.username or address.password):
        raise URLError('INVALID_HTTP_ENDPOINT')
    if address.scheme == 'http' and address.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise URLError('REMOTE_HTTP_REQUIRES_TLS')
    return build_opener(NoRedirect(), HTTPSHandler(context=context)).open(request, timeout=timeout)
