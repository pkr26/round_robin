import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import type { components } from "../../packages/api-client/src/application";
import applicationSchema from "../../contracts/application.openapi.json";

export const API_URL = process.env.NEXT_PUBLIC_ROUND_ROBIN_API_URL ?? "http://127.0.0.1:8000";
const PENDING_KEY = "round-robin:pending-local-command:v1";

export type RunView = components["schemas"]["RunView"];
export type MeetingView = components["schemas"]["MeetingView"];
export type PreflightView = components["schemas"]["PreflightView"];
export type EventView = components["schemas"]["EventView"];
export type Snapshot = components["schemas"]["Snapshot"];
export type OutcomeRecord = components["schemas"]["OutcomeRecord"];
export type DecisionReport = components["schemas"]["RuntimeDecisionReport"];
export type EvidencePassage = components["schemas"]["StoredEvidence"];
export type DraftBrief = components["schemas"]["DraftBrief-Output"];
const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
ajv.addSchema({ ...applicationSchema, $id: "urn:round-robin:application" });
const validateSnapshot = ajv.compile<Snapshot>({ $ref: "urn:round-robin:application#/components/schemas/Snapshot" });
const validateReport = ajv.compile<DecisionReport>({ $ref: "urn:round-robin:application#/components/schemas/RuntimeDecisionReport" });
const visualValidators = Object.fromEntries(["DesignSessionView", "VisualUploadView", "AcceptedDesignView", "NativeRenderView", "VisualCapabilitiesView", "VisualPreflightView", "DesignPage"].map(name => [name, ajv.compile({ $ref: `urn:round-robin:application#/components/schemas/${name}` })]));
export type UploadView = { id: string; filename: string; status: string; byte_size: number; sha256: string | null; recipients: string[] };
export type PendingCommand = {
  id: string; intent: string; path: string; method: string;
  bodyHash: string; createdAt: string; safeBody?: Record<string, unknown>;
  encoding?: "json" | "text" | "binary";
  ownerId?: string;
};

export type SessionInfo = { owner_id: string; profile: string; csrf_token: string | null; expires_at?: string };
let sessionInfo: SessionInfo | null = null;
let sessionBootstrap: Promise<SessionInfo> | null = null;
let sessionGeneration = 0;
let accessGeneration = 0;
const ACCESS_EVENT = "round-robin:access-changed";

export function onAccessChange(callback: () => void): () => void {
  window.addEventListener(ACCESS_EVENT, callback);
  return () => window.removeEventListener(ACCESS_EVENT, callback);
}

function clearAccess() {
  accessGeneration += 1;
  sessionInfo = null;
  privateBodies.clear();
  if (typeof window !== "undefined") window.dispatchEvent(new Event(ACCESS_EVENT));
}

export async function refreshSession(force = false): Promise<SessionInfo> {
  if (sessionInfo && !force) return sessionInfo;
  if (sessionBootstrap && !force) return sessionBootstrap;
  const generation = ++sessionGeneration;
  const bootstrap = (async () => {
    let response: Response;
    try { response = await fetch(`${API_URL}/auth/session`, { credentials: "include", cache: "no-store" }); }
    catch { throw new ApiError("The service is unavailable. Your last confirmed state is still shown."); }
    if (generation !== sessionGeneration) throw new ApiError("Account access changed while the session was being read. Refresh access before continuing.");
    if (!response.ok) { if (response.status === 401 || response.status === 403) clearAccess(); throw new ApiError("Sign in to access this workspace. Previous actions are not replayed when you sign in.", response.status); }
    const value = await response.json() as SessionInfo;
    if (generation !== sessionGeneration) throw new ApiError("Account access changed while the session was being read. Refresh access before continuing.");
    if (!value || typeof value.owner_id !== "string" || !value.owner_id || !["development", "invited_hosted"].includes(value.profile) || value.csrf_token !== null && typeof value.csrf_token !== "string") throw new ApiError("The account session uses an unsupported contract.");
    const changedOwner = !!sessionInfo && sessionInfo.owner_id !== value.owner_id;
    if (changedOwner) clearAccess();
    sessionInfo = value;
    if (changedOwner) throw new ApiError("The signed-in account changed. Reload this account's workspace before submitting an action.");
    return value;
  })();
  sessionBootstrap = bootstrap;
  try { return await bootstrap; } finally { if (sessionBootstrap === bootstrap) sessionBootstrap = null; }
}

