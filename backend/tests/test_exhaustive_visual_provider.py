"""Independent invalid-capability and malicious HTTP image response matrix."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import base64
import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from backend.tests.test_image_provider import image_configuration, synthetic_image_configuration, generation_packet, authorization, pixels
from round_robin.database.models import utcnow
from round_robin.providers import images
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider, ImageFundingLocked, ImageProviderConfiguration
from round_robin.visual.reviews import PixelView, independent_review_packet, issue_review_receipt
from round_robin.visual.schemas import VisualBrief


@pytest.mark.parametrize('change', [
 lambda c:c.update(critic_providers=['anthropic','anthropic']),
 lambda c:c.update(critic_providers=['openai','google']),
 lambda c:c['models'].pop('google'),
 lambda c:c['capabilities'].pop('openai'),
 lambda c:c['capabilities']['openai'].update(generates_images=False),
 lambda c:c['capabilities']['anthropic'].update(provider='google'),
 lambda c:c['models'].update(anthropic='model/?secret'),
 lambda c:c['capabilities']['anthropic'].update(understands_images=False),
 lambda c:c['vision_prices'].update(anthropic='0'),
 lambda c:c.update(generation_price_usd='0'),
 lambda c:c.update(supported_sizes=['0x20']),
 lambda c:c.update(supported_sizes=['9999x9999']),
])
def test_operator_configuration_rejects_unfunded_or_inconsistent_capabilities(change):
    config=synthetic_image_configuration();change(config)
    with pytest.raises(ValidationError):ImageProviderConfiguration.model_validate(config)


def test_missing_expired_and_oversized_operator_configuration_is_closed(image_configuration):
    path=Path(os.environ['ROUND_ROBIN_IMAGE_CONFIG_FILE'])
    config=synthetic_image_configuration();config['expires_at']=(utcnow()-timedelta(seconds=1)).isoformat();path.write_text(json.dumps(config))
    with pytest.raises(ImageFundingLocked):images.load_image_configuration()
    path.write_bytes(b'x'*(128*1024+1))
    with pytest.raises(ImageFundingLocked):images.load_image_configuration()
    path.unlink()
    with pytest.raises(ImageFundingLocked):images.load_image_configuration()


@pytest.mark.parametrize('bound', ['NaN','Infinity','-1','0','bad',None,True])
def test_invalid_or_insufficient_authorization_rejected_before_remote_io(image_configuration,bound):
    sent=[];adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic',transport=httpx.MockTransport(sent.append))
    with pytest.raises(ImageFundingLocked):adapter.preflight(generation_packet(),replace(authorization(),bound_usd=bound))
    assert not sent


def test_missing_credentials_unsupported_dimensions_and_bad_kind_are_closed(image_configuration):
    adapter=HTTPImageProvider('openai',image_configuration,api_key='')
    with pytest.raises(ImageFundingLocked):adapter.preflight(generation_packet(),authorization())
    adapter.api_key='synthetic'
    packet=generation_packet();packet['brief']['width']=63
    with pytest.raises(ImageFundingLocked):adapter.preflight(packet,authorization())
    packet=generation_packet();packet['kind']='unknown'
    with pytest.raises(ImageFundingLocked):adapter.preflight(packet,authorization())
    with pytest.raises(ImageFundingLocked):adapter.review(generation_packet(),authorization())
    with pytest.raises(ImageFundingLocked):adapter.cancel('job',authorization())


def vision_packet(adapter):
    raw=pixels();view=PixelView(raw,__import__('hashlib').sha256(raw).hexdigest())
    receipt=issue_review_receipt(adapter.capabilities,'version_one',view.sha256,[view])
    return {**independent_review_packet(VisualBrief(purpose='synthetic',audience='test'),receipt,[view]),'kind':'vision','owner_id':'owner_one','session_id':'session_one','step_id':'step_one'}


@pytest.mark.parametrize('images_value', [[],None,{},['not bytes'],[b'not image'],[b'x']*5])
def test_invalid_edit_pixels_never_reach_transport(image_configuration,images_value):
    adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic')
    packet=generation_packet('edit');packet['images']=images_value
    with pytest.raises((ImageFundingLocked,ValueError)):adapter.preflight(packet,authorization('edit'))


def test_capability_size_and_receipt_mismatch_prevent_review_and_edit(image_configuration):
    adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic')
    packet=vision_packet(adapter);packet['images']*=2
    with pytest.raises(ImageFundingLocked):adapter.preflight(packet,authorization('vision'))
    packet=vision_packet(adapter);packet['receipt']['provider']='google'
    with pytest.raises(ImageFundingLocked):adapter.preflight(packet,authorization('vision'))
    adapter.capabilities=adapter.capabilities.model_copy(update={'maximum_input_bytes':1})
    with pytest.raises(ImageFundingLocked):adapter.preflight(generation_packet('edit'),authorization('edit'))
    adapter.capabilities=adapter.capabilities.model_copy(update={'generates_images':False})
    with pytest.raises(ImageFundingLocked):adapter.preflight(generation_packet(),authorization())


@pytest.mark.parametrize('status,body,expected',[(401,{},'authentication'),(429,{},'rate_limit'),(503,{},'transient'),(302,{},'provider_rejected_request'),(422,{'error':{'code':'other'}},'provider_rejected_request'),(400,[], 'provider_rejected_request'),(400,'not json','provider_rejected_request'),(400,'x'*17000,'provider_rejected_request')])
def test_all_http_error_classes_are_bounded_and_do_not_echo_private_body(image_configuration,status,body,expected):
    response=httpx.Response(status,content=body.encode() if isinstance(body,str) else json.dumps(body).encode(),headers={'x-request-id':'safe_request'})
    adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic',transport=httpx.MockTransport(lambda request:response))
    with pytest.raises(ProviderFailure) as failure:adapter.submit(generation_packet(),authorization())
    assert failure.value.code==expected and failure.value.provider_request_id=='safe_request'
    assert 'not json' not in str(failure.value)


@pytest.mark.parametrize('status',[200,400,422])
def test_encoded_responses_are_refused_before_reading_or_decompression(image_configuration,status):
    class HostileStream(httpx.SyncByteStream):
        def __iter__(self):raise AssertionError('Compressed private payload must never be read')
    response=httpx.Response(status,headers={'Content-Encoding':'gzip'},stream=HostileStream())
    adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic',transport=httpx.MockTransport(lambda request:response))
    with pytest.raises(ProviderFailure,match='unsupported_response_encoding'):adapter.submit(generation_packet(),authorization())


def test_response_byte_timeout_json_and_transport_boundaries(image_configuration,monkeypatch):
    adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic',transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'12345')))
    with pytest.raises(ProviderFailure,match='response_limit'):adapter._http('https://example.invalid',{}, {},timeout=1,maximum_bytes=4)
    clock=iter([0,2]);monkeypatch.setattr(images.time,'monotonic',lambda:next(clock))
    with pytest.raises(ProviderFailure,match='response_limit'):adapter._http('https://example.invalid',{}, {},timeout=1,maximum_bytes=100)
    monkeypatch.undo()
    adapter.transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'not json'))
    with pytest.raises(ProviderFailure,match='unknown'):adapter._http('https://example.invalid',{}, {},timeout=1,maximum_bytes=100)
    adapter.transport=httpx.MockTransport(lambda request:(_ for _ in ()).throw(httpx.ReadTimeout('private diagnostic')))
    with pytest.raises(ProviderFailure,match='unknown'):adapter._http('https://example.invalid',{}, {},timeout=1,maximum_bytes=100)


def test_bad_pixel_dimensions_and_prompt_size_do_not_publish(image_configuration):
    adapter=HTTPImageProvider('openai',image_configuration,api_key='synthetic',transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(pixels()).decode()}]})))
    packet=generation_packet();packet['brief']['purpose']='x'*16001
    with pytest.raises(ImageFundingLocked):adapter.submit(packet,authorization())
    # Change only the returned pixel dimensions; the approved request stays64x64.
    from backend.tests.test_visual import png
    adapter.transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(png()).decode()}]}))
    with pytest.raises(ProviderFailure,match='invalid_image_result'):adapter.submit(generation_packet(),authorization())


@pytest.mark.parametrize('text,expected',[('not json','schema_invalid'),('{"findings": [], "limitations": []}',None)])
def test_critic_schema_validation_preserves_one_remote_request(image_configuration,text,expected):
    sent=[]
    def respond(request):
        sent.append(request)
        return httpx.Response(200,headers={'x-request-id':'critic_request'},json={'stop_reason':'end_turn','content':[{'type':'text','text':text}]})
    adapter=HTTPImageProvider('anthropic',image_configuration,api_key='synthetic',transport=httpx.MockTransport(respond))
    packet=vision_packet(adapter)
    if expected:
        with pytest.raises(ProviderFailure,match=expected):adapter.review(packet,authorization('vision'))
    else:assert adapter.review(packet,authorization('vision'))['critique']['findings']==[]
    assert len(sent)==1


def test_critic_refusal_preserves_request_identity_without_retry(image_configuration):
    adapter=HTTPImageProvider('anthropic',image_configuration,api_key='synthetic',transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'x-request-id':'refused_request'},json={'stop_reason':'refusal','content':[]})))
    with pytest.raises(ProviderFailure) as failure:adapter.review(vision_packet(adapter),authorization('vision'))
    assert failure.value.provider_request_id=='refused_request'
