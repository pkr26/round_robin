"""Small, deterministic input fixtures for raster privacy and bounds."""
import hashlib
from io import BytesIO
import warnings

from PIL import Image, PngImagePlugin
import pytest

from round_robin.visual import assets
from round_robin.visual.assets import UnsafeRaster, validate_canonical_raster, validate_raster


def encode(image, format='PNG', **kwargs):
    out = BytesIO()
    image.save(out, format=format, **kwargs)
    return out.getvalue()


def decoded(content):
    with Image.open(BytesIO(content)) as image:
        image.load()
        return image.copy(), dict(image.info), image.format


def test_png_metadata_is_removed_while_rgba_pixels_and_identities_survive():
    image = Image.new('RGBA', (3, 2))
    image.putdata([(1, 2, 3, 0), (4, 5, 6, 127), (7, 8, 9, 255)] * 2)
    text = PngImagePlugin.PngInfo()
    text.add_text('private', 'sample-private-text-490')
    exif = image.getexif()
    exif[270] = 'sample-private-exif-501'
    raw = encode(image, pnginfo=text, exif=exif, icc_profile=b'sample-private-icc-819')
    normalized = validate_raster(raw, 'image/png')
    output, info, fmt = decoded(normalized.canonical_bytes)
    assert fmt == 'PNG' and output.mode == 'RGBA' and list(output.getdata()) == list(image.getdata())
    assert not info and not output.getexif()
    for sentinel in (b'sample-private-text-490', b'sample-private-exif-501', b'sample-private-icc-819'):
        assert sentinel not in normalized.canonical_bytes
    assert normalized.original_hash == hashlib.sha256(raw).hexdigest()
    assert normalized.canonical_hash == hashlib.sha256(normalized.canonical_bytes).hexdigest()
    assert (normalized.width, normalized.height, normalized.media_type) == (3, 2, 'image/png')
    again = validate_canonical_raster(normalized.canonical_bytes)
    assert again.canonical_hash == normalized.canonical_hash


def test_palette_transparency_survives_and_opaque_input_stays_rgb():
    palette = Image.new('P', (2, 1))
    palette.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    palette.putdata([0, 1])
    result = validate_raster(encode(palette, transparency=0))
    out, info, _ = decoded(result.canonical_bytes)
    assert out.mode == 'RGBA' and list(out.getdata()) == [(255, 0, 0, 0), (0, 255, 0, 255)] and not info
    opaque, _, _ = decoded(validate_raster(encode(Image.new('RGB', (2, 2), 'blue'))).canonical_bytes)
    assert opaque.mode == 'RGB'


def test_orientation_changes_actual_pixels_before_exif_removal():
    image = Image.new('RGB', (2, 3))
    image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)])
    exif = image.getexif()
    exif[274] = 6
    exif[270] = 'sample-private-description-719'
    normalized = validate_raster(encode(image, exif=exif), 'image/png')
    result, info, _ = decoded(normalized.canonical_bytes)
    assert result.size == (3, 2) and (normalized.width, normalized.height) == (3, 2)
    assert list(result.getdata()) == [image.getpixel((0, 2)), image.getpixel((0, 1)), image.getpixel((0, 0)), image.getpixel((1, 2)), image.getpixel((1, 1)), image.getpixel((1, 0))]
    assert not info and not result.getexif()


def test_both_parsing_passes_use_only_permitted_decoders(monkeypatch):
    calls = []
    original = Image.open
    def tracked(*args, **kwargs):
        calls.append(kwargs.get('formats'))
        return original(*args, **kwargs)
    monkeypatch.setattr(Image, 'open', tracked)
    validate_raster(encode(Image.new('RGB', (2, 2), 'red')), 'image/png')
    assert calls == [('PNG', 'JPEG'), ('PNG', 'JPEG')]


