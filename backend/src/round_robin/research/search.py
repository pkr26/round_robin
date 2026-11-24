"""One documented search transport; only approved public query strings enter it."""

from dataclasses import dataclass
import json
import os
import time
from urllib.parse import urlencode

from .fetcher import FetchRejected, FetchUnavailable, SocketPublicTransport, public_addresses, validate_target


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str


class BraveSearch:
    def __init__(self, *, transport=None, resolver=public_addresses, api_key: str | None = None):
        self.transport = transport or SocketPublicTransport()
        self.resolver = resolver
        self._key = api_key if api_key is not None else os.environ.get("BRAVE_SEARCH_API_KEY", "")

    def search(self, query: str, *, timeout: float = 5) -> list[SearchHit]:
        if not self._key or not query or len(query) > 600 or len(query.split()) > 75:
            raise FetchRejected("Search requires configured credentials and a bounded approved query")
        deadline = time.monotonic() + timeout
        target = validate_target("https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 8, "text_decorations": "false"}), self.resolver, resolution_timeout=timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchUnavailable('Search DNS exhausted its deadline')
        response = self.transport.get(target, timeout=remaining, byte_limit=262144, private_headers={"Accept": "application/json", "X-Subscription-Token": self._key})
        if response.status != 200 or len(response.body) > 262144:
            raise FetchUnavailable("Search unavailable; no source verification is established")
        try:
            payload = json.loads(response.body)
            results = payload.get("web", {}).get("results", [])
            if not isinstance(results, list):
                raise ValueError
            hits = []
            for result in results[:8]:
                if not isinstance(result, dict) or not isinstance(result.get("url"), str) or not isinstance(result.get("title", ""), str):
                    raise ValueError
                hits.append(SearchHit(result.get("title", "")[:500], result["url"][:2048]))
            return hits
        except (ValueError, AttributeError, TypeError) as exc:
            raise FetchUnavailable("Search response did not satisfy its bounded contract") from exc
