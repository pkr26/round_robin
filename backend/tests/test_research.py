import hashlib

import pytest

from round_robin.research.claims import ClaimRejected, assign_claim_ids, validate_reuse
from round_robin.research.fetcher import DeadlineReader, FetchAllowance, FetchRejected, FetchResponse, FetchUnavailable, collect_source, exact_passages, validate_target
from round_robin.research.queries import PublicScope, QueryRejected, render_query


def public(host):
    return ["93.184.216.34"]


@pytest.mark.parametrize("url", ["http://example.com", "https://name:secret@example.com", "https://example.com:444/", "https://localhost/", "https://metadata.google.internal/", "https://example.com/?api_key=private", "https://example.com/?X-Amz-Signature=value", "https://example.com/?%2574oken=private", "https://example.com/%250d%250aHeader", "https://example.com\\@private/"])
def test_secret_and_unsupported_urls_never_reach_transport(url):
    with pytest.raises(FetchRejected):
        validate_target(url, public)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "::1", "::ffff:127.0.0.1", "224.0.0.1", "192.0.2.1"])
def test_private_or_reserved_resolution_is_rejected(address):
    with pytest.raises(FetchRejected):
        validate_target("https://example.com/", lambda _: ["93.184.216.34", address])


def test_url_is_dns_pinned_and_fragment_is_not_disclosed():
    target = validate_target("https://example.com/path?q=public#local", public)
    assert target.address == "93.184.216.34"
    assert target.hostname == "example.com"
    assert target.url == "https://example.com/path?q=public"
    assert target.request_target == "/path?q=public"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, target, **kwargs):
        self.calls.append(target)
        return self.responses.pop(0)


def test_redirect_cannot_open_private_host_and_failed_fetch_stays_counted():
    transport = FakeTransport([FetchResponse(302, {"location": "https://127.0.0.1/"}, b"")])
    allowance = FetchAllowance(registered_urls={"candidate": "https://example.com"})
    resolver = lambda host: ["127.0.0.1"] if host == "127.0.0.1" else public(host)
    with pytest.raises(FetchRejected):
        collect_source("candidate", allowance, transport=transport, resolver=resolver)
    assert len(transport.calls) == 1 and allowance.pages == 1


def test_registered_url_html_is_inert_and_stored_passages_are_exact():
    html = b'<html><script>private tool action</script><style>.x{display:none}</style><p>Offer: 1000 upfront.</p><p>Installation unknown.</p></html>'
    transport = FakeTransport([FetchResponse(200, {"content-type": "text/html"}, html)])
    allowance = FetchAllowance(registered_urls={"candidate": "https://example.com"})
    source = collect_source("candidate", allowance, transport=transport, resolver=public)
    assert "private tool" not in source.text and "Installation unknown." in source.text
    assert source.status == "not_checked"
    passages = exact_passages(source, size=15)
    assert all(passage["sha256"] == hashlib.sha256(passage["text"].encode()).hexdigest() for passage in passages)
    assert "".join(passage["text"] for passage in passages) == source.text


def test_unregistered_model_url_and_exhausted_allowance_do_not_dispatch():
    transport = FakeTransport([])
    allowance = FetchAllowance(pages=8, registered_urls={"known": "https://example.com"})
    for identity in ("https://example.com/arbitrary", "known"):
        with pytest.raises(FetchRejected):
            collect_source(identity, allowance, transport=transport, resolver=public)
    assert transport.calls == []


@pytest.mark.parametrize("response", [FetchResponse(200, {"content-type": "application/pdf"}, b"PDF"), FetchResponse(200, {"content-type": "text/plain", "content-encoding": "gzip"}, b"data"), FetchResponse(200, {"content-type": "text/plain"}, b"\xff"), FetchResponse(200, {"content-type": "text/plain"}, b"x" * 20)])
def test_mime_encoding_and_actual_byte_bounds(response):
    with pytest.raises(FetchRejected):
        collect_source("candidate", FetchAllowance(byte_limit=10, registered_urls={"candidate": "https://example.com"}), transport=FakeTransport([response]), resolver=public)


