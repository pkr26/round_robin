"""Opt-in actual Docker/browser isolation, with synthetic source and no providers."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import base64
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest
from PIL import Image, ImageChops

from backend.tests.test_api import command, create
from backend.tests.test_hosted import hosted, session_client
from round_robin.visual.renderers.diagram import diagram_font

from round_robin.visual.renderers import interface
from round_robin.visual.renderers.interface import NativeRendererConfiguration, NativeRendererTimeout, render_interface

pytestmark = pytest.mark.skipif(os.environ.get("ROUND_ROBIN_TEST_NATIVE_RENDERER") != "1", reason="Actual native browser execution requires an explicitly enabled Docker test profile")


@pytest.fixture
def configuration():
    image = os.environ.get("ROUND_ROBIN_NATIVE_RENDERER_IMAGE", "round-robin-native-renderer:local")
    result = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], capture_output=True, timeout=5, check=False)
    assert result.returncode == 0, "Build the native-renderer Dockerfile before opting into its integration profile"
    return NativeRendererConfiguration(enabled=True, image=image)


def docker_json(arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, timeout=5, check=False)
    assert result.returncode == 0, "Disposable native test Docker inspection failed"
    return json.loads(result.stdout)


def test_actual_browser_click_fill_press_and_exact_narrow_viewport(configuration):
    source = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{background:#eef3ec;font:18px sans-serif;padding:18px}button,input{font:inherit;padding:12px}</style>
    <form id="form"><label>Name<input id="name"></label><button id="submit">Greet</button></form>
    <p id="result">Waiting</p><p id="width"></p><p id="environment"></p>
    <script>width.textContent=String(innerWidth);environment.textContent=typeof process+':'+typeof require;
    form.onsubmit=e=>{e.preventDefault();result.textContent='Hello '+name.value};</script>"""
    # Avoid window.name/id collisions by explicitly accessing the input in source.
    source = source.replace("'Hello '+name.value", "'Hello '+document.getElementById('name').value")
    rendered = render_interface(source, (320, 640), [
        {"action": "fill", "selector": "#name", "value": "Ada", "assertion": {"selector": "#name", "value": "Ada"}},
        {"action": "press", "selector": "#name", "key": "Enter", "assertion": {"selector": "#result", "text": "Hello Ada"}},
        {"action": "click", "selector": "#submit", "assertion": {"selector": "#width", "text": "320"}},
        {"action": "click", "selector": "#submit", "assertion": {"selector": "#environment", "text": "undefined:undefined"}},
    ], configuration=configuration)
    assert (rendered.raster.width, rendered.raster.height) == (320, 640)
    assert all(check["status"] == check["assertion"] == "passed" for check in rendered.checks)
    assert rendered.source_hash == hashlib.sha256(source.encode()).hexdigest()
    assert rendered.snapshot.canonical_pixel_sha256 == hashlib.sha256(rendered.image_bytes).hexdigest()
    assert rendered.snapshot.configuration_hash == rendered.configuration_hash
    actual = subprocess.run(["docker", "image", "inspect", configuration.image, "--format", "{{.Id}}"], capture_output=True, timeout=5, check=True).stdout.decode().strip()
    assert rendered.snapshot.image_id == actual
    assert rendered.snapshot.playwright_version == "1.65.1"
    assert rendered.snapshot.network_attempt_count == 0


def test_external_fetch_images_and_websockets_are_blocked_and_not_logged_as_urls(configuration):
    secret_url = "https://example.invalid/private?sentinel=never-log-the-url"
    source = f"""<!doctype html><img src="{secret_url}"><button id="done" style="display:none">blocked</button>
    <script>new WebSocket('wss://example.invalid/socket');
    fetch('{secret_url}').catch(()=>{{document.getElementById('done').style.display='block'}});</script>"""
    rendered = render_interface(source, (640, 480), [{"action": "click", "selector": "#done", "assertion": {"selector": "#done", "text": "blocked"}}], configuration=configuration)
    assert rendered.checks[0]["status"] == "passed"
    assert 2 <= rendered.snapshot.network_attempt_count <= 32
    assert all(attempt.disposition == "blocked" for attempt in rendered.snapshot.network_attempts)
    assert secret_url not in rendered.snapshot.model_dump_json()
    assert "never-log-the-url" not in rendered.snapshot.model_dump_json()


