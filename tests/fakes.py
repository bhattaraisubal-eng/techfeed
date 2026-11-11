"""Test doubles shared by the worker tests."""

from typing import NamedTuple

from ingest.http import ConditionalResponse, Validators


class Call(NamedTuple):
    method: str
    url: str
    params: dict
    headers: dict


class FakeHttp:
    """Stands in for HttpClient: returns canned responses and records every call.

    `routes` is a list of (url_substring, response). The first substring found
    in the requested URL wins. A response can be:
      * a value          -> returned as-is
      * an Exception     -> raised (to simulate failures)
      * a function       -> called with (url, params); its result is used
    An unmatched URL fails the test, so workers can't make surprise requests.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[Call] = []

    def get_json(self, url, params=None, headers=None):
        return self._respond("GET", url, params, headers)

    def get_bytes(self, url, params=None, headers=None):
        return self._respond("GET", url, params, headers)

    def get_conditional(self, url, validators=None):
        """Like HttpClient.get_conditional. A route can return a ConditionalResponse
        (e.g. body=None to simulate 304); plain bytes become a 200 without validators.
        The conditional headers are recorded, so tests can check what was sent."""
        headers = {}
        if validators and validators.etag:
            headers["If-None-Match"] = validators.etag
        if validators and validators.last_modified:
            headers["If-Modified-Since"] = validators.last_modified
        response = self._respond("GET", url, None, headers)
        if isinstance(response, ConditionalResponse):
            return response
        return ConditionalResponse(response, Validators())

    def post_form(self, url, form, headers=None):
        return self._respond("POST", url, form, headers)

    def _respond(self, method, url, params, headers):
        self.calls.append(Call(method, url, params or {}, headers or {}))
        for pattern, response in self.routes:
            if pattern in url:
                if callable(response):
                    response = response(url, params or {})
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected request: {method} {url}")
