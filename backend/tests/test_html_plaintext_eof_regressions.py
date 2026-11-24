"""Safe EOF flush for text/html bodies containing no literal markup opener."""
import hashlib

import pytest
from round_robin.research.fetcher import FetchAllowance, FetchResponse, collect_source


class FakeTransport:
    def __init__(self, body):
        self.body = body
        self.calls = 0
    def get(self, target, **kwargs):
        self.calls += 1
        return FetchResponse(200, {"content-type": "text/html; charset=utf-8"}, self.body)


def collect(body):
    transport = FakeTransport(body.encode())
    allowance = FetchAllowance(registered_urls={"candidate": "https://public.example/path"})
    result = collect_source("candidate", allowance, transport=transport, resolver=lambda _: ["8.8.8.8"])
    assert allowance.pages == transport.calls == 1
    assert result.content_hash == hashlib.sha256(result.text.encode()).hexdigest()
    return result.text


@pytest.mark.parametrize("body,expected", [
    ("Offer &amp", "Offer &"),
    ("Offer &amp; tax", "Offer & tax"),
    ("plain content", "plain content"),
    ("literal &#60;script&#62;VISIBLE&#60;/script&#62; &amp", "literal <script>VISIBLE</script> &"),
    ("standalone &", "standalone &"),
    ("Price &#60", "Price <"),
    ("Price &#x3c", "Price <"),
    ("Price &lt", "Price <"),
    ("Price &unknown", "Price &unknown"),
    ("Price &quot", 'Price "'),
    ("Price &#x", "Price &#x"),
    ("Price &#", "Price &#"),
])
def test_literal_markup_free_html_flushes_final_text(body, expected):
    assert collect(body) == expected


def test_known_malformed_noscript_eof_does_not_gain_hidden_text():
    source = "<p>BEFORE</p><noscript></script><iframe>LEAK"
    text = collect(source)
    assert text == "BEFORE" and "LEAK" not in text
