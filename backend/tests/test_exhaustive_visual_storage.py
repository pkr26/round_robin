"""Fresh adversarial visual-storage/money audit, independent of historical reports."""
from dataclasses import asdict, replace
from decimal import Decimal, Inexact, localcontext
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import image_configuration, pixels
from backend.tests.test_image_worker import image_factory, seed_image, FakeImageAdapter
from backend.tests.test_visual import visual_app
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.visual import assets, service
from round_robin.visual.assets import UnsafeRaster, store_raster, validate_raster, verified_bytes
from round_robin.visual.iterations import VisualLimit, VisualState, decimal
from round_robin.worker import image_tasks


def test_restrict_decoders_before_parsing_and_strip_all_metadata(monkeypatch):
    data = pixels()
    observed = []
    real = Image.open
    def record(*args, **kwargs):
        observed.append(kwargs.get('formats'))
        return real(*args, **kwargs)
    monkeypatch.setattr(Image, 'open', record)
    raster = validate_raster(data, 'image/png')
    assert observed == [('PNG', 'JPEG'), ('PNG', 'JPEG')]
    assert raster.width == raster.height == 64
    gif = BytesIO(); Image.new('RGB', (10, 10)).save(gif, 'GIF')
    with pytest.raises(UnsafeRaster): validate_raster(gif.getvalue())


@pytest.mark.parametrize('target', ['file', 'directory', 'ancestor'])
def test_canonical_storage_never_follows_symlinks(tmp_path, target):
    raster = validate_raster(pixels())
    private = tmp_path / 'private'; private.mkdir()
    other = tmp_path / 'other'; other.mkdir()
    outside = other / f'{raster.canonical_hash}.png'; outside.write_bytes(raster.canonical_bytes)
    if target == 'file': (private / outside.name).symlink_to(outside)
    elif target == 'directory': private.rmdir(); private.symlink_to(other, target_is_directory=True)
    else:
        (private / 'linked').symlink_to(other, target_is_directory=True)
        private = private / 'linked' / 'child'
    with pytest.raises(UnsafeRaster): store_raster(private, raster)
    assert outside.read_bytes() == raster.canonical_bytes
    assert not (other / 'child').exists()


def test_canonical_storage_rejects_forged_hash_and_corrupt_existing_file(tmp_path):
    raster = validate_raster(pixels())
    with pytest.raises(UnsafeRaster): store_raster(tmp_path, replace(raster, canonical_hash='0'*64))
    path = store_raster(tmp_path, raster)
    assert store_raster(tmp_path, raster) == path
    path.write_bytes(b'corrupt')
    with pytest.raises(UnsafeRaster): store_raster(tmp_path, raster)


def test_failed_durable_write_leaves_no_partial_artifact(tmp_path, monkeypatch):
    raster = validate_raster(pixels())
    monkeypatch.setattr(assets.os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    with pytest.raises(OSError): store_raster(tmp_path, raster)
    assert list(tmp_path.iterdir()) == []


def test_verified_bytes_are_bounded_regular_files(tmp_path, monkeypatch):
    path = tmp_path/'private.png'; path.write_bytes(b'1234')
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', 3)
    with pytest.raises(UnsafeRaster): verified_bytes(path, hashlib.sha256(b'1234').hexdigest())
    fifo = tmp_path/'fifo'; assets.os.mkfifo(fifo)
    with pytest.raises(UnsafeRaster): verified_bytes(fifo, '0'*64)


def test_decoded_canonical_size_limit_is_checked(monkeypatch):
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', 2)
    with pytest.raises(UnsafeRaster, match='Canonical'): validate_raster(pixels())


def test_owned_reads_require_matching_database_scope_and_safe_namespace(tmp_path):
    raster = validate_raster(pixels())
    directory = tmp_path/'visual'/'owner_a'/'design_a'
    path = store_raster(directory, raster)
    design = SimpleNamespace(id='design_a', owner_id='owner_a')
    version = SimpleNamespace(session_id='design_other', asset_path=str(path), canonical_hash=raster.canonical_hash)
    with pytest.raises(VisualLimit, match='different session'): service.owned_asset_bytes(version, design, tmp_path)
    version.session_id = design.id
    assert service.owned_asset_bytes(version, design, tmp_path) == raster.canonical_bytes
    version.asset_path = str(tmp_path/'foreign.png')
    with pytest.raises(VisualLimit): service.owned_asset_bytes(version, design, tmp_path)


def test_purge_cannot_delete_foreign_owner_artifact(image_factory, image_configuration, tmp_path):
    design_id, _ = seed_image(image_factory, image_configuration)
    raster = validate_raster(pixels())
    foreign = store_raster(tmp_path/'visual'/'owner_other'/'design_other', raster)
    with image_factory.begin() as session:
        session.add(DesignVersion(id='version_forged', session_id=design_id, version=1,
            canonical_hash=raster.canonical_hash, original_hash=raster.original_hash,
            asset_path=str(foreign), width=64, height=64, source_kind='upload', provenance={}))
    with image_factory.begin() as session:
        with pytest.raises(VisualLimit): service.purge_designs(session, 'meeting_image', tmp_path)
    assert foreign.read_bytes() == raster.canonical_bytes


def test_private_backup_copy_rejects_linked_parent_and_verifies_reused_content(tmp_path):
    content=b'private pixels'; digest=hashlib.sha256(content).hexdigest()
    source=tmp_path/'source.png'; source.write_bytes(content)
    target=tmp_path/'backup'/'out.png'
    service._private_copy(source,target,digest,100)
    service._private_copy(source,target,digest,100)
    assert target.read_bytes()==content
    target.write_bytes(b'corrupt')
    with pytest.raises(VisualLimit): service._private_copy(source,target,digest,100)
    with pytest.raises(VisualLimit): service._private_copy(source,tmp_path/'size.png',digest,1)
    with pytest.raises(VisualLimit): service._private_copy(source,tmp_path/'hash.png','0'*64,100)
    linked=tmp_path/'linked';linked.symlink_to(tmp_path/'backup',target_is_directory=True)
    with pytest.raises(VisualLimit):service._private_copy(source,linked/'copy.png',digest,100)


def test_failed_worker_publication_removes_orphan_pixels_but_preserves_usage(image_factory, image_configuration, tmp_path, monkeypatch):
    design_id, operation_id = seed_image(image_factory,image_configuration)
    adapter=FakeImageAdapter('openai',image_configuration,image_factory)
    monkeypatch.setattr(image_tasks,'queue_version_reviews',lambda *_: (_ for _ in ()).throw(VisualLimit('synthetic publication rejection')))
    assert image_tasks.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)['status']=='persisted'
    with image_factory() as session:
        assert session.scalar(select(DesignVersion)) is None
        assert session.get(BudgetOperation,operation_id).status=='unknown'
        assert session.get(DesignOperation,operation_id).payload['status']=='failed'
        assert session.get(DesignSession,design_id).state['submission_count']==1
    assert list(tmp_path.rglob('*.png'))==[]


