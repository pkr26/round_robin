// Trusted runner. Supplied source is interpreted only by the isolated browser.
// No source, selector, page console, or exception stack is printed to logs.
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { chromium, errors } from "playwright";

const require = createRequire(import.meta.url);
const playwrightVersion = require("playwright/package.json").version;
const MAX_INPUT = 160 * 1024;
const MAX_PNG = 8 * 1024 * 1024;
const MAX_NETWORK_ATTEMPTS = 32;
const KEYS = new Set(["Enter", "Tab", "Shift+Tab", "Escape", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "Backspace", "Delete"]);
const sha = value => crypto.createHash("sha256").update(value).digest("hex");
let browser;
let expired = false;
let timeout;

function exactKeys(object, allowed) {
  if (!object || typeof object !== "object" || Array.isArray(object) || Object.keys(object).some(key => !allowed.includes(key))) throw new Error("input_invalid");
}

function validate(input) {
  exactKeys(input, ["schema_version", "source", "viewport", "actions", "timeout_ms", "source_sha256", "configuration_hash"]);
  if (input.schema_version !== "1.0" || typeof input.source !== "string" || !input.source.trim() || Buffer.byteLength(input.source) > 128 * 1024 || sha(Buffer.from(input.source)) !== input.source_sha256) throw new Error("input_invalid");
  exactKeys(input.viewport, ["width", "height"]);
  for (const dimension of [input.viewport.width, input.viewport.height]) if (!Number.isInteger(dimension) || dimension < 100 || dimension > 4000) throw new Error("input_invalid");
  if (input.viewport.width * input.viewport.height > 16_000_000 || !Array.isArray(input.actions) || input.actions.length > 20 || !Number.isInteger(input.timeout_ms) || input.timeout_ms < 250 || input.timeout_ms > 20_000) throw new Error("input_invalid");
  for (const action of input.actions) {
    exactKeys(action, ["action", "selector", "value", "key", "assertion"]);
    if (!["click", "fill", "press"].includes(action.action) || typeof action.selector !== "string" || !action.selector || action.selector.length > 500) throw new Error("input_invalid");
    if (action.action === "fill" && (typeof action.value !== "string" || action.value.length > 2000) || action.action !== "fill" && action.value != null) throw new Error("input_invalid");
    if (action.action === "press" && !KEYS.has(action.key) || action.action !== "press" && action.key != null) throw new Error("input_invalid");
    if (action.assertion != null) {
      exactKeys(action.assertion, ["selector", "text", "value", "visible"]);
      if (typeof action.assertion.selector !== "string" || !action.assertion.selector || action.assertion.selector.length > 500 || !["text", "value", "visible"].some(key => action.assertion[key] != null)) throw new Error("input_invalid");
      if (action.assertion.text != null && (typeof action.assertion.text !== "string" || action.assertion.text.length > 2000) || action.assertion.value != null && (typeof action.assertion.value !== "string" || action.assertion.value.length > 2000) || action.assertion.visible != null && typeof action.assertion.visible !== "boolean") throw new Error("input_invalid");
    }
  }
}

