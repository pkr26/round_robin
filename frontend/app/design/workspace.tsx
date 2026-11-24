"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import type { components } from "../../../packages/api-client/src/application";
import { API_URL, ApiError, abandonPendingCommand, downloadDesignSource, onAccessChange, pendingCommand, readApi, readDesignSource, reconcileCommand, refreshSession, retryPendingCommand, sendCommand, uploadDesignImage, type MeetingView, type PendingCommand } from "../api-client";

type VisualCapabilities = components["schemas"]["VisualCapabilitiesView"];
type Version = components["schemas"]["DesignVersionView"];
type Design = components["schemas"]["DesignSessionView"];
type VisualPreflight = components["schemas"]["VisualPreflightView"];
const names: Record<string, string> = { openai: "OpenAI", anthropic: "Anthropic Claude", google: "Google Gemini" };
function diagramStarter(width = 1024, height = 1024) {
  const padding = Math.min(50, Math.floor(width / 10));
  const nodeWidth = Math.min(180, width - padding * 2), nodeHeight = Math.min(70, Math.floor(height * .4));
  const top = Math.floor((height - nodeHeight * 2) / 3);
  return JSON.stringify({ width, height, nodes: [
    { id: "node_input", label: "Input", x: padding, y: top, width: nodeWidth, height: nodeHeight },
    { id: "node_result", label: "Result", x: width - padding - nodeWidth, y: height - top - nodeHeight, width: nodeWidth, height: nodeHeight },
  ], edges: [{ source: "node_input", target: "node_result", label: width >= 300 ? "transforms into" : "" }] }, null, 2);
}
const htmlStarter = "<!doctype html>\n<html lang=\"en\"><meta charset=\"utf-8\"><title>Interface draft</title>\n<style>body{font:18px system-ui;padding:40px;background:#f5f7ef}button{padding:12px}</style>\n<main><h1>Interface draft</h1><button id=\"action\" onclick=\"document.querySelector('#result').textContent='Selected'\">Choose this approach</button><p id=\"result\">Waiting</p></main></html>";