def test_container_actual_security_flags_and_absence_of_operator_environment(configuration, monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_SECRET_SENTINEL", "must-not-enter-the-renderer")
    own_jobs = []
    original = interface._bounded_process
    def observe(arguments, *args, **kwargs):
        if arguments[:2] == ["docker", "run"]:
            own_jobs.append(arguments[arguments.index("--name") + 1])
        return original(arguments, *args, **kwargs)
    monkeypatch.setattr(interface, "_bounded_process", observe)
    source = "<!doctype html><p>Bounded execution</p><script>const start=Date.now();while(Date.now()-start<2000){}</script>"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(render_interface, source, (400, 300), configuration=configuration)
        observed = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not future.done():
            if own_jobs:
                result = subprocess.run(["docker", "inspect", own_jobs[0]], capture_output=True, timeout=3, check=False)
                if result.returncode == 0:
                    observed = json.loads(result.stdout)[0]
                    break
            time.sleep(.025)
        rendered = future.result(timeout=25)
    assert observed is not None
    host, container = observed["HostConfig"], observed["Config"]
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert any("no-new-privileges" in item for item in host["SecurityOpt"])
    assert host["Memory"] == host["MemorySwap"] == 512 * 1024 * 1024
    assert host["PidsLimit"] == 128 and host["NanoCpus"] == 1_000_000_000
    assert not host["Binds"]
    assert container["User"] == "10001:10001"
    assert "size=134217728" in host["Tmpfs"]["/tmp"]
    assert all("ROUND_ROBIN_NATIVE_SECRET_SENTINEL" not in entry for entry in container["Env"])
    assert host["LogConfig"]["Type"] == "none"
    assert rendered.snapshot.sandbox["operator_environment"] is False


def test_infinite_browser_script_has_a_deadline_and_cleans_only_its_own_container(configuration, monkeypatch):
    sentinel = "rr-native-sentinel-" + uuid4().hex
    started = subprocess.run(["docker", "run", "-d", "--rm", "--pull=never", "--name", sentinel, "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=10001:10001", "--entrypoint", "node", configuration.image, "-e", "setTimeout(()=>{},15000)"], capture_output=True, timeout=5, check=False)
    assert started.returncode == 0
    own_jobs = []
    original = interface._bounded_process
    def observe(arguments, *args, **kwargs):
        if arguments[:2] == ["docker", "run"]:
            own_jobs.append(arguments[arguments.index("--name") + 1])
        return original(arguments, *args, **kwargs)
    monkeypatch.setattr(interface, "_bounded_process", observe)
    try:
        began = time.monotonic()
        with pytest.raises(NativeRendererTimeout):
            render_interface("<!doctype html><script>for(;;){}</script>", (400, 300), configuration=replace(configuration, browser_timeout_ms=1000, host_timeout_seconds=2.5))
        assert time.monotonic() - began < 7
        assert len(own_jobs) == 1
        check = subprocess.run(["docker", "inspect", own_jobs[0]], capture_output=True, timeout=3, check=False)
        assert check.returncode != 0
        assert docker_json(["inspect", sentinel])[0]["State"]["Running"] is True
    finally:
        subprocess.run(["docker", "rm", "--force", sentinel], capture_output=True, timeout=3, check=False)


def test_actual_playwright_deadline_wins_before_a_delayed_watchdog_and_stays_a_timeout(configuration, monkeypatch):
    """Scheduling of the JS watchdog must not decide deadline failure identity.

    Delay only the trusted runner watchdog. Chromium and Playwright still run
    their real timers against the unchanged original one-second deadline.
    """
    import base64
    bootstrap = """const original = globalThis.setTimeout;
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (typeof callback === 'function' && callback.toString().includes('expired = true') && delay === 1000) {
        process.stderr.write('synthetic_delayed_watchdog\\n');
        return original(callback, delay + 500, ...args);
      }
      return original(callback, delay, ...args);
    };"""
    preload = "data:text/javascript;base64," + base64.b64encode(bootstrap.encode()).decode()
    observations = []
    original = interface._bounded_process
    def delay_watchdog(arguments, *args, **kwargs):
        if arguments[:2] != ["docker", "run"]:
            return original(arguments, *args, **kwargs)
        arguments = [*arguments[:-1], "--import", preload, arguments[-1]]
        result = original(arguments, *args, **kwargs)
        observations.append(result)
        return result
    monkeypatch.setattr(interface, "_bounded_process", delay_watchdog)
    started = time.monotonic()
    with pytest.raises(NativeRendererTimeout):
        render_interface("<!doctype html><script>for(;;){}</script>", (400, 300), configuration=replace(configuration, browser_timeout_ms=1000, host_timeout_seconds=2.5))
    assert time.monotonic() - started < 7
    assert len(observations) == 1
    code, output, diagnostics = observations[0]
    assert b"synthetic_delayed_watchdog" in diagnostics
    assert code == 124
    assert json.loads(output) == {"schema_version": "1.0", "error": "render_timeout"}


def test_individual_action_timeout_remains_a_failed_check_with_usable_pixels(configuration):
    rendered = render_interface("<!doctype html><p id='ready'>Ready</p>", (400, 300),
        [{"action": "click", "selector": "#missing", "assertion": {"selector": "#ready", "text": "Ready"}}],
        configuration=replace(configuration, browser_timeout_ms=5000, host_timeout_seconds=8))
    assert (rendered.raster.width, rendered.raster.height) == (400, 300)
    assert rendered.checks[0]["status"] == "failed"
    assert rendered.checks[0]["assertion"] == "unavailable"
    assert rendered.snapshot.page_error_count == 0
    assert rendered.snapshot.network_attempt_count == 0


def test_runtime_image_omits_build_tools_unused_browsers_and_media_but_keeps_package_metadata(configuration):
    script = r"""const fs=require('node:fs'),crypto=require('node:crypto');
const status=fs.readFileSync('/var/lib/dpkg/status','utf8');
console.log(JSON.stringify({uid:process.getuid(),node:process.version,
  playwright:require('playwright/package.json').version,
  rendererSha256:crypto.createHash('sha256').update(fs.readFileSync('/opt/renderer/native-renderer.mjs')).digest('hex'),
  browserDirectories:fs.readdirSync('/ms-playwright'),
  buildTools:['/usr/bin/npm','/usr/bin/npx','/usr/lib/node_modules/npm','/usr/bin/pebble'].filter(p=>fs.existsSync(p)),
  packages:status.split('\n').filter(line=>line.startsWith('Package: ')).map(line=>line.slice(9))}));"""
    result = subprocess.run(["docker", "run", "--pull=never", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--entrypoint", "node", configuration.image, "-e", script],
        capture_output=True, text=True, timeout=15, check=True)
    inventory = json.loads(result.stdout)
    assert inventory["uid"] == 10001 and inventory["playwright"] == "1.65.1"
    assert inventory["node"].startswith("v26.")
    assert inventory["rendererSha256"] == hashlib.sha256((Path(__file__).resolve().parents[3] / "scripts/native-renderer.mjs").read_bytes()).hexdigest()
    assert inventory["buildTools"] == []
    assert len(inventory["browserDirectories"]) == 1
    assert inventory["browserDirectories"][0].startswith("chromium_headless_shell-")
    assert {"libnss3", "libc6", "libfreetype6"}.issubset(inventory["packages"])
    assert not any(name.startswith(("gstreamer", "libgstreamer", "libavcodec", "libavformat", "libavfilter")) or name == "ffmpeg" for name in inventory["packages"])


@pytest.mark.parametrize("label", ["i" * 120, "S" * 60, "W" * 33, "  S " * 20])
def test_downloaded_svg_uses_the_validated_font_and_keeps_edge_ink_inside_viewport(hosted, monkeypatch, configuration, label):
    monkeypatch.setenv("ROUND_ROBIN_VISUAL_ENABLED", "1")
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    client, _, csrf, _ = session_client(hosted)
    client.headers["x-csrf-token"] = csrf
    meeting = create(client)
    response = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json=command(
        mode="review_only", maximum_usd="0", brief={"purpose": "Check exact diagram glyph bounds", "audience": "Reviewer", "domain": "structured_diagram", "width": 500, "height": 200}))
    assert response.status_code == 201, response.text
    design = response.json()
    source = {"width": 500, "height": 200, "nodes": [
        {"id": "node_a", "label": "A", "x": 50, "y": 0, "width": 60, "height": 40},
        {"id": "node_b", "label": "B", "x": 140, "y": 120, "width": 60, "height": 40}],
        "edges": [{"source": "node_a", "target": "node_b", "label": label}]}
    response = client.post(f"/api/design-sessions/{design['id']}/native-diagram", json=command(expected_revision=design["revision"], diagram=source))
    assert response.status_code == 201, response.text
    version = response.json()["versions"][0]
    download = client.get(f"/api/design-versions/{version['id']}/source.svg")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    # Preview the downloaded bytes with every actual response CSP enforced.
    # Content-Disposition is omitted only so the fixture document can render.
    packet = {"svg": download.text, "csp": ", ".join(download.headers.get_list("content-security-policy"))}
    script = r"""
const {chromium}=require('/opt/renderer/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:700,height:200},deviceScaleFactor:1});
 const requests=[];
 await context.route('**/*',route=>{
  requests.push(route.request().url());
  if(route.request().url()!=='https://fixture.invalid/diagram.svg')return route.abort();
  return route.fulfill({contentType:'image/svg+xml',headers:{'Content-Security-Policy':input.csp},body:input.svg});
 });
 const page=await context.newPage();
 await page.goto('https://fixture.invalid/diagram.svg');
 await page.evaluate(()=>document.fonts.ready);
 const geometry=await page.locator('text').first().evaluate(e=>({
  x:e.getBBox().x,right:e.getBBox().x+e.getBBox().width,text:e.textContent,
  whitespace:getComputedStyle(e).whiteSpace,
  fonts:Array.from(document.fonts,font=>({family:font.family,status:font.status}))
 }));
 const pixels=(await page.screenshot({type:'png'})).toString('base64');
 // Verify that the fixture actually distinguishes the former fallback font.
 await page.locator('style').evaluate(element=>element.remove());
 await page.locator('text').first().evaluate(element=>{element.removeAttribute('textLength');element.removeAttribute('lengthAdjust')});
 const fallbackRight=await page.locator('text').first().evaluate(e=>e.getBBox().x+e.getBBox().width);
 console.log(JSON.stringify({geometry,pixels,fallbackRight,requests}));
 await browser.close();
})().catch(error=>{console.error(error.message);process.exit(1)});
"""
    result = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "256", "--memory", "768m", "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "--entrypoint", "node", configuration.image, "-"],
        input=("const input=" + json.dumps(packet) + ";\n" + script).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    observed = json.loads(result.stdout)
    assert observed["geometry"]["fonts"] == [{"family": "RoundRobinDiagram", "status": "loaded"}]
    expected_right = 125 + diagram_font().getbbox(label, anchor="ls")[2]
    assert observed["geometry"]["x"] == 125 and observed["geometry"]["right"] <= expected_right + 1
    assert observed["geometry"]["text"] == label and observed["geometry"]["whitespace"] == "pre"
    if not label.startswith(" "):
        assert observed["fallbackRight"] > expected_right
    assert observed["requests"] == ["https://fixture.invalid/diagram.svg"]
    image = Image.open(BytesIO(base64.b64decode(observed["pixels"]))).convert("RGB")
    # This strip contains the relationship label, with boxes outside its y range.
    strip = image.crop((120, 50, 700, 90))
    ink = ImageChops.difference(strip, Image.new("RGB", strip.size, "white")).getbbox()
    assert ink is not None and 400 <= ink[2] + 120 <= expected_right + 2 <= source["width"]