def test_private_sentinel_cannot_become_a_search_query():
    scope = PublicScope(terms=(("term_1", "label printer"),), region="Phoenix")
    assert "label printer" in render_query(scope, term_ids=["term_1"], template_id="costs_v1")
    for terms, template in [(["PRIVATE_SENTINEL"], "costs_v1"), (["term_1"], "arbitrary private prose"), (["term_1", "term_1"], "costs_v1")]:
        with pytest.raises(QueryRejected):
            render_query(scope, term_ids=terms, template_id=template)


def test_alias_mapping_rewrites_local_refs_and_preserves_original():
    original = {"claims": [{"alias": "new_claim_1", "reference_ids": ["passage_local"]}, {"alias": "new_claim_2", "reference_ids": ["new_claim_1"]}]}
    identities = iter(["claim_server_1", "claim_server_2"])
    committed, mapping = assign_claim_ids(original, allowed_reference_ids={"passage_local"}, id_factory=lambda: next(identities))
    assert committed["claims"][1]["reference_ids"] == ["claim_server_1"]
    assert original["claims"][1]["reference_ids"] == ["new_claim_1"]
    assert mapping["new_claim_2"] == "claim_server_2"


@pytest.mark.parametrize("alias,reference", [("new_claim_0", "passage_local"), ("authoritative_claim", "passage_local"), ("new_claim_1", "foreign_run_claim")])
def test_invented_cross_run_or_invalid_claim_identity_rejected(alias, reference):
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"claims": [{"alias": alias, "reference_ids": [reference]}]}, allowed_reference_ids={"passage_local"})


def test_reuse_creates_child_scope_and_does_not_claim_stale_verification():
    parents = {"parent": {"meeting_id": "meeting_local", "content_hash": "a" * 64, "version": 1, "time_sensitive": True, "verification_status": "supported"}}
    result = validate_reuse(selected_items=[{"id": "parent", "content_hash": "a" * 64, "version": 1}], parent_items=parents, meeting_id="meeting_local", child_run_id="run_child")
    assert result[0]["run_id"] == "run_child" and result[0]["verification_status"] == "not_checked"
    with pytest.raises(ClaimRejected):
        validate_reuse(selected_items=[{"id": "parent", "content_hash": "a" * 64, "version": 1}], parent_items=parents, meeting_id="foreign_meeting", child_run_id="run_child")


def test_slow_drip_bytes_cannot_renew_total_fetch_deadline(monkeypatch):
    from round_robin.research import fetcher
    ticks = iter([1.0, 2.0, 4.0])
    monkeypatch.setattr(fetcher.time, 'monotonic', lambda: next(ticks))
    class DripSocket:
        def __init__(self): self.reads = 0; self.timeouts = []
        def settimeout(self, value): self.timeouts.append(value)
        def recv_into(self, buffer): self.reads += 1; buffer[0] = 65; return 1
    sock = DripSocket()
    reader = DeadlineReader(sock, 3.0)
    assert reader.readinto(bytearray(8)) == 1
    assert reader.readinto(bytearray(8)) == 1
    with pytest.raises(TimeoutError): reader.readinto(bytearray(8))
    assert sock.reads == 2 and sock.timeouts == [2.0, 1.0]


@pytest.mark.parametrize('key', ['apiKey','apikey','accessToken','clientSecret','Authorization','x_auth_token'])
def test_camel_case_credential_query_keys_never_become_public_sources(key):
    with pytest.raises(FetchRejected): validate_target(f'https://example.com/?{key}=PRIVATE_SENTINEL', public)


def test_redirects_share_one_total_time_allowance(monkeypatch):
    from round_robin.research import fetcher
    clock = [0.0]
    monkeypatch.setattr(fetcher.time, 'monotonic', lambda: clock[0])
    class Redirecting:
        def __init__(self): self.timeouts = []
        def get(self, target, **kwargs):
            self.timeouts.append(kwargs['timeout']); clock[0] += .75
            return FetchResponse(302, {'location':'https://example.com/next'}, b'')
    transport = Redirecting()
    with pytest.raises(FetchUnavailable):
        collect_source('candidate', FetchAllowance(registered_urls={'candidate':'https://example.com'}), timeout=1, transport=transport, resolver=public)
    assert transport.timeouts == [1, .25]


def test_public_ipv6_literal_remains_a_well_formed_canonical_url():
    target = validate_target('https://[2606:4700:4700::1111]/page', lambda _: ['2606:4700:4700::1111'])
    assert target.url == 'https://[2606:4700:4700::1111]/page'