export default function DesignWorkspace() {
  const [capabilities, setCapabilities] = useState<VisualCapabilities | null>(null);
  const [meetings, setMeetings] = useState<MeetingView[]>([]);
  const [nextMeetingCursor, setNextMeetingCursor] = useState<string | null>(null);
  const [meetingId, setMeetingId] = useState("");
  const [designs, setDesigns] = useState<Design[]>([]);
  const [design, setDesign] = useState<Design | null>(null);
  const [mode, setMode] = useState<"review_only" | "improve">("review_only");
  const [domain, setDomain] = useState("raster_concept");
  const [purpose, setPurpose] = useState("");
  const [audience, setAudience] = useState("");
  const [requirements, setRequirements] = useState("");
  const [requiredText, setRequiredText] = useState("");
  const [maximumUsd, setMaximumUsd] = useState("0");
  const [recipients, setRecipients] = useState(["openai", "anthropic", "google"]);
  const [width, setWidth] = useState(1024);
  const [height, setHeight] = useState(1024);
  const [visibleVersion, setVisibleVersion] = useState<string | null>(null);
  const [compare, setCompare] = useState(false);
  const [intent, setIntent] = useState("");
  const [source, setSource] = useState(diagramStarter);
  const sourceEditGeneration = useRef(0);
  // Capture hydration authority while selecting the candidate, before its
  // editable DOM commits. A passive effect may start after the first edit.
  const sourceScopeGeneration = useRef(0);
  const [actions, setActions] = useState("[]");
  const [preflight, setPreflight] = useState<VisualPreflight | null>(null);
  const [consent, setConsent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingCommand | null>(null);
  const [now, setNow] = useState(Date.now());
  const blocked = !!busy || !!pending;
  const pendingUploadId = pending?.encoding === "binary" ? /^\/api\/design-sessions\/([^/]+)\/images$/.exec(pending.path)?.[1] : undefined;
  const sizes = capabilities?.supported_sizes ?? [];
  const version = design?.versions.find(item => item.id === visibleVersion) ?? design?.versions.at(-1);
  const latest = design?.versions.at(-1);

  async function act(label: string, callback: () => Promise<void>) {
    setBusy(label); setError(null); setNotice(null);
    try { await callback(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "The action could not be confirmed."); }
    finally { setBusy(null); try { setPending(pendingCommand()); } catch { setError("An earlier action is unconfirmed. Reconcile its receipt before continuing."); } }
  }

  async function loadDesigns(id: string) {
    const value = await readApi<{ items: Design[] }>(`/api/meetings/${id}/design-sessions`);
    setDesigns(value.items);
  }

  useEffect(() => onAccessChange(() => {
    sourceEditGeneration.current += 1;
    setMeetings([]); setNextMeetingCursor(null); setMeetingId(""); setDesigns([]); setDesign(null);
    setPreflight(null); setConsent(false); setSource(""); setActions("[]");
    setPurpose(""); setAudience(""); setRequirements(""); setRequiredText(""); setIntent("");
    setError("Account access changed. Reload to read this account's saved visual work.");
  }), []);

  useEffect(() => {
    void act("Reading visual capabilities", async () => {
      await refreshSession(true);
      setPending(pendingCommand());
      const caps = await readApi<VisualCapabilities>("/api/visual/capabilities");
      setCapabilities(caps);
      const defaultSize = caps.supported_sizes?.includes("1024x1024") ? "1024x1024" : caps.supported_sizes?.[0];
      if (defaultSize) { const [w, h] = defaultSize.split("x").map(Number); setWidth(w); setHeight(h); }
      if (caps.enabled) {
        const result = await readApi<{ items: MeetingView[]; next_cursor: string | null }>("/api/meetings");
        setMeetings(result.items); setNextMeetingCursor(result.next_cursor ?? null);
        if (result.items[0]) { setMeetingId(result.items[0].id); await loadDesigns(result.items[0].id); }
      }
    });
  }, []);
  useEffect(() => {
    const current = design?.versions.at(-1);
    if (!current || !current.source_kind.startsWith("native_")) return;
    let cancelled = false;
    const generation = sourceScopeGeneration.current;
    const suffix = current.source_kind === "native_diagram" ? "json" : "html";
    void readDesignSource(current.id, suffix).then(content => {
      if (!cancelled && sourceEditGeneration.current === generation) setSource(suffix === "json" ? JSON.stringify(JSON.parse(content), null, 2) : content);
    }).catch(() => { if (!cancelled) setError("The saved native source could not be read. Reopen this candidate to retry, or enter the exact source before rendering again."); });
    return () => { cancelled = true; };
  }, [design?.id, design?.versions.at(-1)?.id]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    if (design) setDesigns(items => items.map(item => item.id === design.id && item.revision <= design.revision ? design : item));
  }, [design]);
  useEffect(() => {
    if (!design?.pending_operations.length) return;
    let stopped = false;
    const id = design.id;
    const timer = setInterval(() => { void readApi<Design>(`/api/design-sessions/${id}`).then(value => { if (!stopped) setDesign(current => current?.id === id && current.revision <= value.revision ? value : current); }).catch(() => { if (!stopped) setNotice("Connection interrupted. The last confirmed candidate remains visible; reconnecting does not submit another image job."); }); }, 1200);
    return () => { stopped = true; clearInterval(timer); };
  }, [design?.id, design?.pending_operations.length]);

  function open(value: Design, submittedActions?: string) {
    sourceEditGeneration.current += 1; sourceScopeGeneration.current = sourceEditGeneration.current;
    const latest = value.versions.at(-1);
    setSource(latest?.source_kind.startsWith("native_") ? "" : value.brief.domain === "native_interface" ? htmlStarter : diagramStarter(value.brief.width, value.brief.height));
    const plan = latest?.provenance.interaction_plan;
    setActions(Array.isArray(plan) ? JSON.stringify(plan, null, 2) : submittedActions ?? "[]");
    setDesign(value); setVisibleVersion(latest?.id ?? null); setPreflight(null); setConsent(false); setCompare(false); setNotice(null); setError(null); setIntent("");
  }

  async function reopen(id: string) {
    await act("Reading saved visual brief", async () => {
      const value = await readApi<Design>(`/api/design-sessions/${id}`);
      if (!meetings.some(item => item.id === value.meeting_id)) {
        const savedMeeting = await readApi<MeetingView>(`/api/meetings/${value.meeting_id}`);
        setMeetings(items => items.some(item => item.id === savedMeeting.id) ? items : [...items, savedMeeting]);
      }
      open(value);
      if (meetingId !== value.meeting_id) { setMeetingId(value.meeting_id); await loadDesigns(value.meeting_id); }
    });
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    await act("Saving the frozen visual brief", async () => {
      if (mode === "improve" && domain === "raster_concept" && sizes.length && !sizes.includes(`${width}x${height}`)) {
        throw new ApiError("Choose a configured generation size before freezing this Improve brief.");
      }
      const created = await sendCommand<Design>(`create-design:${meetingId}`, `/api/meetings/${meetingId}/design-sessions`, {
        mode, brief: { purpose, audience, domain, width, height, required_text: requiredText.split("\n").filter(Boolean), locked_requirements: requirements.split("\n").filter(Boolean) }, recipients, maximum_usd: maximumUsd,
      });
      open(created); await loadDesigns(meetingId);
    });
  }

  async function upload(file: File) {
    if (!design) return;
    await act("Validating and saving original pixels", async () => {
      const value = await uploadDesignImage<Design & { upload_status?: string }>(design.id, design.revision, file);
      open(value);
      setNotice(value.upload_status === "ready" ? "Original V1 saved. Metadata was removed. No model review has been claimed." : "The image upload is still unconfirmed; no usable candidate has been claimed.");
    });
  }

  async function prepare(action: VisualPreflight["action"]) {
    if (!design) return;
    await act("Preparing visual preflight", async () => {
      const value = await sendCommand<VisualPreflight>(`design-preflight:${design.id}:${design.revision}:${action}`, `/api/design-sessions/${design.id}/preflight`, { expected_revision: design.revision, action });
      setPreflight(value); setConsent(false);
    });
  }

  async function startReview() {
    if (!design || !preflight || !consent) return;
    await act("Confirming the bounded visual work", async () => {
      const improvement = preflight.action !== "review";
      const value = await sendCommand<Design>(`visual-start:${preflight.id}`, `/api/design-sessions/${design.id}/${improvement ? "improvements" : "reviews"}`, {
        expected_revision: preflight.revision, preflight_id: preflight.id,
        ...(improvement ? { target_version_id: preflight.action === "edit" ? latest?.id : null, intent: intent.trim() || (design.brief.purpose.length <= 2000 ? design.brief.purpose : "Follow the complete frozen visual brief, preserving all required text and locked requirements."), finding_dispositions: {} } : {}),
      });
      setDesign(value); setPreflight(null); setConsent(false); setNotice("Visual work accepted. Only saved results will appear; an interrupted acknowledgement is reconciled by its original receipt.");
    });
  }

  async function select(id: string | null) {
    if (!design) return;
    await act("Recording your candidate choice", async () => {
      const result = await sendCommand<Design>(`visual-select:${design.id}:${design.revision}`, `/api/design-sessions/${design.id}/selection`, { expected_revision: design.revision, version_id: id });
      setDesign(result); setNotice(id ? "Your chosen version is saved. Review coverage remains visible separately." : "Declined all candidates. Earlier versions and review history are preserved.");
    });
  }

  async function renderNative() {
    if (!design) return;
    await act("Rendering the exact native source", async () => {
      const isDiagram = design.brief.domain === "structured_diagram";
      const body = isDiagram ? { expected_revision: design.revision, diagram: JSON.parse(source) } : { expected_revision: design.revision, source, actions: JSON.parse(actions) };
      const result = await sendCommand<Design>(`native-render:${design.id}:${design.revision}`, `/api/design-sessions/${design.id}/${isDiagram ? "native-diagram" : "native-interface"}`, body);
      open(result, isDiagram ? undefined : actions); setNotice("Native source and its exact rendered pixels are saved together. The recorded checks describe their tested scope; image appearance alone does not establish correctness.");
    });
  }

  function candidate(item: Version) {
    return <article className="live-card visual-candidate" key={item.id}><div className="visual-heading"><h2>V{item.version}</h2><span className="live-pill">{item.coverage.complete ? "Two eligible critics reviewed these pixels" : "Independent review incomplete"}</span></div><img src={`${API_URL}/api/design-versions/${encodeURIComponent(item.id)}/image`} alt={`Version ${item.version}: ${design?.brief.purpose ?? "saved visual candidate"}`} /><p>{item.width} × {item.height} pixels · {item.source_kind.replaceAll("_", " ")}</p><details><summary>Exact version and provenance</summary><p>Canonical SHA-256 <code>{item.canonical_hash}</code></p><p>Parent: {item.parent_version_id ?? "Original V1"}</p><p>Generator provenance: {item.coverage.generator_provenance}</p>{!!item.provenance.renderer_version && <p>Renderer: {String(item.provenance.renderer_version)}</p>}{!!item.provenance.checks && <pre>{JSON.stringify(item.provenance.checks, null, 2)}</pre>}</details>{item.coverage.limitations.map(line => <p key={line}>{line}</p>)}{item.regression_review && item.regression_review.dispositions.length > 0 && <section><h3>Checks against prior findings and locked requirements</h3><p>{item.regression_review.complete ? "Every assigned check has a recorded response." : `${item.regression_review.pending_target_ids.length} assigned checks still need a response.`} {item.regression_review.unresolved_blockers.length} unresolved blockers.</p><ul>{item.regression_review.dispositions.map(check => <li key={check.id}><strong>{check.description}</strong> · {check.status}{check.checks.map(judgment => <p key={judgment.provider}>{names[judgment.provider]}: {judgment.result} — {judgment.reason}</p>)}</li>)}</ul></section>}<div className="live-actions"><button type="button" className="live-secondary" disabled={blocked} onClick={() => void select(item.id)}>Keep V{item.version}{item.coverage.complete ? "" : " with incomplete review"}</button>{item.source_kind.startsWith("native_") && <button type="button" className="live-link" disabled={blocked} onClick={() => void act("Downloading exact native source", () => downloadDesignSource(item.id, item.version, item.source_kind === "native_diagram" ? "svg" : "html"))}>Download editable source</button>}</div></article>;
  }

  return <div className="live-app visual-app"><div className="visual-shell"><header className="visual-heading"><a className="live-brand" href="/live">◉ round robin</a><a href="/live">Return to saved comparisons</a></header><main><span className="live-eyebrow">Optional visual workspace</span><h1>Keep the pixels, the critique and the choice together.</h1><p className="visual-intro">Review an original, inspect version-bound findings and preserve every candidate. Two distinct eligible critics are required for complete review. Your selection remains your choice.</p>
      {error && <p className="live-error" role="alert">{error}</p>}{notice && <p className="live-notice" role="status">{notice}</p>}{busy && <p role="status">{busy}…</p>}
      {pending && <section className="live-alert"><strong>Visual action not confirmed</strong><p>Its original noncontent receipt is retained. Private source text and image bytes are kept only in memory.</p><button type="button" className="live-secondary" disabled={!!busy} onClick={() => void act("Reconciling visual action", async () => { const result = await reconcileCommand(); if (!result.confirmed) { setNotice("The action is not yet confirmed. Retry its original payload; reselect the exact original image if this tab was reloaded."); return; } if (result.result && typeof result.result === "object" && "versions" in result.result) open(result.result as Design); })}>Reconcile original action</button><button type="button" className="live-link" disabled={!!busy} onClick={() => void act("Retrying the same visual action", async () => { const result = await retryPendingCommand(); if (result && typeof result === "object" && "versions" in result) open(result as Design); })}>Retry the same action</button><button type="button" className="live-link" disabled={!!busy} onClick={() => void act("Resolving the original visual action", async () => { const outcome = await abandonPendingCommand(); if (outcome.result && typeof outcome.result === "object" && "versions" in outcome.result) open(outcome.result as Design); if (meetingId) await loadDesigns(meetingId); setNotice(outcome.status === "abandoned" ? "The original visual action was safely abandoned. It cannot execute later; you can enter your inputs again." : "The original action was already saved. Its server result has been recovered."); })}>Abandon unconfirmed action</button>{pendingUploadId && <button type="button" className="live-secondary" disabled={!!busy} onClick={() => void reopen(pendingUploadId)}>Open original upload to reselect image</button>}</section>}
      {capabilities && !capabilities.enabled && <section className="live-card"><h2>Visual work is disabled in this deployment.</h2><p>The raster and native workflows have separate configuration, demand and access gates. The core comparison workspace is available independently.</p><a href="/live">Open saved comparisons</a></section>}
      {capabilities?.enabled && <><section className="live-card"><div className="visual-heading"><h2>Your visual briefs</h2><button type="button" className="live-secondary" disabled={blocked} onClick={() => { setDesign(null); setPreflight(null); }}>New visual brief</button></div><label>Saved comparison<select value={meetingId} disabled={!!busy} onChange={event => { const id = event.target.value; setMeetingId(id); setDesign(null); void act("Reading visual history", () => loadDesigns(id)); }}>{meetings.map(item => <option value={item.id} key={item.id}>{item.title}</option>)}</select></label>{meetings.length === 0 && <p>Save a comparison in the core workspace first.</p>}<div className="live-actions">{designs.map(item => <button type="button" key={item.id} className="live-secondary" disabled={!!busy} onClick={() => void reopen(item.id)}>{item.brief.purpose.slice(0, 80)} · {item.versions.length} versions</button>)}</div>{nextMeetingCursor && <button type="button" className="live-link" disabled={!!busy} onClick={() => void act("Reading more saved comparisons", async () => { const more = await readApi<{ items: MeetingView[]; next_cursor: string | null }>(`/api/meetings?cursor=${encodeURIComponent(nextMeetingCursor)}`); setMeetings(items => [...items, ...more.items.filter(item => !items.some(saved => saved.id === item.id))]); setNextMeetingCursor(more.next_cursor); })}>Load more saved comparisons</button>}</section>
      {!design && meetings.length > 0 && <form onSubmit={event => void create(event)} className="live-card live-editor"><h2>Freeze the purpose and scope.</h2><div className="live-form-grid"><label>Mode<select value={mode} disabled={blocked} onChange={event => setMode(event.target.value as typeof mode)}><option value="review_only">Review only</option><option value="improve">Improve within limits</option></select></label><label>Output domain<select value={domain} disabled={blocked} onChange={event => { setDomain(event.target.value); setSource(event.target.value === "native_interface" ? htmlStarter : diagramStarter(width, height)); }}><option value="raster_concept">Raster concept</option><option value="structured_diagram" disabled={!capabilities.native_diagram_enabled}>Structured diagram</option><option value="native_interface" disabled={!capabilities.native_interface_enabled}>Native interface</option></select></label><label className="live-span">Purpose<textarea required maxLength={4000} value={purpose} onChange={event => setPurpose(event.target.value)} disabled={blocked} /></label><label>Audience<input required maxLength={500} value={audience} onChange={event => setAudience(event.target.value)} disabled={blocked} /></label><label>Maximum provider allowance (USD)<input inputMode="decimal" required pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?" value={maximumUsd} onChange={event => setMaximumUsd(event.target.value)} disabled={blocked || !capabilities.paid_access_enabled} /></label>{mode === "improve" && domain === "raster_concept" && sizes.length > 0 && <label>Generation size<select value={sizes.includes(`${width}x${height}`) ? `${width}x${height}` : ""} disabled={blocked} onChange={event => { const [w, h] = event.target.value.split("x").map(Number); setWidth(w); setHeight(h); }}><option value="" disabled>Choose supported dimensions</option>{sizes.map(size => <option key={size} value={size}>{size.replace("x", " × ")}</option>)}</select></label>}<label>Width (pixels)<input type="number" min={domain !== "raster_concept" ? 100 : 1} max={domain !== "raster_concept" ? 4000 : 8192} value={width} onChange={event => setWidth(Number(event.target.value))} disabled={blocked} /></label><label>Height (pixels)<input type="number" min={domain !== "raster_concept" ? 100 : 1} max={domain !== "raster_concept" ? 4000 : 8192} value={height} onChange={event => setHeight(Number(event.target.value))} disabled={blocked} /></label><label className="live-span">Protected requirements — one per line<textarea value={requirements} maxLength={10000} onChange={event => setRequirements(event.target.value)} disabled={blocked} /></label><label className="live-span">Required literal text — one item per line<textarea value={requiredText} maxLength={10000} onChange={event => setRequiredText(event.target.value)} disabled={blocked} /></label></div><fieldset className="visual-recipients"><legend>Approved image recipients</legend>{["openai", "anthropic", "google"].map(provider => <label key={provider}><input type="checkbox" checked={recipients.includes(provider)} disabled={blocked} onChange={event => setRecipients(old => event.target.checked ? [...old, provider] : old.filter(item => item !== provider))} />{names[provider]}</label>)}</fieldset><p>{capabilities.paid_access_enabled ? `Operator configuration permits at most USD ${capabilities.maximum_usd}. You must explicitly set the allowance and confirm a preflight.` : "No funded image/vision configuration is available. Uploading, inspecting and selecting an original do not call a model."}</p><button type="submit" className="live-primary" disabled={blocked}>Save frozen visual brief</button></form>}
      {design && <><section className="live-card"><span className="live-eyebrow">{design.mode.replaceAll("_", " ")} · {design.status}</span><h2>{design.brief.purpose}</h2><p>Audience: {design.brief.audience}. Approved recipients: {design.recipients.map(provider => names[provider] ?? provider).join(", ") || "None"}.</p>{design.brief.locked_requirements.length > 0 && <ul>{design.brief.locked_requirements.map(line => <li key={line}>{line}</li>)}</ul>}<p>{design.state.submission_count}/4 image submissions · {design.state.request_count}/24 inference calls · {design.state.version_count}/3 versions · original allowance USD {design.state.maximum_usd}</p>{design.selection_recorded && <p className="live-notice">{design.selected_version_id ? `Selected V${design.versions.find(item => item.id === design.selected_version_id)?.version}.` : "All candidates declined."} Review coverage is recorded separately.</p>}{design.pending_operations.map(item => <p role="status" key={item.id}>{item.kind}: {item.status}</p>)}{design.operation_failures.map(item => <p className="live-alert" key={item.id}>{item.kind}: {item.error.replaceAll("_", " ")}. Any unknown exposure remains counted.</p>)}{design.status === "active" && design.versions.length === 0 && design.brief.domain === "raster_concept" && <label>Upload original PNG or JPEG<input type="file" accept="image/png,image/jpeg,.png,.jpg,.jpeg" disabled={!!busy || (!!pending && !pending.intent.startsWith(`designimage:${design.id}:`))} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void upload(file); }} /></label>}{design.brief.domain !== "raster_concept" && design.status === "active" && design.versions.length < 3 && <details><summary>Render the exact native source</summary><p>Editable source and rendered pixels are stored together. Executable interfaces run only in the configured isolated container.</p><label>{design.brief.domain === "structured_diagram" ? "Structured diagram JSON" : "Native HTML source"}<textarea className="visual-source" rows={10} value={source} onChange={event => { sourceEditGeneration.current += 1; setSource(event.target.value); }} disabled={blocked} /></label>{design.brief.domain === "native_interface" && <label>Declared browser actions and assertions (JSON)<textarea rows={5} value={actions} onChange={event => setActions(event.target.value)} disabled={blocked} /></label>}<button type="button" className="live-secondary" disabled={blocked || !source.trim()} onClick={() => void renderNative()}>Render a saved native candidate</button></details>}<div className="live-actions">{latest && <button type="button" className="live-secondary" disabled={blocked || design.status !== "active" || latest.coverage.complete || design.pending_operations.length > 0} onClick={() => void prepare("review")}>Review visual preflight</button>}{design.mode === "improve" && design.brief.domain === "raster_concept" && !latest && <button type="button" className="live-secondary" disabled={blocked || design.status !== "active"} onClick={() => void prepare("generate")}>Prepare original generation</button>}<button type="button" className="live-link" disabled={blocked || design.selection_recorded && design.selected_version_id === null} onClick={() => void select(null)}>Decline all candidates</button><button type="button" className="live-link" disabled={blocked || design.status !== "active"} onClick={() => void act("Stop not confirmed", async () => { const value = await sendCommand<Design>(`visual-cancel:${design.id}:${design.revision}`, `/api/design-sessions/${design.id}/cancel`, { expected_revision: design.revision }); setDesign(value); setPreflight(null); setNotice("Visual cancellation acknowledged; already submitted remote work may still finish or bill."); })}>Stop visual work</button></div></section>
      {preflight && <section className="live-card live-preflight"><h2>Confirm the exact visual work.</h2><p>Action: {preflight.action}. Frozen mode: {preflight.mode}. Expires {new Date(preflight.expires_at).toLocaleString()}.</p><p>Recipients: {preflight.recipients.map(provider => `${names[provider] ?? provider}${preflight.models?.[provider] ? ` (${preflight.models[provider]})` : ""}`).join(", ")}.</p><dl>{Object.entries(preflight.limits).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{String(value)}</dd></div>)}</dl>{preflight.blockers.map(line => <p className="live-alert" key={line}>{line}</p>)}{preflight.action !== "review" && <label>Specific generation or edit intent<textarea value={intent} maxLength={2000} onChange={event => setIntent(event.target.value)} /></label>}<label className="live-consent"><input type="checkbox" checked={consent} disabled={blocked} onChange={event => setConsent(event.target.checked)} />I approve this exact input scope, action and original allowance.</label><button type="button" className="live-primary" disabled={blocked || !consent || preflight.blockers.length > 0 || now >= new Date(preflight.expires_at).getTime()} onClick={() => void startReview()}>Start bounded visual {preflight.action}</button></section>}
      {design.versions.length > 0 && <><nav className="live-actions" aria-label="Immutable design versions">{design.versions.map(item => <button type="button" key={item.id} aria-pressed={version?.id === item.id} className="live-secondary" onClick={() => setVisibleVersion(item.id)}>Inspect V{item.version}</button>)}{design.versions.length > 1 && <label className="live-consent"><input type="checkbox" checked={compare} onChange={event => setCompare(event.target.checked)} />Show a linear comparison</label>}</nav><div className={compare ? "visual-compare" : ""}>{compare ? design.versions.map(candidate) : version && candidate(version)}</div>{version && <section className="live-card"><h2>Findings tied to V{version.version}</h2>{version.reviews.length === 0 && <p>No actual critic review is saved for these pixels. This candidate remains unreviewed.</p>}{[...version.reviews, ...(version.review_supplements ?? [])].map((review, reviewIndex) => <article key={`${review.provider}:${reviewIndex}`} className="live-evidence"><h3>{names[review.provider] ?? review.provider}</h3>{review.findings.map(finding => <div className="visual-finding" key={finding.id}><strong>{finding.description}</strong><p>{finding.category.replaceAll("_", " ")} · {finding.severity} · {finding.disposition} · supplied view {finding.view_index + 1}</p>{finding.region && <p>Region: {Math.round(finding.region.x * 100)}% from left, {Math.round(finding.region.y * 100)}% from top; {Math.round(finding.region.width * 100)}% wide × {Math.round(finding.region.height * 100)}% high.</p>}{finding.previous_finding_id && <p>Rechecks prior finding {finding.previous_finding_id}.</p>}</div>)}{review.limitations.map(line => <p key={line}>{line}</p>)}</article>)}</section>}{design.mode === "improve" && design.brief.domain === "raster_concept" && latest && design.versions.length < 3 && <section className="live-card"><h2>Make a targeted next candidate.</h2><p>Previous versions are preserved. Required next-version review is protected before another image submission.</p><button type="button" className="live-secondary" disabled={blocked || design.status !== "active" || !latest.coverage.complete || latest.regression_review?.complete === false || design.pending_operations.length > 0} onClick={() => void prepare("edit")}>Prepare next-version edit</button>{!latest.coverage.complete && <p>Complete actual independent review of the latest version before automatic editing.</p>}</section>}</>}
      </>}
      {capabilities?.limitations.map(line => <p className="live-muted" key={line}>{line}</p>)}
      </>}
    </main></div></div>;
}