export async function signOut(): Promise<void> {
  const identity = await refreshSession();
  const response = await fetch(`${API_URL}/auth/logout`, { method: "POST", credentials: "include", redirect: "manual", headers: identity.csrf_token ? { "X-CSRF-Token": identity.csrf_token } : {} });
  if (response.type !== "opaqueredirect" && ![200, 204, 303].includes(response.status)) throw new ApiError("Sign-out has not been confirmed. Refresh access before trying again.", response.status);
  sessionGeneration += 1;
  clearAccess();
}

export class ApiError extends Error {
  constructor(message: string, public status = 0, public uncertain = false, public commandOutcome?: string, public code?: string) { super(message); }
}

function errorDetail(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(errorDetail).filter(Boolean).join("; ");
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    return errorDetail(item.message ?? item.detail ?? item.msg ?? item.error ?? "The request could not be accepted.");
  }
  return "The request could not be accepted.";
}

export async function readApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  const mutation = options.method !== undefined && options.method !== "GET" && options.method !== "HEAD";
  const identity = mutation ? await refreshSession() : null;
  const generation = accessGeneration;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...options, cache: "no-store", credentials: "include",
      headers: { "Content-Type": "application/json", ...(identity?.csrf_token ? { "X-CSRF-Token": identity.csrf_token } : {}), ...options.headers },
    });
  } catch {
    throw new ApiError("The service is unavailable. Your last confirmed state is still shown.", 0, options.method !== undefined && options.method !== "GET");
  }
  const result = await response.json().catch(() => null);
  if (generation !== accessGeneration) throw new ApiError("Account access changed while saved state was being read. Reload this account's workspace before continuing.", 0, mutation);
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) { sessionGeneration += 1; clearAccess(); }
    const detail = result?.error ?? result?.detail;
    throw new ApiError(errorDetail(result), response.status, response.status >= 500, detail?.command_outcome, detail?.code);
  }
  if (result === null) throw new ApiError("The service returned an unreadable response. Reconcile the action before continuing.", response.status, options.method !== undefined && options.method !== "GET");
  if ((/\/api\/runs\/[^/]+\/snapshot$/.test(path) && !validateSnapshot(result)) ||
      (/\/api\/runs\/[^/]+\/reports\/\d+$/.test(path) && !validateReport(result))) {
    throw new ApiError("This saved result uses an unsupported contract. Reload after updating the local application; saved data has not been changed.");
  }
  let visualContract: string | undefined;
  if (path === "/api/visual/capabilities") visualContract = "VisualCapabilitiesView";
  else if (/^\/api\/meetings\/[^/]+\/design-sessions$/.test(path)) visualContract = mutation ? "DesignSessionView" : "DesignPage";
  else if (/^\/api\/design-sessions\/[^/]+(?:\/(?:cancel|selection))?$/.test(path)) visualContract = "DesignSessionView";
  else if (/^\/api\/design-sessions\/[^/]+\/images$/.test(path)) visualContract = "VisualUploadView";
  else if (/^\/api\/design-sessions\/[^/]+\/(?:reviews|improvements)$/.test(path)) visualContract = "AcceptedDesignView";
  else if (/^\/api\/design-sessions\/[^/]+\/native-(?:diagram|interface)$/.test(path)) visualContract = "NativeRenderView";
  else if (/^\/api\/design-sessions\/[^/]+\/preflight$/.test(path)) visualContract = "VisualPreflightView";
  if (visualContract && !visualValidators[visualContract](result)) throw new ApiError("The service response uses an unsupported visual contract. Reconcile any submitted action before continuing.", 0, mutation);
  return result as T;
}

const privateBodies = new Map<string, { body: Record<string, unknown>; binary?: ArrayBuffer }>();
let commandInFlight = false;

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
  return JSON.stringify(value);
}

async function bodyFingerprint(body: Record<string, unknown>): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(body)));
  return [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, "0")).join("");
}

