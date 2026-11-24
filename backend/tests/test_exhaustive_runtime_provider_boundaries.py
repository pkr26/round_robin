"""Fresh adversarial contract and transport probes; all network seams are synthetic."""
from dataclasses import replace
from decimal import Decimal
import json
import socket
import time
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from backend.tests.test_providers import packet, output, response_for, configuration
from round_robin.providers.base import ProviderFailure, ProviderOutput, validate_output
from round_robin.providers.http import HTTPProvider, _decimal, configured_provider
from round_robin.providers import transport
from round_robin.providers.simulated import SimulatedProvider


@pytest.mark.parametrize('value', ['NaN', '-1', '0', 'not-money', None, True])
def test_nonpositive_or_nonfinite_provider_price_fails_closed(value):
    with pytest.raises(ProviderFailure, match='pricing_unavailable') as error:
        _decimal(value)
    assert error.value.may_have_sent is False


@pytest.mark.parametrize('change,code', [
    ({'brief': {'unserializable': set()}}, 'invalid_packet'),
    ({'brief': {'private_number': float('nan')}}, 'invalid_packet'),
    ({'brief': {'text': 'x' * 128001}}, 'context_limit'),
    ({'evidence': [{'text': 'x' * 2001}]}, 'context_limit'),
    ({'evidence': [{}] * 25}, 'context_limit'),
    ({'max_output_tokens': 0}, 'invalid_request_limits'),
    ({'max_output_tokens': 9999}, 'invalid_request_limits'),
    ({'timeout_seconds': 0}, 'invalid_request_limits'),
    ({'timeout_seconds': float('nan')}, 'invalid_request_limits'),
])
def test_packet_limits_reject_whole_request_without_truncating_private_requirements(change, code):
    with pytest.raises(ProviderFailure, match=code) as error:
        replace(packet(), **change).packet()
    assert error.value.may_have_sent is False


@pytest.mark.parametrize('change,code', [
    ({'constraints': [{'option_id':'option_foreign','requirement_id':'requirement_budget','result':'pass','reason':'untrusted assertion','basis_refs':[]}]}, 'invalid_reference'),
    ({'constraints': [{'option_id':'option_a','requirement_id':'foreign_requirement','result':'pass','reason':'untrusted assertion','basis_refs':[]}]}, 'invalid_reference'),
    ({'questions':['secret question']}, 'unexpected_clarification'),
    ({'reference_ids':['foreign_ref']}, 'invalid_reference'),
    ({'recommendation_option_id':'foreign_option'}, 'invalid_reference'),
])
def test_provider_output_rejects_cross_scope_and_unexpected_clarification(change, code):
    with pytest.raises(ProviderFailure, match=code):
        validate_output(ProviderOutput.model_validate({**output(), **change}), packet())


def test_combined_constraint_refs_and_claim_aliases_are_bounded_and_scoped():
    request = packet()
    requirement = request.brief['hard_requirements'][0]['id']
    valid_constraint = {'option_id':'option_a','requirement_id':requirement,'result':'unknown','reason':'Needs evidence','basis_refs':['evidence_owned']}
    assert validate_output(ProviderOutput.model_validate({**output(), 'constraints':[valid_constraint]}), request).constraints
    with pytest.raises(ProviderFailure, match='schema_invalid'):
        validate_output(ProviderOutput.model_validate({**output(), 'constraints':[valid_constraint, valid_constraint]}), request)
    values = [f'evidence_{n}' for n in range(40)]
    too_many = ProviderOutput.model_validate({**output(), 'reference_ids':values, 'constraints':[valid_constraint]})
    with pytest.raises(ProviderFailure, match='schema_invalid'):
        validate_output(too_many, replace(request, allowed_reference_ids=frozenset([*values, 'evidence_owned'])))
    claim = {'alias':'new_claim_1','proposition':'A hypothetical supported claim','kind':'fact','evidence_status':'not_checked','reference_ids':['evidence_owned']}
    valid = ProviderOutput.model_validate({**output(), 'claims':[claim], 'reference_ids':['new_claim_1']})
    assert validate_output(valid, request) is valid
    with pytest.raises(ProviderFailure, match='schema_invalid'):
        validate_output(ProviderOutput.model_validate({**output(), 'claims':[claim,claim]}), request)
    with pytest.raises(ProviderFailure, match='invalid_reference'):
        validate_output(ProviderOutput.model_validate({**output(), 'claims':[{**claim,'reference_ids':['foreign'] }]}), request)
    with pytest.raises(ProviderFailure, match='missing_audit_result'):
        validate_output(ProviderOutput.model_validate(output()), packet('audit'))


