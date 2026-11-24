"""Explicitly enabled HTML execution inside a hardened Docker container.

Source enters through stdin, never a host mount or command argument. Only bounded
typed screenshots and declared interaction checks return to the application.
"""
from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
import re
import selectors
import subprocess
import threading
import time
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from round_robin.schemas.common import Contract, Hash, ShortText
from ..assets import Raster, validate_raster

RENDERER_VERSION = "native-interface-browser-v1"
MAX_STDOUT = 12 * 1024 * 1024


class IsolatedRendererRequired(RuntimeError):
    pass


class NativeRendererError(RuntimeError):
    pass


class NativeRendererTimeout(NativeRendererError):
    pass


class Viewport(Contract):
    width: int = Field(ge=100, le=4000, strict=True)
    height: int = Field(ge=100, le=4000, strict=True)

    @model_validator(mode="after")
    def pixels(self) -> Self:
        if self.width * self.height > 16_000_000:
            raise ValueError("Native screenshot exceeds sixteen megapixels")
        return self


class DOMAssertion(Contract):
    selector: str = Field(min_length=1, max_length=500)
    text: str | None = Field(default=None, max_length=2000)
    value: str | None = Field(default=None, max_length=2000)
    visible: bool | None = None

    @model_validator(mode="after")
    def expected_value(self) -> Self:
        if self.text is None and self.value is None and self.visible is None:
            raise ValueError("A DOM assertion needs an expected text, value or visibility")
        return self


class InterfaceAction(Contract):
    action: Literal["click", "fill", "press"]
    selector: str = Field(min_length=1, max_length=500)
    value: str | None = Field(default=None, max_length=2000)
    key: Literal["Enter", "Tab", "Shift+Tab", "Escape", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "Backspace", "Delete"] | None = None
    assertion: DOMAssertion | None = None

    @model_validator(mode="after")
    def action_arguments(self) -> Self:
        if self.action == "fill" and self.value is None or self.action != "fill" and self.value is not None:
            raise ValueError("Only a fill action accepts its required value")
        if self.action == "press" and self.key is None or self.action != "press" and self.key is not None:
            raise ValueError("Only a press action accepts its required allowlisted key")
        return self


class ActionCheck(Contract):
    index: int = Field(ge=0, le=19, strict=True)
    action: Literal["click", "fill", "press"]
    selector_sha256: Hash
    status: Literal["passed", "failed"]
    assertion: Literal["passed", "failed", "pending", "unavailable", "not_requested"]


class NetworkAttempt(Contract):
    kind: str = Field(min_length=1, max_length=100)
    url_sha256: Hash
    disposition: Literal["blocked"]


class RenderSnapshot(Contract):
    schema_version: Literal["1.0"] = "1.0"
    renderer_version: Literal["native-interface-browser-v1"] = RENDERER_VERSION
    source_sha256: Hash
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    playwright_version: ShortText
    browser_version: ShortText
    configuration_hash: Hash
    screenshot_sha256: Hash
    canonical_pixel_sha256: Hash
    viewport: Viewport
    checks: list[ActionCheck] = Field(max_length=20)
    network_attempts: list[NetworkAttempt] = Field(max_length=32)
    network_attempt_count: int = Field(ge=0, le=32, strict=True)
    page_error_count: int = Field(ge=0, le=100, strict=True)
    sandbox: dict
    limitations: list[ShortText] = Field(max_length=10)


class ContainerResult(Contract):
    schema_version: Literal["1.0"]
    renderer_version: Literal["native-interface-browser-v1"]
    playwright_version: ShortText
    browser_version: ShortText
    source_sha256: Hash
    configuration_hash: Hash
    viewport: Viewport
    screenshot_sha256: Hash
    screenshot_base64: str = Field(min_length=1, max_length=12 * 1024 * 1024)
    checks: list[ActionCheck] = Field(max_length=20)
    network_attempts: list[NetworkAttempt] = Field(max_length=32)
    network_attempt_count: int = Field(ge=0, le=32, strict=True)
    page_error_count: int = Field(ge=0, le=100, strict=True)
    limitations: list[ShortText] = Field(max_length=10)


