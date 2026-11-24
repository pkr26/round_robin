"""Bounded reads of operator-controlled configuration files."""
import os
import stat
from pathlib import Path


def read_operator_configuration(path: Path) -> str:
    """Read a regular configuration target, allowing existing symlink use."""
    maximum = 128 * 1024
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("Operator configuration is unavailable")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("Operator configuration is unavailable")
        return content.decode("utf-8")
    finally:
        if descriptor is not None:
            os.close(descriptor)