@pytest.mark.parametrize('traps', [False, True])
def test_visual_money_is_exact_independent_of_caller_decimal_context(traps):
    state=VisualState('improve','0.300001',100)
    with localcontext() as context:
        context.prec=1;context.traps[Inexact]=traps
        state.authorize('a','a','generation','0.100001',now=101,expected_fence=0,epoch=1,review_money='0',review_calls=0,review_seconds=0)
        state.authorize('b','b','vision','0.2',now=102,expected_fence=0,epoch=1,review_money='0',review_calls=0,review_seconds=0)
        assert state.exposure()==Decimal('0.300001')
        with pytest.raises(VisualLimit,match='Money'):state.authorize('c','c','vision','0.000001',now=103,expected_fence=0,epoch=1,review_money='0',review_calls=0,review_seconds=0)
    assert state.request_count==2 and len(state.operations)==2


@pytest.mark.parametrize('value', ['NaN', 'Infinity','-1',{},None])
def test_invalid_visual_money_never_authorizes(value):
    with pytest.raises(VisualLimit):decimal(value)


@pytest.mark.parametrize('changes', [
 {'request_count':True},{'paid_admitted':1},{'mode':'other'},{'status':'other'},
 {'admitted_at':float('nan')},{'admitted_at':-1},{'epoch':0},{'fence':-1},
 {'request_count':25},{'submission_count':5},{'poll_count':61},{'version_count':4},
 {'intentional_edits':3},{'renderer_attempts':7},
 {'operations':{'a':{'kind':'other','status':'authorized'}}},
 {'operations':{'a':{'kind':'vision','status':'settled','actual':None}}},
 {'request_count':1},
])
def test_corrupt_visual_state_is_rejected_before_authority(changes):
    with pytest.raises(VisualLimit): VisualState(**{'mode':'improve','maximum_usd':'2','admitted_at':100,**changes})


def test_visual_settlement_is_idempotent_and_monotone():
    state=VisualState('improve','2',100)
    state.authorize('a','a','generation','0.4',now=101,expected_fence=0,epoch=1)
    with pytest.raises(VisualLimit):state.authorize('a','a','generation','0.4',now=101,expected_fence=0,epoch=1)
    state.settle('a',None);assert state.exposure()==Decimal('.4')
    state.settle('a','.2');state.settle('a','.2');state.settle('a',None)
    assert state.exposure()==Decimal('.2')
    with pytest.raises(VisualLimit):state.settle('a','.3')
    assert VisualState(**asdict(state)).exposure()==Decimal('.2')
    with pytest.raises(VisualLimit):VisualState('improve','2',100).publish_version(parent_hash='wrong',expected_parent_hash='wrong')


def test_generation_and_inference_limits_are_independent():
    state=VisualState('review_only','2',100)
    with pytest.raises(VisualLimit):state.authorize('a','a','generation','.1',now=101,expected_fence=0,epoch=1)
    state=VisualState('improve','2',100)
    with pytest.raises(VisualLimit):state.authorize('a','a','edit','.1',now=101,expected_fence=0,epoch=1)
    with pytest.raises(VisualLimit):state.authorize('a','a','vision',None,now=101,expected_fence=0,epoch=1)


def test_private_source_copy_never_opens_fifo_and_preserves_exact_bounds(tmp_path):
    import os
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    source=tmp_path/'source';source.write_bytes(b'private')
    digest=hashlib.sha256(b'private').hexdigest()
    with pytest.raises(VisualLimit):service._private_copy(fifo,tmp_path/'copy',digest,100)
    with pytest.raises(VisualLimit):service._private_copy(source,fifo,digest,100)
    with pytest.raises(VisualLimit):service.checked_private_bytes(tmp_path/'missing',digest,100)
    with pytest.raises(VisualLimit):service.checked_private_bytes(source,digest,1)
    assert source.read_bytes()==b'private'