function safeReplayBody(path: string, body: Record<string, unknown>): Record<string, unknown> | undefined {
  let allowed: string[] | undefined;
  if (/^\/api\/meetings\/[^/]+\/runs$/.test(path)) allowed = ["preflight_id", "expected_draft_revision", "replace_parent_run_version"];
  else if (/^\/api\/meetings\/[^/]+\/preflight$/.test(path)) allowed = ["expected_draft_revision", "mode"];
  else if (/^\/api\/runs\/[^/]+\/exports$/.test(path)) allowed = ["report_id", "format"];
  else if (/^\/api\/runs\/[^/]+\/cancel$/.test(path)) allowed = ["expected_run_version"];
  else if (/^\/api\/runs\/[^/]+\/retries$/.test(path)) allowed = ["expected_run_version", "stage"];
  else if (/^\/api\/design-sessions\/[^/]+\/selection$/.test(path)) allowed = ["expected_revision", "version_id"];
  else if (/^\/api\/design-sessions\/[^/]+\/images$/.test(path)) allowed = ["expected_revision", "sha256", "content_type"];
  if (!allowed || Object.keys(body).some(key => !allowed!.includes(key))) return undefined;
  return structuredClone(body);
}

export function pendingCommand(): PendingCommand | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingCommand & { body?: Record<string, unknown> };
    if (!parsed.id || !parsed.intent || !parsed.path.startsWith("/api/")) throw new Error();
    // Older development receipts held full payloads. Scrub them immediately,
    // keeping only the UUID so their committed outcome can still be reconciled.
    if (parsed.body) {
      privateBodies.set(parsed.id, { body: parsed.body });
      const clean: PendingCommand = { id: parsed.id, intent: parsed.intent, path: parsed.path,
        method: parsed.method, createdAt: parsed.createdAt, encoding: parsed.encoding,
        bodyHash: "legacy_unverified", safeBody: safeReplayBody(parsed.path, parsed.body) };
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(clean));
      return clean;
    }
    if (!parsed.bodyHash) throw new Error();
    return parsed;
  } catch {
    throw new ApiError("The pending action receipt cannot be read. Keep this tab open and reconcile the saved action before submitting another.", 0, true);
  }
}

function clearReceipt(command: PendingCommand) {
  sessionStorage.removeItem(PENDING_KEY);
  privateBodies.delete(command.id);
}

export async function sendCommand<T>(intent: string, path: string, body: Record<string, unknown>, method = "POST", encoding: "json" | "text" | "binary" = "json", binary?: ArrayBuffer): Promise<T> {
  if (commandInFlight) throw new ApiError("An action is already being sent. Wait for its acknowledgement before submitting another.");
  commandInFlight = true;
  let command: PendingCommand | undefined;
  try {
    const identity = await refreshSession(true);
    const generation = accessGeneration;
    const retained = pendingCommand();
    if (retained && retained.intent !== intent) throw new ApiError("Another action is not yet confirmed. Reconcile it before making a new change.", 0, true);
    if (retained?.ownerId && retained.ownerId !== identity.owner_id) throw new ApiError("This unconfirmed action belongs to a different signed-in account. Sign in to its original account to reconcile it; it cannot be replayed here.", 0, true);
    const bodyHash = await bodyFingerprint(body);
    if (generation !== accessGeneration) throw new ApiError("Account access changed while the action was being prepared. Reload before submitting private inputs.");
    if (retained && retained.bodyHash !== bodyHash) throw new ApiError("These inputs differ from the unconfirmed action. Its original UUID is retained; reconcile the original action before changing its input.", 0, true);
    command = retained ?? { id: crypto.randomUUID(), intent, path, method, bodyHash,
      safeBody: safeReplayBody(path, body), createdAt: new Date().toISOString(), encoding, ownerId: identity.owner_id };
    const frozen = privateBodies.get(command.id) ?? { body: structuredClone(body), binary };
    if (binary) frozen.binary = binary;
    if (command.encoding === "binary" && !frozen.binary) throw new ApiError("Reselect the exact original image before retrying this unconfirmed upload. Only its noncontent receipt was retained.", 0, true);
    privateBodies.set(command.id, frozen);
    try { sessionStorage.setItem(PENDING_KEY, JSON.stringify(command)); }
    catch { throw new ApiError("This browser cannot preserve an action receipt. Enable session storage before saving or starting a review."); }
    const payload = frozen.body;
    const options: RequestInit = command.encoding === "text" ? {
      body: String(payload.text), headers: { "Content-Type": String(payload.content_type),
        "X-Command-Id": command.id, "X-Expected-Revision": String(payload.expected_revision),
        "X-Filename": String(payload.filename), "X-Content-Sha256": String(payload.sha256) },
    } : command.encoding === "binary" ? {
      body: frozen.binary, headers: { "Content-Type": String(payload.content_type),
        "X-Command-Id": command.id, "X-Expected-Revision": String(payload.expected_revision), "X-Content-Sha256": String(payload.sha256) },
    } : { body: JSON.stringify({ ...payload, command_id: command.id }) };
    const result = await readApi<T>(command.path, { ...options, method: command.method });
    clearReceipt(command);
    return result;
  } catch (error) {
    // A definitive rejection of the original action is safe to retire on its
    // first send or any retry. A UUID conflict can refer to another committed
    // action and still needs reconciliation; a status code alone is not proof.
    if (command && error instanceof ApiError && error.status >= 400 && error.status < 500 && error.commandOutcome === "not_committed" && error.code !== "command_conflict") clearReceipt(command);
    throw error;
  } finally { commandInFlight = false; }
}