@dataclass(frozen=True)
class NativeRendererConfiguration:
    enabled: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED") == "1")
    image: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_NATIVE_RENDERER_IMAGE", ""))
    browser_timeout_ms: int = 20000
    host_timeout_seconds: float = 25

    def validate(self):
        if not self.enabled or not self.image:
            raise IsolatedRendererRequired("Native interfaces require an explicitly enabled, configured local isolated renderer")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]{0,255}", self.image):
            raise IsolatedRendererRequired("Native renderer image configuration is invalid")
        if type(self.browser_timeout_ms) is not int or not 250 <= self.browser_timeout_ms <= 20000 or not 1 <= self.host_timeout_seconds <= 25 or self.host_timeout_seconds * 1000 < self.browser_timeout_ms:
            raise IsolatedRendererRequired("Native renderer deadlines exceed their bounded execution profile")


@dataclass(frozen=True)
class RenderedInterface:
    source: str
    source_hash: str
    raster: Raster
    renderer_version: str
    configuration_hash: str
    checks: list[dict]
    snapshot: RenderSnapshot

    @property
    def image_bytes(self) -> bytes:
        return self.raster.canonical_bytes


def _bounded_process(arguments: list[str], content: bytes, *, timeout: float, stdout_limit: int, stderr_limit=8192):
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise IsolatedRendererRequired("Local Docker renderer is unavailable") from error
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    writer = None
    try:
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        def write():
            try:
                process.stdin.write(content)
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeRendererTimeout("Native renderer exceeded its host deadline")
            for key, _ in selector.select(min(.1, remaining)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.data]
                buffer.extend(chunk)
                if len(buffer) > (stdout_limit if key.data == "stdout" else stderr_limit):
                    raise NativeRendererError("Native renderer exceeded its bounded output allowance")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeRendererTimeout("Native renderer exceeded its host deadline")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise NativeRendererTimeout("Native renderer exceeded its host deadline") from error
        return code, bytes(buffers["stdout"]), bytes(buffers["stderr"])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        selector.close()
        for name in ("stdout", "stderr"):
            getattr(process, name).close()
        if writer and writer.ident is not None:
            writer.join(timeout=.5)
        else:
            process.stdin.close()


def _sandbox_profile() -> dict:
    return {"version": "docker-native-v1", "network": "none", "read_only": True, "capabilities": "none", "no_new_privileges": True, "user": "10001:10001", "memory_bytes": 536870912, "pids": 128, "cpus": 1, "tmpfs_bytes": 134217728, "host_mounts": False, "operator_environment": False, "cookies": "new_empty_context", "service_workers": "blocked"}