def test_disallowed_mismatch_truncated_and_animated_inputs_fail_closed():
    png = encode(Image.new('RGB', (2, 2), 'red'))
    jpeg = encode(Image.new('RGB', (2, 2), 'red'), 'JPEG')
    gif = encode(Image.new('RGB', (2, 2), 'red'), 'GIF')
    animated = encode(Image.new('RGB', (2, 2), 'red'), save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')], duration=40, loop=0)
    for raw, media in [(b'', None), (png, 'image/jpeg'), (jpeg, 'image/png'), (gif, None), (png[:30], 'image/png'), (animated, 'image/png')]:
        with pytest.raises(UnsafeRaster): validate_raster(raw, media)
    assert validate_raster(jpeg, 'image/jpeg').media_type == 'image/png'


def test_small_exact_byte_and_separate_upload_canonical_limits(monkeypatch):
    raw = encode(Image.new('RGB', (3, 3), 'blue'))
    monkeypatch.setattr(assets, 'MAX_UPLOAD_BYTES', len(raw))
    assert validate_raster(raw).width == 3
    monkeypatch.setattr(assets, 'MAX_UPLOAD_BYTES', len(raw) - 1)
    with pytest.raises(UnsafeRaster): validate_raster(raw)
    assert validate_canonical_raster(raw).width == 3
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', len(raw) - 1)
    with pytest.raises(UnsafeRaster): validate_canonical_raster(raw)


def test_small_pixel_and_each_dimension_exact_boundaries(monkeypatch):
    monkeypatch.setattr(assets, 'MAX_PIXELS', 12)
    monkeypatch.setattr(assets, 'MAX_DIMENSION', 20)
    assert (validate_raster(encode(Image.new('RGB', (3, 4)))).width) == 3
    with pytest.raises(UnsafeRaster): validate_raster(encode(Image.new('RGB', (3, 5))))
    monkeypatch.setattr(assets, 'MAX_PIXELS', 100)
    monkeypatch.setattr(assets, 'MAX_DIMENSION', 4)
    assert validate_raster(encode(Image.new('RGB', (4, 4)))).height == 4
    with pytest.raises(UnsafeRaster): validate_raster(encode(Image.new('RGB', (5, 4))))
    with pytest.raises(UnsafeRaster): validate_raster(encode(Image.new('RGB', (4, 5))))


def test_canonical_output_byte_limit_exact_boundary(monkeypatch):
    raw = encode(Image.new('RGB', (3, 3), 'green'))
    canonical_length = len(validate_raster(raw).canonical_bytes)
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', canonical_length)
    assert len(validate_raster(raw).canonical_bytes) == canonical_length
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', canonical_length - 1)
    with pytest.raises(UnsafeRaster): validate_raster(raw)


def test_generated_canonical_input_must_already_be_png():
    jpeg = encode(Image.new('RGB', (3, 3), 'red'), 'JPEG')
    with pytest.raises(UnsafeRaster): validate_canonical_raster(jpeg)
    assert validate_raster(jpeg, 'image/jpeg').media_type == 'image/png'


@pytest.mark.parametrize('mode', ['1', 'L', 'P'])
def test_noncolor_modes_normalize_to_rgb(mode):
    image = Image.new(mode, (2, 2))
    if mode == 'P': image.putpalette([255, 0, 0] + [0] * 765)
    raw = encode(image)
    expected = image.convert('RGB')
    out, info, _ = decoded(validate_raster(raw).canonical_bytes)
    assert out.mode == 'RGB' and list(out.getdata()) == list(expected.getdata()) and not info


def test_only_decompression_warning_is_promoted(monkeypatch):
    raw = encode(Image.new('RGB', (2, 2)))
    real = Image.open
    def warned(*args, **kwargs):
        warnings.warn('synthetic unrelated warning', UserWarning)
        return real(*args, **kwargs)
    monkeypatch.setattr(Image, 'open', warned)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        assert validate_raster(raw).width == 2


def test_tiny_synthetic_decompression_warning_is_rejected(monkeypatch):
    raw = encode(Image.new('RGB', (3, 3)))
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 5)
    with pytest.raises(UnsafeRaster): validate_raster(raw)


def test_rejections_explain_actionable_media_and_decode_errors():
    png = encode(Image.new('RGB', (2, 2)))
    animated = encode(Image.new('RGB', (2, 2)), save_all=True, append_images=[Image.new('RGB', (2, 2), 'red')], duration=40, loop=0)
    for data, media, expected in [
        (animated, 'image/png', ('single', 'png', 'jpeg')),
        (png, 'image/jpeg', ('declared', 'actual')),
        (b'not an image', None, ('safely decoded',)),
    ]:
        with pytest.raises(UnsafeRaster) as failure: validate_raster(data, media)
        message = str(failure.value).casefold()
        assert all(word in message for word in expected)


def test_rejections_distinguish_upload_and_canonical_byte_limits(monkeypatch):
    raw = encode(Image.new('RGB', (2, 2)))
    monkeypatch.setattr(assets, 'MAX_UPLOAD_BYTES', len(raw) - 1)
    with pytest.raises(UnsafeRaster) as failure: validate_raster(raw)
    assert 'ten mib' in str(failure.value).casefold()
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', len(raw) - 2)
    with pytest.raises(UnsafeRaster) as failure: validate_canonical_raster(raw)
    assert 'canonical' in str(failure.value).casefold()


def test_rejections_explain_pixel_and_dimension_limits(monkeypatch):
    raw = encode(Image.new('RGB', (3, 3)))
    monkeypatch.setattr(assets, 'MAX_PIXELS', 8)
    with pytest.raises(UnsafeRaster) as failure: validate_raster(raw)
    assert 'sixteen megapixels' in str(failure.value).casefold()
    monkeypatch.setattr(assets, 'MAX_PIXELS', 100)
    monkeypatch.setattr(assets, 'MAX_DIMENSION', 2)
    with pytest.raises(UnsafeRaster) as failure: validate_raster(raw)
    assert '8192' in str(failure.value)


def test_rejection_explains_canonical_output_bound(monkeypatch):
    raw = encode(Image.new('RGB', (3, 3)))
    size = len(validate_raster(raw).canonical_bytes)
    monkeypatch.setattr(assets, 'MAX_CANONICAL_BYTES', size - 1)
    with pytest.raises(UnsafeRaster) as failure: validate_raster(raw)
    assert 'canonical' in str(failure.value).casefold()