export async function reconcileCommand(): Promise<{ confirmed: boolean; result?: unknown }> {
  const command = pendingCommand();
  if (!command) return { confirmed: true };
  const identity = await refreshSession(true);
  if (command.ownerId && command.ownerId !== identity.owner_id) throw new ApiError("Sign in to the original account to reconcile this action.", 0, true);
  try {
    const receipt = await readApi<{ outcome: string; result: unknown }>(`/api/commands/${command.id}`);
    if (receipt.outcome === "committed" || receipt.outcome === "gone" || receipt.outcome === "abandoned") {
      clearReceipt(command);
      return { confirmed: true, result: receipt.result };
    }
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
  }
  return { confirmed: false };
}

export async function retryPendingCommand(): Promise<unknown> {
  const command = pendingCommand();
  if (!command) return undefined;
  const payload = privateBodies.get(command.id) ?? (command.safeBody ? { body: command.safeBody } : undefined);
  if (!payload) throw new ApiError("Private inputs were kept only in memory and are unavailable after reload. Reconcile the original action, or abandon it safely before entering your inputs again.", 0, true);
  return sendCommand(command.intent, command.path, payload.body, command.method, command.encoding, "binary" in payload ? payload.binary : undefined);
}

export async function abandonPendingCommand(): Promise<{ status: "abandoned" | "committed" | "gone"; result?: unknown }> {
  if (commandInFlight) throw new ApiError("An action is already being sent. Wait for its acknowledgement before abandoning it.");
  commandInFlight = true;
  try {
    const command = pendingCommand();
    if (!command) return { status: "abandoned" };
    const identity = await refreshSession(true);
    if (command.ownerId && command.ownerId !== identity.owner_id) throw new ApiError("Sign in to the original account to abandon this action.", 0, true);
    const result = await readApi<{ status: string; result: unknown }>(`/api/commands/${encodeURIComponent(command.id)}/abandon`, { method: "POST" });
    if (!["abandoned", "committed", "gone"].includes(result.status) || !result.result || typeof result.result !== "object" || Array.isArray(result.result)) {
      throw new ApiError("Abandonment could not be confirmed. The original action receipt is retained for reconciliation.", 0, true);
    }
    // The server serializes this fence with the original command. Clearing an
    // absent GET receipt alone would allow a delayed original to execute later.
    clearReceipt(command);
    return { status: result.status as "abandoned" | "committed" | "gone", result: result.result };
  } finally { commandInFlight = false; }
}

export async function uploadTextFile(meetingId: string, revision: number, file: File): Promise<UploadView> {
  if (!/\.(txt|md)$/i.test(file.name) || file.size > 1024 * 1024 || file.size === 0) throw new ApiError("Choose a nonempty UTF-8 .txt or .md file no larger than one MiB.");
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()).replace(/\r\n?/g, "\n"); }
  catch { throw new ApiError("This file is not valid UTF-8 text."); }
  if (!text.trim() || text.includes("\0")) throw new ApiError("Choose readable text without binary content.");
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  const sha256 = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, "0")).join("");
  return sendCommand(`upload:${meetingId}:${revision}:${sha256}`, `/api/meetings/${meetingId}/uploads`, {
    text, sha256, expected_revision: revision, filename: file.name.replace(/[^a-zA-Z0-9_. -]/g, "_").slice(0, 200),
    content_type: /\.md$/i.test(file.name) ? "text/markdown" : "text/plain",
  }, "POST", "text");
}

