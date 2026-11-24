import hashlib

import pytest
from round_robin.research.fetcher import (
    FetchAllowance,
    FetchResponse,
    collect_source,
    exact_passages,
)


@pytest.mark.parametrize(
    "ignored",
    [
        "<noscript>NOT_FOR_THE_PACKET</noscript>",
        "<svg><text>NOT_FOR_THE_PACKET</text></svg>",
        "<svg><svg><text>NOT_FOR_THE_PACKET</text></svg>ALSO_NOT_FOR_THE_PACKET</svg>",
    ],
    ids=["noscript", "svg", "nested-svg"],
)
def test_ignored_section_closes_without_losing_or_admitting_source_text(ignored):
    body = f"<html><p>Before</p>{ignored}<p>After</p></html>".encode()

    class FakeTransport:
        def __init__(self):
            self.calls = []

        def get(self, target, *, timeout, byte_limit):
            self.calls.append((target.url, timeout, byte_limit))
            return FetchResponse(200, {"content-type": "text/html"}, body)

    transport = FakeTransport()
    allowance = FetchAllowance(
        registered_urls={"candidate": "https://public.example/source"}
    )
    source = collect_source(
        "candidate", allowance, transport=transport, resolver=lambda _: ["8.8.8.8"]
    )
    assert source.text == "Before\nAfter"
    assert "NOT_FOR_THE_PACKET" not in source.text
    assert source.status == "not_checked"
    assert source.content_hash == hashlib.sha256(source.text.encode()).hexdigest()
    passages = exact_passages(source, maximum=24, size=2000)
    assert "".join(item["text"] for item in passages) == source.text
    assert allowance.pages == len(transport.calls) == 1