def render_interface(source: str, viewport: tuple[int, int] = (1280, 800), actions: list[dict | InterfaceAction] | None = None, *, configuration: NativeRendererConfiguration | None = None) -> RenderedInterface:
    configuration = configuration or NativeRendererConfiguration()
    configuration.validate()
    if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > 128 * 1024:
        raise NativeRendererError("Native HTML must be nonempty UTF-8 source of at most 128 KiB")
    if not isinstance(viewport, (tuple, list)) or len(viewport) != 2:
        raise NativeRendererError("Native viewport requires width and height")
    dimensions = Viewport(width=viewport[0], height=viewport[1])
    if actions is None:
        actions = []
    if not isinstance(actions, list) or len(actions) > 20:
        raise NativeRendererError("Native interaction plans contain at most twenty actions")
    validated = [item if isinstance(item, InterfaceAction) else InterfaceAction.model_validate(item) for item in actions]
    code, output, _ = _bounded_process(["docker", "image", "inspect", configuration.image, "--format", "{{.Id}}"], b"", timeout=4, stdout_limit=256)
    image_id = output.decode("ascii", errors="replace").strip()
    if code != 0 or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise IsolatedRendererRequired("Configured native renderer image must already exist locally; automatic pulls are disabled")
    sandbox = _sandbox_profile()
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    settings = {"image_id": image_id, "renderer_version": RENDERER_VERSION, "viewport": dimensions.model_dump(), "actions": [item.model_dump(mode="json") for item in validated], "browser_timeout_ms": configuration.browser_timeout_ms, "host_timeout_seconds": configuration.host_timeout_seconds, "sandbox": sandbox}
    configuration_hash = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    packet = {"schema_version": "1.0", "source": source, "source_sha256": source_hash, "configuration_hash": configuration_hash, "viewport": dimensions.model_dump(), "actions": settings["actions"], "timeout_ms": configuration.browser_timeout_ms}
    serialized = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode()
    if len(serialized) > 160 * 1024:
        raise NativeRendererError("Combined native source and interaction metadata exceed 160 KiB")
    job = "rr-native-" + uuid4().hex
    arguments = ["docker", "run", "--rm", "--pull=never", "--name", job, "--label", "org.roundrobin.native-job=" + job, "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=10001:10001", "--memory=512m", "--memory-swap=512m", "--pids-limit=128", "--cpus=1", "--ipc=private", "--shm-size=64m", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=134217728,uid=10001,gid=10001,mode=700", "--ulimit", "nofile=256:256", "--log-driver=none", "--env", "LANG=C.UTF-8", "--env", "TZ=UTC", "--env", "TMPDIR=/tmp", "--entrypoint", "node", "-i", image_id, "/opt/renderer/native-renderer.mjs"]
    try:
        code, output, _ = _bounded_process(arguments, serialized, timeout=configuration.host_timeout_seconds, stdout_limit=MAX_STDOUT)
    finally:
        # Only this invocation's server-issued UUID name is eligible for cleanup.
        if re.fullmatch(r"rr-native-[a-f0-9]{32}", job):
            try:
                _bounded_process(["docker", "rm", "--force", job], b"", timeout=3, stdout_limit=256)
            except (NativeRendererError, IsolatedRendererRequired):
                pass
    if code == 124:
        raise NativeRendererTimeout("Native browser source exceeded its execution deadline")
    if code != 0:
        raise NativeRendererError("Native browser rendering failed inside its isolated container")
    try:
        result = ContainerResult.model_validate_json(output)
        image = base64.b64decode(result.screenshot_base64, validate=True)
        if hashlib.sha256(image).hexdigest() != result.screenshot_sha256 or result.source_sha256 != source_hash or result.configuration_hash != configuration_hash or result.viewport != dimensions or len(result.checks) != len(validated):
            raise ValueError("renderer_binding")
        raster = validate_raster(image, "image/png")
        if (raster.width, raster.height) != (dimensions.width, dimensions.height):
            raise ValueError("viewport_mismatch")
        if result.network_attempt_count != len(result.network_attempts):
            raise ValueError("network_receipt_binding")
        for index, (check, action) in enumerate(zip(result.checks, validated, strict=True)):
            if check.index != index or check.action != action.action or check.selector_sha256 != hashlib.sha256(action.selector.encode()).hexdigest() or check.assertion == "pending":
                raise ValueError("action_binding")
            if action.assertion is None:
                if check.assertion != "not_requested":
                    raise ValueError("unrequested_assertion")
            elif (check.status, check.assertion) not in {("passed", "passed"), ("failed", "failed"), ("failed", "unavailable")}:
                raise ValueError("assertion_binding")
        snapshot = RenderSnapshot(**result.model_dump(exclude={"screenshot_base64"}), image_id=image_id, canonical_pixel_sha256=raster.canonical_hash, sandbox=sandbox)
    except Exception as error:
        raise NativeRendererError("Native renderer output failed schema, provenance or image validation") from error
    return RenderedInterface(source, source_hash, raster, RENDERER_VERSION, configuration_hash, [item.model_dump(mode="json") for item in result.checks], snapshot)
