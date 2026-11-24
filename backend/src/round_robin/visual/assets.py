"""Bounded PNG/JPEG decoding, canonical pixels and immutable private storage."""
from dataclasses import dataclass
import hashlib
from io import BytesIO
import os
from pathlib import Path
import stat
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 8192
MAX_CANONICAL_BYTES = 64 * 1024 * 1024


class UnsafeRaster(ValueError):
    pass


@dataclass(frozen=True)
class Raster:
    canonical_bytes: bytes
    original_hash: str
    canonical_hash: str
    width: int
    height: int
    media_type: str = "image/png"


def validate_raster(data: bytes, declared_media: str | None = None) -> Raster:
    """Validate newly supplied upload bytes under the upload byte allowance."""
    return _validate_raster(data, declared_media, MAX_UPLOAD_BYTES)


def validate_canonical_raster(data: bytes) -> Raster:
    """Validate normalized/generated PNGs under their separate storage bound."""
    return _validate_raster(data, "image/png", MAX_CANONICAL_BYTES)


def _validate_raster(data: bytes, declared_media: str | None, maximum_bytes: int) -> Raster:
    if not data or len(data) > maximum_bytes:
        raise UnsafeRaster("Raster input must contain at most ten MiB" if maximum_bytes == MAX_UPLOAD_BYTES else "Canonical raster exceeds the storage bound")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Select the decoders before parsing any untrusted header. Checking
            # probe.format afterwards still invokes every registered decoder.
            with Image.open(BytesIO(data), formats=("PNG", "JPEG")) as probe:
                if probe.format not in {"PNG", "JPEG"} or getattr(probe, "n_frames", 1) != 1:
                    raise UnsafeRaster("Use a single PNG or JPEG image")
                expected = "image/png" if probe.format == "PNG" else "image/jpeg"
                if declared_media is not None and expected != declared_media:
                    raise UnsafeRaster("Declared image type differs from its actual bytes")
                if probe.width * probe.height > MAX_PIXELS:
                    raise UnsafeRaster("Raster exceeds sixteen megapixels")
                if probe.width > MAX_DIMENSION or probe.height > MAX_DIMENSION:
                    raise UnsafeRaster("Raster dimensions must each be at most 8192 pixels")
                probe.verify()
            with Image.open(BytesIO(data), formats=("PNG", "JPEG")) as source:
                source.load()
                image = ImageOps.exif_transpose(source)
                image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in source.info else "RGB")
                # Rebuild pixel-only content. Source EXIF, location, ICC, comments,
                # text chunks and other metadata are deliberately not retained.
                canonical = Image.frombytes(image.mode, image.size, image.tobytes())
                output = BytesIO()
                canonical.save(output, format="PNG")
                encoded = output.getvalue()
                if len(encoded) > MAX_CANONICAL_BYTES:
                    raise UnsafeRaster("Canonical raster exceeds the storage bound")
                return Raster(encoded, hashlib.sha256(data).hexdigest(), hashlib.sha256(encoded).hexdigest(), canonical.width, canonical.height)
    except UnsafeRaster:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise UnsafeRaster("Raster bytes cannot be safely decoded") from error


def reject_linked_path(path: Path) -> None:
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise UnsafeRaster("Private visual storage cannot contain linked path components")


def store_raster(directory: Path, raster: Raster) -> Path:
    reject_linked_path(directory)
    if hashlib.sha256(raster.canonical_bytes).hexdigest() != raster.canonical_hash:
        raise UnsafeRaster("Canonical raster bytes differ from their immutable identity")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{raster.canonical_hash}.png"
    reject_linked_path(path)
    if path.exists():
        verified_bytes(path, raster.canonical_hash)
        return path
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(raster.canonical_bytes)
            target.flush()
            os.fsync(target.fileno())
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def verified_bytes(path: Path, expected_hash: str) -> bytes:
    reject_linked_path(path)
    # O_NONBLOCK prevents a corrupt FIFO from hanging a request, O_NOFOLLOW
    # closes the final-component symlink race, and fstat binds checks to the
    # same descriptor that is read.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CANONICAL_BYTES:
            raise UnsafeRaster("Stored canonical asset exceeds the regular-file byte bound")
        content = source.read(MAX_CANONICAL_BYTES + 1)
    if len(content) > MAX_CANONICAL_BYTES:
        raise UnsafeRaster("Stored canonical asset exceeds the byte bound")
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise UnsafeRaster("Stored canonical asset failed its checksum")
    return content