@pytest.mark.parametrize('provider,mutation,code', [
    ('openai', lambda config: config['models'].update(openai='bad?model'), 'provider_configuration_invalid'),
    ('openai', lambda config: config['capabilities']['openai'].update(structured_text=False), 'capability_unverified'),
    ('openai', lambda config: config['pricing']['openai'].update(reasoning_exposure_bounded=False), 'pricing_unavailable'),
    ('openai', lambda config: config['capabilities']['openai'].update(context_window_tokens=1), 'context_limit'),
    ('anthropic', lambda config: config.update(anthropic_api_version='bad'), 'api_version_unconfigured'),
    ('google', lambda config: config['models'].update(google='model/foreign'), 'provider_configuration_invalid'),
])
def test_configuration_defects_never_call_transport(provider, mutation, code):
    config = configuration(provider)
    mutation(config)
    sent = []
    adapter = HTTPProvider(provider, config, api_key='synthetic', transport=httpx.MockTransport(lambda req: sent.append(req)))
    with pytest.raises(ProviderFailure, match=code) as error:
        adapter.generate(packet())
    assert not error.value.may_have_sent and sent == []


def test_provider_factory_and_unknown_provider_fail_closed():
    with pytest.raises(ProviderFailure, match='unsupported_provider'):
        HTTPProvider('foreign', {})
    assert isinstance(configured_provider('openai', {'mode':'live'}), HTTPProvider)
    assert isinstance(configured_provider('google', {'mode':'simulated'}), SimulatedProvider)


@pytest.mark.parametrize('kind', ['compressed','oversized','identity','connect','protocol','unexpected_status','invalid_retry','google_feedback'])
def test_http_failure_shapes_retain_uncertainty_and_safe_public_codes(kind):
    provider = 'google' if kind == 'google_feedback' else 'openai'
    raw = response_for(provider)
    headers = {}
    status = 200
    body = None
    expected = {'compressed':'unsupported_response_encoding','oversized':'response_limit','identity':None,'connect':'connection_failure','protocol':'transport_failure','unexpected_status':'provider_rejected_request','invalid_retry':'rate_limit','google_feedback':'refusal'}[kind]
    if kind == 'compressed': headers['content-encoding'] = 'br'
    if kind == 'oversized': body = b' ' * (2 * 1024 * 1024 + 1)
    if kind == 'identity': raw['id'] = 'response_owned_123'
    if kind == 'unexpected_status': status = 409
    if kind == 'invalid_retry': status = 429; headers['retry-after'] = 'not-a-duration'
    if kind == 'google_feedback': raw['promptFeedback'] = {'blockReason':'SAFETY'}
    seen = []
    def send(request):
        seen.append(request)
        if kind == 'connect': raise httpx.ConnectError('secret upstream')
        if kind == 'protocol': raise httpx.ProtocolError('secret protocol')
        return httpx.Response(status, headers=headers, content=body if body is not None else json.dumps(raw).encode())
    adapter = HTTPProvider(provider, configuration(provider), api_key='synthetic', transport=httpx.MockTransport(send))
    if expected is None:
        assert adapter.generate(packet()).provider_request_id == 'response_owned_123'
    else:
        with pytest.raises(ProviderFailure, match=expected) as error:
            adapter.generate(packet())
        assert 'secret' not in str(error.value) and error.value.may_have_sent
        if kind == 'invalid_retry': assert error.value.retry_after == 3
    assert len(seen) == 1