export async function uploadDesignImage<T>(sessionId: string, revision: number, file: File): Promise<T> {
  const media = file.type || (/\.jpe?g$/i.test(file.name) ? "image/jpeg" : /\.png$/i.test(file.name) ? "image/png" : "");
  if (!["image/png", "image/jpeg"].includes(media) || file.size === 0 || file.size > 10 * 1024 * 1024) throw new ApiError("Choose one PNG or JPEG image no larger than ten MiB.");
  const binary = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", binary);
  const sha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
  const path = `/api/design-sessions/${sessionId}/images`;
  const retained = pendingCommand();
  // Only noncontent metadata survives reload. Reuse its original revision so
  // selecting exact bytes retries the original payload even after a new read.
  const originalRevision = retained?.path === path && retained.encoding === "binary" && typeof retained.safeBody?.expected_revision === "number" ? retained.safeBody.expected_revision : revision;
  return sendCommand<T>(`designimage:${sessionId}:${sha256}`, path, { expected_revision: originalRevision, sha256, content_type: media }, "POST", "binary", binary);
}

export async function downloadDesignSource(versionId: string, version: number, format: "svg" | "html"): Promise<void> {
  const generation = accessGeneration;
  const response = await fetch(`${API_URL}/api/design-versions/${encodeURIComponent(versionId)}/source.${format}`, { credentials: "include", cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) { sessionGeneration += 1; clearAccess(); }
    throw new ApiError("This saved candidate's editable source is unavailable. Its image and history remain available.", response.status);
  }
  const blob = await response.blob();
  if (generation !== accessGeneration) throw new ApiError("Account access changed while the private source was downloading. Reload before trying again.");
  if (blob.size > 1024 * 1024) throw new ApiError("The native source exceeded the expected download bound.");
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = `round-robin-native-v${version}.${format}`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function downloadSavedExport(runId: string, reportId: string, version: number, format: "json" | "markdown" | "pdf"): Promise<void> {
  const artifact = await sendCommand<{ id: string; status: string; report_id: string; report_version: number; download_url: string; error: string | null }>(
    `export:${runId}:${reportId}:${format}`, `/api/runs/${encodeURIComponent(runId)}/exports`, { report_id: reportId, format },
  );
  if (artifact.status !== "ready") throw new ApiError(artifact.error === "pdf_font_unsupported"
    ? "This report includes characters the PDF font cannot display. Download JSON or Markdown to preserve the exact text."
    : artifact.error ?? "The saved report is available, but its export could not be created. Retry the export.");
  if (artifact.report_id !== reportId || artifact.report_version !== version) throw new ApiError("The export does not match the saved report version. Refresh the report before downloading.");
  const downloadPath = `/api/exports/${encodeURIComponent(artifact.id)}/download`;
  const generation = accessGeneration;
  const response = await fetch(`${API_URL}${downloadPath}`, { credentials: "include", cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) { sessionGeneration += 1; clearAccess(); }
    const detail = await response.json().catch(() => null);
    throw new ApiError(errorDetail(detail), response.status);
  }
  const blob = await response.blob();
  if (generation !== accessGeneration) throw new ApiError("Account access changed while the private export was downloading. Reload before trying again.");
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = `round-robin-${runId}-v${version}.${format === "markdown" ? "md" : format}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

export async function readDesignSource(versionId: string, format: "json" | "html"): Promise<string> {
  const generation = accessGeneration;
  const response = await fetch(`${API_URL}/api/design-versions/${encodeURIComponent(versionId)}/source.${format}`, { credentials: "include", cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) { sessionGeneration += 1; clearAccess(); }
    throw new ApiError("The saved native source is unavailable.", response.status);
  }
  const blob = await response.blob();
  if (generation !== accessGeneration) throw new ApiError("Account access changed while the native source was being read.");
  if (blob.size > (format === "html" ? 128 * 1024 : 1024 * 1024)) throw new ApiError("The native source exceeded its saved byte bound.");
  const content = await blob.text();
  if (generation !== accessGeneration) throw new ApiError("Account access changed while the native source was being read.");
  return content;
}