async function main() {
  const parts = [];
  let received = 0;
  for await (const part of process.stdin) {
    received += part.length;
    if (received > MAX_INPUT) throw new Error("input_limit");
    parts.push(part);
  }
  const input = JSON.parse(Buffer.concat(parts).toString("utf8"));
  validate(input);
  timeout = setTimeout(() => {
    expired = true;
    browser?.close().catch(() => {});
    setTimeout(() => process.exit(124), 200).unref();
  }, input.timeout_ms);
  const began = Date.now();
  const remaining = () => Math.max(1, input.timeout_ms - (Date.now() - began));
  browser = await chromium.launch({ headless: true, timeout: remaining(), args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking", "--disable-component-update", "--disable-extensions", "--disable-sync", "--no-first-run"] });
  const context = await browser.newContext({ viewport: input.viewport, deviceScaleFactor: 1, serviceWorkers: "block", acceptDownloads: false, javaScriptEnabled: true, locale: "en-US", timezoneId: "UTC", colorScheme: "light", reducedMotion: "reduce" });
  const blocked = [];
  let attempts = 0;
  const recordAttempt = (url, kind) => {
    attempts += 1;
    if (blocked.length < MAX_NETWORK_ATTEMPTS) blocked.push({ kind, url_sha256: sha(String(url)), disposition: "blocked" });
    if (attempts > MAX_NETWORK_ATTEMPTS) context.close().catch(() => {});
  };
  await context.route("**/*", route => {
    recordAttempt(route.request().url(), route.request().resourceType());
    return route.abort("blockedbyclient").catch(() => {});
  });
  await context.routeWebSocket("**/*", websocket => {
    recordAttempt(websocket.url(), "websocket");
    websocket.close();
  });
  context.on("page", page => page.on("dialog", dialog => dialog.dismiss().catch(() => {})));
  const page = await context.newPage();
  page.setDefaultTimeout(Math.min(2000, remaining()));
  page.setDefaultNavigationTimeout(Math.min(2000, remaining()));
  let pageErrorCount = 0;
  page.on("pageerror", () => { pageErrorCount += 1; });
  page.on("download", download => download.cancel().catch(() => {}));
  page.on("popup", popup => popup.close().catch(() => {}));
  await page.setContent(input.source, { waitUntil: "domcontentloaded", timeout: remaining() });
  const checks = [];
  for (const [index, action] of input.actions.entries()) {
    const result = { index, action: action.action, selector_sha256: sha(action.selector), status: "passed", assertion: action.assertion ? "pending" : "not_requested" };
    try {
      const locator = page.locator(action.selector);
      const actionTimeout = Math.min(2000, remaining());
      if (action.action === "click") await locator.click({ timeout: actionTimeout });
      if (action.action === "fill") await locator.fill(action.value, { timeout: actionTimeout });
      if (action.action === "press") await locator.press(action.key, { timeout: actionTimeout });
      if (action.assertion) {
        const target = page.locator(action.assertion.selector);
        const values = [];
        if (action.assertion.text != null) values.push((await target.textContent({ timeout: Math.min(2000, remaining()) })) === action.assertion.text);
        if (action.assertion.value != null) values.push((await target.inputValue({ timeout: Math.min(2000, remaining()) })) === action.assertion.value);
        if (action.assertion.visible != null) values.push((await target.isVisible()) === action.assertion.visible);
        result.assertion = values.every(Boolean) ? "passed" : "failed";
        if (result.assertion === "failed") result.status = "failed";
      }
    } catch {
      if (expired) throw new Error("render_timeout");
      result.status = "failed";
      result.assertion = action.assertion ? "unavailable" : "not_requested";
    }
    checks.push(result);
  }
  // Capture exactly the declared viewport, without document-height expansion.
  const png = await page.screenshot({ type: "png", fullPage: false, animations: "disabled", timeout: remaining() });
  if (png.length > MAX_PNG || attempts > MAX_NETWORK_ATTEMPTS) throw new Error("output_limit");
  const body = {
    schema_version: "1.0", renderer_version: "native-interface-browser-v1", playwright_version: playwrightVersion,
    browser_version: browser.version(), source_sha256: input.source_sha256, configuration_hash: input.configuration_hash,
    viewport: input.viewport, screenshot_sha256: sha(png), screenshot_base64: png.toString("base64"),
    checks, network_attempts: blocked, network_attempt_count: attempts, page_error_count: Math.min(pageErrorCount, 100),
    limitations: ["Browser appearance and declared action assertions do not establish complete functionality or accessibility.", "External traffic and service workers are blocked; the container has no network or host mounts.", "Network attempt counts cover intercepted requests and WebSockets; other transports are denied by the container network.", "Chromium process sandbox is disabled inside the unprivileged resource-limited container; container isolation is the execution boundary."],
  };
  await browser.close();
  browser = null;
  clearTimeout(timeout);
  process.stdout.write(JSON.stringify(body));
}

try { await main(); }
catch (error) {
  // Playwright's remaining-budget timer can reject before the watchdog's
  // callback runs. An uncaught operation timeout still exhausted this render's
  // deadline; callback ordering must not turn it into a generic render failure.
  // Per-action timeouts are handled inside the action loop above.
  expired = expired || error instanceof errors.TimeoutError;
  clearTimeout(timeout);
  await browser?.close().catch(() => {});
  process.stdout.write(JSON.stringify({ schema_version: "1.0", error: expired ? "render_timeout" : "render_failed" }));
  process.exitCode = expired ? 124 : 1;
}