def test_cost_ambiguous_cached_counts_and_malformed_prices_remain_unknown():
    adapter = HTTPProvider('anthropic', configuration('anthropic'), api_key='synthetic')
    assert adapter._cost({'input_tokens':1,'output_tokens':2,'cache_read_input_tokens':True}) is None
    assert adapter._cost({'input_tokens':1,'output_tokens':2,'cache_creation_input_tokens':3,'cache_read_input_tokens':4}) is None
    adapter.pricing['input_usd_per_million'] = 'not-a-price'
    # Invalid operator pricing never becomes a guessed zero receipt.
    with pytest.raises(ProviderFailure, match='pricing_unavailable'):
        adapter._cost({'input_tokens':1,'output_tokens':2})
    assert adapter._usage({'usage':{'bad': float('nan')}}) == {}


def test_simulated_intake_cannot_invent_missing_options_requirements_or_horizon(monkeypatch):
    request = packet('intake')
    request.brief.update(options=[], horizon_months=None, hard_requirements=[])
    result = SimulatedProvider('openai').generate(request)
    assert len(result.output.questions) == 3 and result.simulated and result.cost_usd == 0
    slept=[]
    monkeypatch.setattr('round_robin.providers.simulated.time.sleep', slept.append)
    simulated = SimulatedProvider('openai','test_slow_success')
    simulated.generate(request)
    assert slept == [20] and simulated.scenario == 'success'


class Socket:
    def __init__(self, error=None): self.error=error; self.timeouts=[]; self.sent=[]
    def settimeout(self, value): self.timeouts.append(value)
    def sendall(self, value):
        if self.error: raise self.error
        self.sent.append(value)

class Stream:
    def __init__(self, sock=None): self.sock=sock or Socket(); self.closed=False; self.reads=[]; self.tls=[]
    def get_extra_info(self, info): return self.sock if info=='socket' else 'metadata'
    def read(self, size, timeout=None): self.reads.append((size,timeout)); return b'ok'
    def start_tls(self, context, hostname, timeout=None): self.tls.append((context,hostname,timeout)); return self
    def close(self): self.closed=True


def test_transport_deadline_is_shared_across_read_write_and_tls(monkeypatch):
    monkeypatch.setattr(transport.time, 'monotonic', lambda:10)
    raw=Stream(); bounded=transport._DeadlineStream(raw,15)
    assert bounded.read(4,timeout=20)==b'ok' and raw.reads==[(4,5)]
    bounded.write(b'private',timeout=2)
    assert raw.sock.sent==[b'private'] and raw.sock.timeouts==[2]
    secured=bounded.start_tls('context','provider.test',timeout=9)
    assert secured.deadline==15 and raw.tls==[('context','provider.test',5)]
    assert bounded.get_extra_info('metadata')=='metadata'
    bounded.close(); assert raw.closed
    with pytest.raises(httpcore.ReadTimeout): transport._remaining(10)


@pytest.mark.parametrize('error,expected', [(socket.timeout('secret'),httpcore.WriteTimeout),(OSError('secret'),httpcore.WriteError)])
def test_transport_partial_write_failures_are_typed_and_safe(error,expected):
    bounded=transport._DeadlineStream(Stream(Socket(error)),time.monotonic()+5)
    with pytest.raises(expected) as raised: bounded.write(b'private')
    assert 'secret' not in str(raised.value)


@pytest.mark.parametrize('error,expected', [(httpcore.ReadTimeout('secret'),httpx.TimeoutException),(httpcore.ConnectError('secret'),httpx.ConnectError),(httpcore.WriteError('secret'),httpx.NetworkError),(httpcore.RemoteProtocolError('secret'),httpx.ProtocolError)])
def test_core_transport_errors_cannot_leak_upstream_exception_text(error,expected):
    with pytest.raises(expected) as raised:
        with transport._httpx_errors(): raise error
    assert 'secret' not in str(raised.value)


