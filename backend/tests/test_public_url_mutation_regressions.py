"""Public evidence URLs cannot bypass destination and credential checks."""

import pytest
from round_robin.research import fetcher


@pytest.mark.parametrize("url", [
    "https://user@example.com/",
    "https://?query=public",
    "https://example.com/path with-space",
    "https://example.com/path\x7f",
    "https://example.com/path\\suffix",
    "https://example.com/path%7f",
    "https://printer.localhost./path",
    "https://printer.local./path",
    "https://service.internal./path",
    "https://example.com/?access_token=",
    "https://example.com/?api.key=synthetic",
    "https://example.com/?access.token=synthetic",
    "https://example.com/?myApiToken=",
    "https://example.com/?X-Amz-Expires=60",
    "https://example.com/?X-Goog-Algorithm=synthetic",
    "https://example.com/SK-ABCDEFGHIJKL",
])
def test_unsafe_urls_are_rejected_before_dns(url):
    lookups = []
    with pytest.raises(fetcher.FetchRejected):
        fetcher.validate_target(url, resolver=lambda host: lookups.append(host) or ["8.8.8.8"])
    assert lookups == []


def test_non_string_and_url_length_limits_fail_closed():
    with pytest.raises(fetcher.FetchRejected):
        fetcher.validate_target(None, resolver=lambda _: pytest.fail("DNS"))
    prefix = "https://example.com/"
    at_limit = prefix + "x" * (2048 - len(prefix))
    assert len(at_limit) == 2048
    target = fetcher.validate_target(at_limit, resolver=lambda _: ["8.8.8.8"])
    assert target.url == at_limit
    with pytest.raises(fetcher.FetchRejected):
        fetcher.validate_target(at_limit + "x", resolver=lambda _: pytest.fail("DNS"))


def test_public_url_canonicalization_preserves_encoded_query_and_discards_fragment():
    lookups = []
    target = fetcher.validate_target(
        "https://EXAMPLE.COM./?topic=public%20information#local-fragment",
        resolver=lambda host: lookups.append(host) or ["8.8.8.8", "1.1.1.1"],
    )
    assert lookups == ["example.com"]
    assert target.hostname == "example.com"
    assert target.address == "1.1.1.1"
    assert target.url == "https://example.com/?topic=public%20information"
    assert target.request_target == "/?topic=public%20information"


@pytest.mark.parametrize("address", ["::ffff:8.8.8.8", "ff02::1", "64:ff9b::808:808"])
def test_public_dns_answer_cannot_include_mapped_ipv4_or_multicast(address):
    with pytest.raises(fetcher.FetchRejected):
        fetcher.validate_target("https://example.com/", resolver=lambda _: ["8.8.8.8", address])


def test_default_dns_path_receives_the_supplied_resolution_timeout(monkeypatch):
    calls = []

    def resolver(hostname, *, timeout=5):
        calls.append((hostname, timeout))
        return ["8.8.8.8"]

    monkeypatch.setattr(fetcher, "public_addresses", resolver)
    target = fetcher.validate_target("https://example.com/", resolver=resolver, resolution_timeout=0.25)
    assert target.address == "8.8.8.8"
    assert calls == [("example.com", 0.25)]


def test_valid_punctuation_and_empty_path_are_canonicalized():
    resolver = lambda _: ["8.8.8.8"]
    assert fetcher.validate_target("https://example.com/!", resolver=resolver).request_target == "/!"
    root = fetcher.validate_target("https://example.com", resolver=resolver)
    assert root.url == "https://example.com/"
    assert root.request_target == "/"
