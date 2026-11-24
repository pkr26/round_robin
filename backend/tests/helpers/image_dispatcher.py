"""Run the real combined dispatcher under an explicit loopback test boundary."""
import os

if os.environ.get("ROUND_ROBIN_IMAGE_PROCESS_FIXTURE") != "1":
    raise RuntimeError("Synthetic image dispatcher requires its explicit test profile")

import pytest
from backend.tests.conftest import PROVIDER_CREDENTIAL_NAMES, _install_network_boundary

if any(os.environ.get(name) for name in PROVIDER_CREDENTIAL_NAMES):
    raise RuntimeError("Synthetic image dispatcher refuses provider credentials")
_BOUNDARY = pytest.MonkeyPatch()
_install_network_boundary(_BOUNDARY)

from round_robin.dispatcher.service import main

if __name__ == "__main__":
    main()