def test_dns_capacity_timeout_empty_resolution_and_tcp_fallback_are_bounded(monkeypatch):
    class Capacity:
        def acquire(self, timeout): return False
    monkeypatch.setattr(transport, '_DNS_CAPACITY', Capacity())
    with pytest.raises(httpcore.ConnectTimeout, match='capacity'):
        transport._addresses('provider.test',443,time.monotonic()+1)
    class Capacity:
        def acquire(self, timeout): return True
        def release(self): pass
    monkeypatch.setattr(transport, '_DNS_CAPACITY', Capacity())
    monkeypatch.setattr(transport.socket, 'getaddrinfo', lambda *a,**kw: [])
    with pytest.raises(httpcore.ConnectError, match='resolution'):
        transport._addresses('provider.test',443,time.monotonic()+1)
    def broken(*a,**kw): raise OSError('secret DNS')
    monkeypatch.setattr(transport.socket, 'getaddrinfo', broken)
    with pytest.raises(httpcore.ConnectError, match='resolution'):
        transport._addresses('provider.test',443,time.monotonic()+1)
    backend=transport._DeadlineBackend(time.monotonic()+5)
    monkeypatch.setattr(transport, '_addresses', lambda *args:['192.0.2.1','192.0.2.2'])
    attempts=[]
    def tcp(address,*args,**kwargs):
        attempts.append(address)
        if address=='192.0.2.1': raise httpcore.ConnectError('first unavailable')
        return Stream()
    backend.backend=SimpleNamespace(connect_tcp=tcp)
    assert isinstance(backend.connect_tcp('provider.test',443),transport._DeadlineStream)
    assert attempts==['192.0.2.1','192.0.2.2']
    monkeypatch.setattr(transport, '_addresses', lambda *args:['192.0.2.1'])
    with pytest.raises(httpcore.ConnectError): backend.connect_tcp('provider.test',443)


def test_response_stream_and_transport_own_lifecycle(monkeypatch):
    closed=[]
    class CoreBody:
        def __iter__(self): yield b'bytes'
        def close(self): closed.append('body')
    body=CoreBody()
    stream=transport._ResponseStream(body)
    assert list(stream)==[b'bytes']
    stream.close()
    pool=SimpleNamespace(handle_request=lambda req: SimpleNamespace(status=200,headers=[(b'x-safe',b'yes')],stream=body,extensions={}),close=lambda:closed.append('pool'))
    adapter=transport.DeadlineHTTPTransport(time.monotonic()+1)
    adapter.pool.close()
    adapter.pool=pool
    response=adapter.handle_request(httpx.Request('POST','https://provider.test/path',content=b'payload'))
    assert response.read()==b'bytes' and response.headers['x-safe']=='yes'
    adapter.close()
    assert closed==['body','body','pool']


@pytest.mark.parametrize('cached', [1,True,False,'0',None])
def test_unconfigured_cached_input_cannot_be_reported_as_final_ordinary_price(cached):
    raw=response_for('openai')
    raw['usage']['input_tokens_details']={'cached_tokens':cached}
    adapter=HTTPProvider('openai',configuration('openai'),api_key='synthetic',transport=httpx.MockTransport(lambda _:httpx.Response(200,json=raw)))
    result=adapter.generate(packet())
    assert result.cost_usd is None and result.usage['input_tokens_details']['cached_tokens'] is cached


def test_strict_provider_schemas_require_nested_claim_arrays_even_for_storage_defaults():
    from round_robin.providers.http import _output_schema
    schema=_output_schema()
    assert 'claims' in schema['required']
    assert 'reference_ids' in schema['$defs']['Claim']['required']
    assert 'default' not in json.dumps(schema)
    assert schema['$defs']['Claim']['additionalProperties'] is False


@pytest.mark.parametrize('predecessor',['evidence_owned','new_claim_1','claim_foreign','step_peer'])
def test_claim_revisions_can_only_name_an_existing_scoped_claim(predecessor):
    claim={'alias':'new_claim_1','proposition':'Revised scoped assertion','kind':'fact','evidence_status':'not_checked','reference_ids':['evidence_owned'],'predecessor_id':predecessor}
    request=replace(packet(),allowed_reference_ids=frozenset({'evidence_owned','step_peer'}),claims=({'id':'claim_owned'},))
    with pytest.raises(ProviderFailure,match='invalid_reference'):
        validate_output(ProviderOutput.model_validate({**output(),'claims':[claim]}),request)
    claim['predecessor_id']='claim_owned'
    assert validate_output(ProviderOutput.model_validate({**output(),'claims':[claim]}),request).claims[0].predecessor_id=='claim_owned'
