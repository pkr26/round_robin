"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import type { Calculation } from "./contracts";
import { validateDraft } from "./contracts";
import {
  API_URL, ApiError, abandonPendingCommand, downloadSavedExport, pendingCommand, readApi, reconcileCommand,
  refreshSession, retryPendingCommand, sendCommand, signOut, uploadTextFile, onAccessChange, type MeetingView, type PendingCommand,
  type OutcomeRecord, type PreflightView, type RunView, type Snapshot, type UploadView, type DraftBrief, type DecisionReport, type EvidencePassage,
} from "./api-client";

type Tab = "overview" | "evidence" | "activity";
type ParentItem = { kind: "evidence" | "claim"; id: string; version: number; sha256: string };
type ParentCandidate = ParentItem & { label: string; status: string; verifiedAt: string; scope?: string };
type SavedClaim = { id: string; version: number; sha256: string; proposition: string; evidence_status: string; verified_at: string; support_scope?: string | null };
type Inspection = { title: string; passage?: EvidencePassage; calculation?: Calculation; text?: string };
type AssessmentTarget = { target_type: "option_costs" | "requirement"; option_id: string; requirement_id: string | null; target_hash: string; label: string; reference_ids: string[] };
const TERMINAL = new Set(["completed", "completed_with_limitations", "cancelled", "failed", "limited", "admission_expired"]);
const PROVIDERS: Record<string, string> = { openai: "OpenAI", anthropic: "Anthropic Claude", google: "Google Gemini" };
const READINESS: Record<string, string> = { ready_to_choose: "Ready within the stated scope", conditional: "Conditional decision", no_feasible_option: "No feasible option", insufficient_evidence: "More evidence needed" };
const CURRENT_KEY = "round-robin:current-local-comparison:v1";
const clone = <T,>(value: T): T => structuredClone(value);
const words = (value: string | null | undefined) => (value ?? "unavailable").replaceAll("_", " ");
const money = (value: string | null | undefined, currency = "USD") => {
  if (value == null) return "Unknown";
  const [whole, fractional = ""] = value.split(".");
  const minorUnits = new Intl.NumberFormat("en-US", { style: "currency", currency }).resolvedOptions().maximumFractionDigits ?? 2;
  const fraction = fractional.replace(/0+$/, "").padEnd(minorUnits, "0");
  const exact = `${BigInt(whole).toLocaleString("en-US")}${fraction ? `.${fraction}` : ""}`;
  return new Intl.NumberFormat("en-US", { style: "currency", currency, minimumFractionDigits: 0, maximumFractionDigits: 0 }).formatToParts(0n).map(part => part.type === "integer" ? exact : part.value).join("");
};
const when = (value: string) => new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short", timeZone: "America/Phoenix" }).format(new Date(value));

function option(id: string, label: string, role: "candidate" | "status_quo", currency = "USD"): DraftBrief["options"][number] {
  return { id, label, role, provenance: "user_supplied", consequences: [], cost_components: [
    ...(["upfront", "recurring", "installation", "maintenance", "tax_delivery"] as const).map(kind => ({
      id: `cost_${id}_${kind}`, label: kind === "recurring" ? "Monthly recurring cost" : kind === "maintenance" ? "Monthly maintenance cost" : kind === "tax_delivery" ? "Tax and delivery cost" : `${kind} cost`, kind,
      amount: null, currency, quantity: "1", frequency: kind === "recurring" || kind === "maintenance" ? "monthly" as const : "once" as const,
      status: "included" as const, unknown_reason: "Amount not supplied", source_ref: `quote_${id}`,
      unit: "item" as const, quote_valid_until: null,
    })),
  ] };
}

function editableRequirement(item: DraftBrief["hard_requirements"][number]) {
  return item.id !== "requirement_budget" && item.id !== "requirement_deadline" && item.metric === "manual" && item.operator === "manual";
}

function newBrief(): DraftBrief {
  return { schema_version: "1.0", revision: 1, title: "", question: "", background: "", goal: "", currency: "USD", budget: null,
    deadline: null, timezone: "America/Phoenix", location: "", horizon_months: 24,
    options: [option("candidate_a", "Option A", "candidate"), option("candidate_b", "Option B", "candidate"), option("status_quo", "Keep the current approach", "status_quo")],
    hard_requirements: [], preferences: [], user_facts: [], selected_attachment_ids: [], public_research_terms: [],
  };
}

function retainRunHistory(fresh: MeetingView, previous: MeetingView): MeetingView {
  if (previous.runs.length <= fresh.runs.length) return fresh;
  const freshIds = new Set(fresh.runs.map(item => item.id));
  return { ...fresh, runs: [...fresh.runs, ...previous.runs.filter(item => !freshIds.has(item.id))], next_run_cursor: previous.next_run_cursor };
}

export default function LiveWorkspace() {
  const [meetings, setMeetings] = useState<MeetingView[]>([]);
  const [nextMeetingCursor, setNextMeetingCursor] = useState<string | null>(null);
  const [meeting, setMeeting] = useState<MeetingView | null>(null);
  const [draft, setDraft] = useState<DraftBrief>(newBrief);
  const [requirementsText, setRequirementsText] = useState("");
  const [preferencesText, setPreferencesText] = useState("");
  const [publicTermsText, setPublicTermsText] = useState("");
  const requirementsInput = useRef<HTMLTextAreaElement>(null);
  const preferencesInput = useRef<HTMLTextAreaElement>(null);
  const publicTermsInput = useRef<HTMLTextAreaElement>(null);
  const [dirty, setDirty] = useState(false);
  const [autosaveStatus, setAutosaveStatus] = useState<"idle" | "saving" | "error">("idle");
  const editGeneration = useRef(0);
  const editorScopeGeneration = useRef(0);
  const autosaveInFlight = useRef(false);
  const selectedMeetingId = useRef<string | null>(null);
  const [editor, setEditor] = useState(true);
  const [clarifying, setClarifying] = useState(false);
  const [readEpoch, setReadEpoch] = useState(0);
  const [parentRunId, setParentRunId] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<PreflightView | null>(null);
  const [consent, setConsent] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingCommand | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [sessionProfile, setSessionProfile] = useState("development");
  const [signInRequired, setSignInRequired] = useState(false);
  const [liveAvailable, setLiveAvailable] = useState(false);
  const [selectedMode, setSelectedMode] = useState<"simulated" | "live">("simulated");
  const [connectionNote, setConnectionNote] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const inspectionGeneration = useRef(0);
  const [choiceOption, setChoiceOption] = useState("");
  const [choiceNote, setChoiceNote] = useState("");
  const [outcomeNote, setOutcomeNote] = useState("");
  const [outcomes, setOutcomes] = useState<OutcomeRecord[]>([]);
  const [uploads, setUploads] = useState<UploadView[]>([]);
  const [parentCandidates, setParentCandidates] = useState<ParentCandidate[]>([]);
  const [selectedParentItems, setSelectedParentItems] = useState<ParentItem[]>([]);
  const [parentSourceCursor, setParentSourceCursor] = useState<string | null>(null);
  const [reuseDirty, setReuseDirty] = useState(false);
  const [assessmentTargets, setAssessmentTargets] = useState<AssessmentTarget[]>([]);
  const [assessmentTarget, setAssessmentTarget] = useState("");
  const [attestation, setAttestation] = useState("");
  const [assessmentResult, setAssessmentResult] = useState("pass");
  const [assessmentUntil, setAssessmentUntil] = useState("");

  const currentRun = snapshot?.run ?? null;
  const ledgerUnavailable = currentRun?.remaining_limits.requests_unavailable_reason === "ledger_integrity_failed";
  const savedStateUnavailable = currentRun?.report_unavailable_reason === "saved_state_integrity_failed";
  const report = ledgerUnavailable || savedStateUnavailable ? null : snapshot?.report ?? null;
  const running = !!currentRun && !TERMINAL.has(currentRun.status);
  const blocked = !!busy || !!pending;
  const expired = !!preflight && now >= new Date(preflight.expires_at).getTime();
  const displayMode = editor ? selectedMode : preflight?.mode ?? currentRun?.execution_mode ?? "simulated";
  const manualRequirements = requirementsText.split("\n").filter(line => line.trim());
  const reservedRequirements = draft.hard_requirements.filter(item => !editableRequirement(item)).length;
  const requirementLimit = 20 - reservedRequirements;
  const requirementsError = manualRequirements.length > requirementLimit
    ? `Use at most ${requirementLimit} manual requirements; saved numeric checks, budget and deadline share the limit of 20.`
    : manualRequirements.some(line => line.length > 500) ? "Keep each requirement within 500 characters." : "";
  const preferenceLines = preferencesText.split("\n").filter(line => line.trim());
  const preferencesError = preferenceLines.length > 10 ? "Use at most 10 preferences. Your entered lines are retained."
    : preferenceLines.some(line => line.length > 500) ? "Keep each preference within 500 characters." : "";
  const publicTerms = publicTermsText.split("\n").filter(line => line.trim());
  const publicTermsError = publicTerms.length > 8 || publicTerms.some(term => term.length > 120)
    ? "Use at most eight public terms, each no longer than 120 characters." : "";

  useEffect(() => onAccessChange(() => {
    selectedMeetingId.current = null; editGeneration.current += 1; editorScopeGeneration.current += 1;
    setMeetings([]); setNextMeetingCursor(null); setMeeting(null); setDraft(newBrief());
    setSnapshot(null); setRunId(null); setPreflight(null); setConsent(false); setInspection(null);
    dialog.current?.close(); setUploads([]); setOutcomes([]);
    setRequirementsText(""); setPreferencesText(""); setPublicTermsText("");
    setChoiceOption(""); setChoiceNote(""); setOutcomeNote("");
    setDirty(false); setEditor(true); setParentRunId(null); setClarifying(false); setAutosaveStatus("idle");
    setParentCandidates([]); setSelectedParentItems([]); setParentSourceCursor(null); setReuseDirty(false);
    setSignInRequired(true); setConnected(false);
    setAssessmentTargets([]); setAssessmentTarget(""); setAttestation(""); setAssessmentUntil("");
  }), []);

  useEffect(() => {
    requirementsInput.current?.setCustomValidity(requirementsError);
    preferencesInput.current?.setCustomValidity(preferencesError);
    publicTermsInput.current?.setCustomValidity(publicTermsError);
  }, [requirementsError, preferencesError, publicTermsError, editor]);

  useEffect(() => {
    inspectionGeneration.current += 1;
    if (ledgerUnavailable || savedStateUnavailable) {
      setInspection(null);
      if (dialog.current?.open) dialog.current.close();
    }
  }, [runId, ledgerUnavailable, savedStateUnavailable]);

  useEffect(() => {
    setChoiceOption(""); setChoiceNote(""); setOutcomeNote("");
  }, [runId, report?.id]);

  useEffect(() => {
    let cancelled = false;
    setAssessmentTargets([]); setAssessmentTarget(""); setAttestation(""); setAssessmentUntil("");
    if (runId && report && !report.simulated && currentRun && TERMINAL.has(currentRun.status)) {
      void readApi<{ targets: AssessmentTarget[] }>(`/api/runs/${runId}/assessments`).then(result => {
        if (!cancelled && Array.isArray(result.targets)) setAssessmentTargets(result.targets);
      }).catch(() => {});
    }
    return () => { cancelled = true; };
  }, [runId, report?.id]);

  useEffect(() => {
    if (!dirty) return;
    const protectDraft = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protectDraft);
    return () => window.removeEventListener("beforeunload", protectDraft);
  }, [dirty]);

  useEffect(() => {
    const parent = preflight?.parent_run_id;
    setParentCandidates([]); setSelectedParentItems([]); setParentSourceCursor(null); setReuseDirty(false);
    if (!parent) return;
    let cancelled = false;
    void Promise.all([readApi<{ items: EvidencePassage[]; next_cursor: string | null }>(`/api/runs/${parent}/sources`), readApi<{ items: SavedClaim[] }>(`/api/runs/${parent}/claims`)]).then(([evidence, claims]) => {
      if (cancelled) return;
      setParentCandidates([...evidence.items.map(item => ({ kind: "evidence" as const, id: item.id, version: 1, sha256: item.sha256, label: `${item.source_title}: ${item.text.slice(0, 400)}`, status: item.verification_status, verifiedAt: item.verified_at })), ...claims.items.map(item => ({ kind: "claim" as const, id: item.id, version: item.version, sha256: item.sha256, label: item.proposition, status: item.evidence_status, verifiedAt: item.verified_at, scope: item.support_scope ?? "Attributed claim support; inspect the exact basis. This is not independent fact verification." }))]);
      setParentSourceCursor(evidence.next_cursor);
    }).catch(() => { if (!cancelled) setError("The parent evidence and claims could not be read. No earlier input has been selected automatically."); });
    return () => { cancelled = true; };
  }, [preflight?.parent_run_id]);

  async function refreshMeetings() {
    const result = await readApi<{ items: MeetingView[]; next_cursor: string | null }>("/api/meetings");
    setMeetings(result.items); setNextMeetingCursor(result.next_cursor); setConnected(true);
    setMeeting(current => { const fresh = result.items.find(item => item.id === current?.id); return current && fresh ? retainRunHistory(fresh, current) : current; });
    return result.items;
  }

  function selectMeeting(value: MeetingView, selectedRun?: string | null) {
    selectedMeetingId.current = value.id; editGeneration.current += 1; editorScopeGeneration.current += 1; setAutosaveStatus("idle");
    setMeeting(value); setDraft(clone(value.draft)); setDirty(false); setParentRunId(null); setClarifying(false);
    setRequirementsText(value.draft.hard_requirements.filter(editableRequirement).map(item => item.description).join("\n"));
    setPreferencesText(value.draft.preferences.map(item => item.description).join("\n"));
    setPublicTermsText(value.draft.public_research_terms.map(item => item.text).join("\n"));
    setPreflight(null); setConsent(false); setSnapshot(null); setError(null); setNotice(null);
    const latest = [...value.runs].sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    const nextRun = selectedRun === undefined ? latest?.id ?? null : selectedRun;
    setRunId(nextRun); setEditor(!nextRun); setTab("overview"); setChoiceOption(""); setChoiceNote(""); setOutcomeNote("");
    try { sessionStorage.setItem(CURRENT_KEY, JSON.stringify({ meetingId: value.id, runId: nextRun })); } catch { /* Selection is optional; pending command storage is enforced separately. */ }
  }

  function canLeaveDraft() {
    return !dirty || window.confirm("This draft has unsaved edits. Discard them and leave this comparison?");
  }

  async function loadOlderRuns() {
    if (!meeting?.next_run_cursor) return;
    const id = meeting.id;
    const cursor = meeting.next_run_cursor;
    await act("Reading earlier reviews", async () => {
      const more = await readApi<{ items: RunView[]; next_cursor: string | null }>(`/api/meetings/${id}/runs?cursor=${encodeURIComponent(cursor)}`);
      const append = (current: MeetingView) => ({ ...current, runs: [...current.runs, ...more.items.filter(item => !current.runs.some(saved => saved.id === item.id))], next_run_cursor: more.next_cursor });
      setMeeting(current => current?.id === id ? append(current) : current);
      setMeetings(items => items.map(item => item.id === id ? append(item) : item));
    });
  }

  async function initialize() {
    try {
      const identity = await refreshSession(true);
      setSessionProfile(identity.profile); setSignInRequired(false);
      const health = await readApi<{ mode: string; paid_access_configured?: boolean }>("/health");
      setLiveAvailable(health.mode === "live" && health.paid_access_configured === true);
      setPending(pendingCommand());
      const items = await refreshMeetings();
      let retained: { meetingId?: string; runId?: string } = {};
      try { retained = JSON.parse(sessionStorage.getItem(CURRENT_KEY) ?? "{}"); } catch { /* Read server history instead. */ }
      const existing = items.find(item => item.id === retained.meetingId) ?? (retained.meetingId ? await readApi<MeetingView>(`/api/meetings/${retained.meetingId}`).catch(() => undefined) : undefined);
      if (existing) selectMeeting(existing, retained.runId);
      setConnectionNote(null);
    } catch (failure) {
      if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403)) setSignInRequired(true);
      setConnected(false); setError(failure instanceof Error ? failure.message : "The local service is unavailable.");
    }
  }

  useEffect(() => { void initialize(); }, []); // Mount performs reads only; it never replays a paid command.
  useEffect(() => {
    if (!meeting) { setOutcomes([]); return; }
    let cancelled = false;
    void readApi<{ items: OutcomeRecord[] }>(`/api/meetings/${meeting.id}/outcomes`).then(value => { if (!cancelled) setOutcomes(value.items); }).catch(() => {});
    void readApi<{ items: UploadView[] }>(`/api/meetings/${meeting.id}/uploads`).then(value => { if (!cancelled) setUploads(value.items); }).catch(() => {});
    return () => { cancelled = true; };
  }, [meeting?.id]);
  useEffect(() => { const interval = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(interval); }, []);
  useEffect(() => {
    if (!dirty || !editor || !meeting || clarifying || parentRunId || busy || pending || autosaveStatus === "error" || autosaveInFlight.current || !validateDraft(draft)) return;
    const targetId = meeting.id;
    const expectedRevision = meeting.draft_revision;
    const timer = setTimeout(async () => {
      if (autosaveInFlight.current || pendingCommand()) return;
      autosaveInFlight.current = true;
      setAutosaveStatus("saving");
      const generation = editGeneration.current;
      const scopeGeneration = editorScopeGeneration.current;
      try {
        const saved = await sendCommand<{ meeting_id: string; revision: number; brief: DraftBrief }>(`draft:${targetId}`, `/api/meetings/${targetId}/draft`, { expected_revision: expectedRevision, brief: clone(draft) }, "PATCH");
        setMeeting(current => current?.id === targetId ? { ...current, draft: saved.brief, draft_revision: saved.revision, title: saved.brief.title } : current);
        setMeetings(current => current.map(item => item.id === targetId ? { ...item, draft: saved.brief, draft_revision: saved.revision, title: saved.brief.title } : item));
        if (selectedMeetingId.current === targetId && editorScopeGeneration.current === scopeGeneration) {
          if (editGeneration.current === generation) { setDraft(clone(saved.brief)); setDirty(false); }
          else setDraft(current => ({ ...current, revision: saved.revision }));
          setAutosaveStatus("idle");
        }
      } catch (failure) {
        if (selectedMeetingId.current === targetId && editorScopeGeneration.current === scopeGeneration) {
          setAutosaveStatus("error");
          setError(failure instanceof Error ? failure.message : "Autosave is not confirmed. Your edits remain in this tab.");
        }
        try { setPending(pendingCommand()); } catch { /* Preserve the pending receipt and the visible error. */ }
      } finally {
        autosaveInFlight.current = false;
      }
    }, 700);
    return () => clearTimeout(timer);
  }, [draft, dirty, editor, meeting?.id, meeting?.draft_revision, clarifying, parentRunId, busy, pending, autosaveStatus]);
  useEffect(() => {
    if (!inspection || !dialog.current) return;
    const active = document.activeElement as HTMLElement | null;
    dialog.current.showModal();
    return () => active?.focus();
  }, [inspection]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await readApi<Snapshot>(`/api/runs/${runId}/snapshot`);
        if (cancelled) return;
        const events = new Map(value.events.map(item => [item.sequence, item]));
        let cursor = value.next_event_cursor;
        const paginated = cursor !== null && cursor !== undefined && cursor < value.event_cutoff;
        while (cursor !== null && cursor !== undefined && cursor < value.event_cutoff) {
          const page = await readApi<{ items: Snapshot["events"]; next_cursor: number; event_cutoff: number }>(`/api/runs/${runId}/events?after=${cursor}&limit=200`);
          if (cancelled) return;
          if (!Array.isArray(page.items) || !Number.isInteger(page.next_cursor) || page.next_cursor <= cursor) throw new Error("Saved activity pagination did not advance.");
          for (const event of page.items) {
            if (event.run_id !== runId) throw new Error("Saved activity belongs to a different run.");
            if (event.sequence <= value.event_cutoff) events.set(event.sequence, event);
          }
          cursor = page.next_cursor;
        }
        value.events = [...events.values()].sort((left, right) => left.sequence - right.sequence);
        if (paginated && value.events.length && value.events.some((item, index) => item.sequence !== index + 1)) throw new Error("Saved activity is incomplete; reconnect to read the remaining history.");
        value.next_event_cursor = null;
        setSnapshot(current => current?.run.id === value.run.id && (current.run.version > value.run.version || current.event_cutoff > value.event_cutoff) ? current : value);
        setConnected(true); setConnectionNote(null);
        if (!TERMINAL.has(value.run.status)) timer = setTimeout(poll, 700);
        else void refreshMeetings().catch(() => {});
      } catch (failure) {
        if (cancelled) return;
        setConnected(false); setConnectionNote("Connection interrupted. This is the last confirmed result. Reconnecting reads saved state and does not start another review.");
        if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403)) { setSignInRequired(true); return; }
        timer = setTimeout(poll, 1800);
      }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [runId, readEpoch]);

  async function act(label: string, action: () => Promise<void>) {
    setBusy(label); setError(null); setNotice(null);
    try { await action(); setConnected(true); }
    catch (failure) { if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403)) setSignInRequired(true); setError(failure instanceof Error ? failure.message : "The action could not be confirmed."); }
    finally {
      setBusy(null);
      try { setPending(pendingCommand()); } catch (failure) { setError(failure instanceof Error ? failure.message : "Pending action unavailable"); }
    }
  }

  function edit(change: Partial<DraftBrief>) { editGeneration.current += 1; setDraft(old => ({ ...old, ...change })); setDirty(true); setPreflight(null); setConsent(false); if (autosaveStatus === "error") setAutosaveStatus("idle"); }
  function editOption(index: number, label: string) { edit({ options: draft.options.map((item, i) => i === index ? { ...item, label } : item) }); }
  function editCost(index: number, componentIndex: number, value: string) {
    edit({ options: draft.options.map((item, i) => i === index ? { ...item, cost_components: item.cost_components.map((cost, j) => j === componentIndex && cost.status === "included" ? { ...cost, amount: value || null, unknown_reason: value ? null : "Amount not supplied" } : cost) } : item) });
  }
  function editCostStatus(index: number, componentIndex: number, status: DraftBrief["options"][number]["cost_components"][number]["status"]) {
    edit({ options: draft.options.map((item, i) => i === index ? { ...item, cost_components: item.cost_components.map((cost, j) => j === componentIndex ? { ...cost, status, unknown_reason: cost.amount === null && status === "included" ? "Amount not supplied" : null } : cost) } : item) });
  }
  function setBudget(value: string) {
    const hard = draft.hard_requirements.filter(item => item.id !== "requirement_budget");
    if (value) hard.unshift({ id: "requirement_budget", description: "Total cost within the supplied budget", origin: "user_supplied", metric: "total_cost", operator: "lte", threshold: value });
    edit({ budget: value || null, hard_requirements: hard });
  }
  function setDeadline(value: string) {
    const hard = draft.hard_requirements.filter(item => item.id !== "requirement_deadline");
    if (value) hard.push({ id: "requirement_deadline", description: `Complete by ${value}; obtain a committed supplier schedule`, origin: "user_supplied", metric: "manual", operator: "manual", threshold: null });
    edit({ deadline: value || null, hard_requirements: hard });
  }
  function editRequirements(value: string) {
    setRequirementsText(value);
    const previous = draft.hard_requirements.filter(editableRequirement);
    const used = new Set<string>();
    const items = value.split("\n").filter(line => line.trim()).map(description => {
      const unchanged = previous.find(item => item.description === description && !used.has(item.id));
      if (unchanged) { used.add(unchanged.id); return unchanged; }
      return { id: `requirement_${crypto.randomUUID().replaceAll("-", "")}`, description, origin: "user_supplied" as const, metric: "manual" as const, operator: "manual" as const, threshold: null };
    });
    edit({ hard_requirements: [...draft.hard_requirements.filter(item => !editableRequirement(item)), ...items] });
  }

  async function save(event?: FormEvent) {
    event?.preventDefault();
    await act("Saving draft", async () => {
      if (clarifying && snapshot) {
        const confirmed = await sendCommand<RunView>(`clarification:${snapshot.run.id}:${snapshot.run.version}`, `/api/runs/${snapshot.run.id}/clarifications`, { expected_run_version: snapshot.run.version, brief: draft });
        setRunId(confirmed.id); setEditor(false); setClarifying(false); setDirty(false); setReadEpoch(value => value + 1);
        setNotice("The brief is confirmed. The review resumes with its original allowance and saved work.");
      } else if (parentRunId && meeting) {
        const saved = await sendCommand<MeetingView>(`followup:${meeting.id}:${parentRunId}`, `/api/meetings/${meeting.id}/follow-ups`, { parent_run_id: parentRunId, expected_draft_revision: meeting.draft_revision, brief: draft });
        selectMeeting(saved, null); setNotice("Follow-up draft saved. The original report is preserved. Review a fresh preflight before starting.");
      } else if (meeting) {
        const saved = await sendCommand<{ meeting_id: string; revision: number; brief: DraftBrief }>(`draft:${meeting.id}`, `/api/meetings/${meeting.id}/draft`, { expected_revision: meeting.draft_revision, brief: draft }, "PATCH");
        const next = { ...meeting, draft: saved.brief, draft_revision: saved.revision, title: saved.brief.title };
        setMeeting(next); setDraft(clone(saved.brief)); setDirty(false); setNotice("Draft saved on the local service. No adviser was called.");
      } else {
        const saved = await sendCommand<MeetingView>("create-comparison", "/api/meetings", { brief: draft });
        selectMeeting(saved, null); setNotice("Comparison saved. Review the input and allowance before starting.");
      }
      await refreshMeetings();
    });
  }

  async function reviewPreflight() {
    if (!meeting || dirty) { setError("Save the current draft before reviewing its preflight."); return; }
    await act("Preparing preflight", async () => {
      const result = await sendCommand<PreflightView>(`preflight:${meeting.id}:${meeting.draft_revision}:${selectedMode}`, `/api/meetings/${meeting.id}/preflight`, { expected_draft_revision: meeting.draft_revision, mode: selectedMode, selected_parent_items: selectedParentItems });
      setReuseDirty(false); setPreflight(result); setConsent(false); setEditor(false); setNotice(null);
    });
  }

  async function start() {
    if (!meeting || !preflight || !consent || expired || reuseDirty) return;
    await act("Confirming start", async () => {
      const result = await sendCommand<RunView>(`start:${preflight.id}`, `/api/meetings/${meeting.id}/runs`, { preflight_id: preflight.id, expected_draft_revision: preflight.draft_revision, ...(preflight.parent_replacement ? { replace_parent_run_version: preflight.parent_replacement.version } : {}) });
      setRunId(result.id); setSnapshot(null); setPreflight(null); setEditor(false); setTab("activity");
      sessionStorage.setItem(CURRENT_KEY, JSON.stringify({ meetingId: meeting.id, runId: result.id }));
      await refreshMeetings();
    });
  }

  async function stop(abandonRetry = false) {
    if (!currentRun) return;
    await act("Stop not confirmed", async () => {
      const result = await sendCommand<RunView>(`cancel:${currentRun.id}`, `/api/runs/${currentRun.id}/cancel`, { expected_run_version: currentRun.version });
      const fresh = await readApi<Snapshot>(`/api/runs/${result.id}/snapshot`); setSnapshot(fresh);
      setReadEpoch(value => value + 1);
      setNotice(abandonRetry ? "Unused retry allowance released. The saved report and unresolved charges are preserved." : "Stop acknowledged by the local service. Saved contributions and any uncertainty are preserved.");
    });
  }

  function followUp() {
    if (!snapshot?.brief || !canLeaveDraft()) return;
    editGeneration.current += 1; editorScopeGeneration.current += 1; setAutosaveStatus("idle");
    setDraft(clone(snapshot.brief)); setParentRunId(snapshot.run.id); setEditor(true); setDirty(true); setPreflight(null); setClarifying(false);
    setRequirementsText(snapshot.brief.hard_requirements.filter(editableRequirement).map(item => item.description).join("\n"));
    setPreferencesText(snapshot.brief.preferences.map(item => item.description).join("\n"));
    setPublicTermsText(snapshot.brief.public_research_terms.map(item => item.text).join("\n"));
    setNotice("Edit the facts that changed, then save this linked follow-up. The original report remains unchanged.");
  }

  function clarify() {
    if (!snapshot?.brief || !canLeaveDraft()) return false;
    editGeneration.current += 1; editorScopeGeneration.current += 1; setAutosaveStatus("idle");
    setDraft(clone(snapshot.brief)); setParentRunId(null); setEditor(true); setClarifying(true); setDirty(false); setPreflight(null);
    setRequirementsText(snapshot.brief.hard_requirements.filter(editableRequirement).map(item => item.description).join("\n"));
    setPreferencesText(snapshot.brief.preferences.map(item => item.description).join("\n"));
    setPublicTermsText(snapshot.brief.public_research_terms.map(item => item.text).join("\n"));
    setNotice("Resolve the listed gaps and confirm this brief. Confirmation resumes the existing review; it does not start a new allowance.");
    return true;
  }
  function reviewIntakeChange(change: { field: string; proposed_value_json: string }) {
    if (!snapshot?.brief) return;
    try {
      const candidate = { ...clone(snapshot.brief), [change.field]: JSON.parse(change.proposed_value_json) };
      if (!validateDraft(candidate)) throw new Error("The suggested change does not match the brief contract.");
      if (!clarify()) return;
      setDraft(candidate); setDirty(true);
      setRequirementsText(candidate.hard_requirements.filter(editableRequirement).map(item => item.description).join("\n"));
      setPreferencesText(candidate.preferences.map(item => item.description).join("\n"));
      setNotice("Suggested change copied into the editor. Review and confirm the updated brief before proposals continue.");
    } catch { setError("This intake suggestion could not be applied. Enter and confirm the intended change in the brief editor."); }
  }

  async function retryStage(stage: "synthesis" | "audit") {
    if (!currentRun) return;
    await act(`Requesting ${stage} recovery`, async () => {
      await sendCommand(`retry:${currentRun.id}:${currentRun.version}:${stage}`, `/api/runs/${currentRun.id}/retries`, { expected_run_version: currentRun.version, stage });
      setReadEpoch(value => value + 1); setNotice("Recovery requested within the original review limits. Earlier proposals remain saved.");
    });
  }

  async function recover(mode: "reconcile" | "retry" | "abandon") {
    await act(mode === "retry" ? "Retrying the same action" : mode === "abandon" ? "Resolving the original action" : "Reconciling action", async () => {
      const abandoned = mode === "abandon" ? await abandonPendingCommand() : null;
      const result = abandoned ? { confirmed: true, result: abandoned.result } : mode === "retry" ? { confirmed: true, result: await retryPendingCommand() } : await reconcileCommand();
      if (!result.confirmed) { setNotice("The action is not yet confirmed. Its original receipt is retained. Retry the same action to resolve it safely."); return; }
      const value = result.result as { meeting_id?: string; id?: string; receipt_unavailable?: boolean } | undefined;
      if (abandoned?.status !== "abandoned" && value?.receipt_unavailable === true) setNotice("The original action committed, but its saved receipt cannot be displayed. Refresh the workspace to view current state.");
      const items = await refreshMeetings();
      const original = pending;
      const isRun = original?.path.endsWith("/runs") || original?.path.endsWith("/cancel");
      const next = items.find(item => item.id === value?.meeting_id || item.id === value?.id || item.id === meeting?.id);
      if (next) selectMeeting(next, isRun ? value?.id : undefined);
      setNotice(abandoned?.status === "abandoned" ? "The original action was safely abandoned. It cannot execute later; you can enter and save your inputs again." : value?.receipt_unavailable === true ? "The original action committed, but its saved receipt cannot be displayed. The workspace has been refreshed." : "The original action has been reconciled with saved server state.");
    });
  }

  async function inspect(kind: "passage" | "calculation", id: string, reportId?: string) {
    if (!runId || (kind !== "passage" && (ledgerUnavailable || savedStateUnavailable))) return;
    const inspectionAtStart = inspectionGeneration.current;
    await act("Opening saved evidence", async () => {
      if (kind === "passage") {
        const value = await readApi<EvidencePassage>(`/api/runs/${runId}/sources/${id}`);
        if (inspectionGeneration.current === inspectionAtStart) setInspection({ title: value.source_title, passage: value });
      } else {
        const value = await readApi<Calculation>(`/api/runs/${runId}/calculations/${id}${reportId ? `?report_id=${encodeURIComponent(reportId)}` : ""}`);
        if (inspectionGeneration.current === inspectionAtStart) setInspection({ title: value.label, calculation: value });
      }
    });
  }

  function reset() {
    if (!canLeaveDraft()) return;
    selectedMeetingId.current = null; editGeneration.current += 1; editorScopeGeneration.current += 1; setAutosaveStatus("idle");
    setMeeting(null); setDraft(newBrief()); setDirty(false); setEditor(true); setRunId(null); setSnapshot(null); setPreflight(null); setParentRunId(null); setNotice(null); setError(null); setClarifying(false);
    setRequirementsText(""); setPreferencesText(""); setPublicTermsText(""); setOutcomes([]); setUploads([]);
    sessionStorage.removeItem(CURRENT_KEY);
  }

  async function upload(file: File) {
    if (!meeting) return;
    await act("Uploading selected text", async () => {
      const result = await uploadTextFile(meeting.id, meeting.draft_revision, file);
      setUploads(current => [...current.filter(item => item.id !== result.id), result]);
      if (result.status === "ready") {
        edit({ selected_attachment_ids: [...new Set([...draft.selected_attachment_ids, result.id])] });
        setNotice("Text uploaded and validated. Save the draft to include this selected input in the next preflight.");
      } else setNotice("The upload is not ready. It has not been attached to this review.");
    });
  }

  function reportPanel(saved: DecisionReport) {
    return <>
      <section className="live-decision" aria-labelledby="decision-heading">
        <div><span className="live-eyebrow">The decision brief · version {saved.version}</span><h2 id="decision-heading">{saved.recommendation?.summary ?? "A choice is not established yet."}</h2></div>
        <span className={`live-pill ${saved.readiness === "ready_to_choose" ? "positive" : "caution"}`}>{saved.readiness ? READINESS[saved.readiness] ?? words(saved.readiness) : "Readiness unavailable"}</span>
        <p>{saved.readiness_reasons.join(" ")}</p>
        {saved.main_reasons.length > 0 && <ul>{saved.main_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
      </section>
      <section className="live-card"><h3>{saved.simulated ? "Simulated work and review coverage" : "Provider spending and review coverage"}</h3><p>{saved.simulated ? "No external provider spending. " : ""}{saved.usage.actual_cost === null ? "Final provider cost is unconfirmed. " : `Saved provider cost: ${money(saved.usage.actual_cost)}. `}Conservatively counted exposure: {money(saved.usage.conservative_exposure_usd)}. {saved.usage.request_count} counted {saved.simulated ? "simulated requests" : "inference requests"}.</p>{saved.usage.status === "unknown" && <p className="live-alert">Unresolved charges remain counted. A new review or retry does not make them free.</p>}{saved.research_summary.status !== "not_requested" && <p>Public research: {words(saved.research_summary.status)}. {saved.research_summary.reason ?? "Retrieved passages still require a support check."}</p>}</section>
      <div className="live-two"><section className="live-card"><span className="live-eyebrow">The decisive tradeoff</span><p>{saved.tradeoff}</p></section><section className="live-card live-caveat"><span className="live-eyebrow">The largest uncertainty</span><p>{saved.largest_uncertainty}</p></section></div>
      <section className="live-card"><h3>Compare the approaches</h3><p>One common horizon. Missing components remain unknown.</p><div className="live-table-scroll"><table><caption className="visually-hidden">Cost and hard-requirement comparison over the saved planning horizon</caption><thead><tr><th>Approach</th><th>All-in total</th><th>Hard requirements</th><th>Basis</th></tr></thead><tbody>{saved.comparison.map(item => <tr key={item.option_id}><th scope="row">{item.label}<small>{item.horizon_months} months · {words(item.role)}</small></th><td>{money(item.total, item.currency)}{item.total === null && <small>{money(item.known_subtotal, item.currency)} known subtotal</small>}</td><td>{item.constraint_results.length ? item.constraint_results.map(result => <span className="live-requirement" key={result.requirement_id}>{words(result.result)} · {result.reason}</span>) : "No assessment supplied"}</td><td><button type="button" className="live-link" onClick={() => void inspect("calculation", item.calculation_id, saved.id)}>Inspect {item.label} calculation</button><small>{item.caveat}</small></td></tr>)}</tbody></table></div></section>
      <div className="live-two"><section className="live-card"><h3>Your next action</h3><p>{saved.next_action}</p>{saved.next_evidence.map(item => <div className="live-check" key={item.id}><strong>{item.question}</strong><p>{item.verification_method}</p><button type="button" className="live-link" onClick={() => setInspection({ title: "Quote or site-check request", text: item.request_text })}>Open copyable request</button></div>)}</section><section className="live-card"><h3>What would change the answer?</h3><p>{saved.what_would_change_answer}</p>{saved.change_summary.map(line => <p key={line}>{line}</p>)}<button className="live-secondary" type="button" disabled={blocked || !snapshot?.brief} onClick={followUp}>Create a linked follow-up</button></section></div>
      {saved.dissent.length > 0 && <section className="live-card"><h3>Alternatives and unresolved disagreement</h3><ul>{saved.dissent.map(line => <li key={line}>{line}</li>)}</ul></section>}
      {!!saved.issues?.length && <section className="live-card"><h3>Objections and recorded resolutions</h3><p>Every material concern remains in the saved record until an explicit response and review address it.</p>{saved.issues.map(issue => <details key={issue.id} open={issue.severity === "critical" && issue.status !== "resolved" && issue.status !== "withdrawn"}><summary>{issue.severity} · {words(issue.status)} · {issue.concern}</summary><p>{PROVIDERS[issue.source_provider] ?? issue.source_provider} · {words(issue.source_stage)}</p>{issue.events.map(event => <p key={event.id}>{words(event.disposition)}: {event.response} · {PROVIDERS[event.provider] ?? event.provider}</p>)}</details>)}</section>}
      {saved.readiness_dimensions && <section className="live-card"><h3>Scope of the decision checks</h3><p>{saved.readiness_dimensions.basis_scope}</p><ul><li>Comparable inputs: {saved.readiness_dimensions.complete_comparison ? "complete" : "incomplete"}</li><li>Scoped cost evidence: {saved.readiness_dimensions.scoped_cost_support ? "accepted within its recorded scope" : "still needs checking"}</li><li>Distinct review: {saved.readiness_dimensions.distinct_review ? "complete" : "incomplete"}</li><li>Material objections: {saved.readiness_dimensions.material_objections_resolved ? "addressed" : "remain unresolved"}</li></ul></section>}
      {saved.decision_insights && <section className="live-card"><h3>Cost sensitivity and option ordering</h3>{saved.decision_insights.sensitivity.map(item => <p key={`${item.option_id}:${item.requirement_id}`}>{saved.comparison.find(option => option.option_id === item.option_id)?.label ?? item.option_id}: margin {item.margin} {item.unit} against requirement {item.requirement_id}. {item.scope}</p>)}{saved.decision_insights.limitations.map(line => <p key={line}>{line}</p>)}</section>}
      <section className="live-card"><h3>Your choice and later outcome</h3><p>These are your records. They remain separate from the saved adviser recommendation.</p><div className="live-two"><label>Chosen approach<select value={choiceOption} onChange={event => setChoiceOption(event.target.value)}><option value="">Select an approach</option>{saved.comparison.map(item => <option key={item.option_id} value={item.option_id}>{item.label}</option>)}<option value="defer">Defer this decision</option></select></label><label>Why you chose it<input value={choiceNote} maxLength={1500} onChange={event => setChoiceNote(event.target.value)} /></label></div><button type="button" className="live-secondary" disabled={blocked || !validChoice(saved)} onClick={() => void saveChoice(saved)}>Save my choice</button><label>What happened afterwards?<textarea value={outcomeNote} maxLength={4000} onChange={event => setOutcomeNote(event.target.value)} placeholder="Record the observed result when you know it." /></label><button type="button" className="live-secondary" disabled={blocked || !validChoice(saved) || !outcomeNote.trim()} onClick={() => void saveOutcome(saved)}>Save outcome</button>{outcomes.filter(item => item.payload.run_id === saved.run_id).map(item => <article className="live-evidence" key={item.id}><strong>{words(item.payload.choice)} · {when(item.created_at)}</strong><p>{item.payload.reason}</p>{item.payload.observed_outcome && <p>Observed outcome: {item.payload.observed_outcome}</p>}</article>)}</section>
    </>;
  }

  async function saveChoice(saved: DecisionReport) {
    if (!meeting) return;
    await act("Saving your choice", async () => {
      const result = await sendCommand<OutcomeRecord>(`choice:${meeting.id}:${saved.id}`, `/api/meetings/${meeting.id}/outcomes`, outcomePayload(saved, ""));
      setOutcomes(current => [...current, result]); setNotice("Your choice is saved separately from the recommendation.");
    });
  }
  async function saveAssessment() {
    const target = assessmentTargets.find((_, index) => String(index) === assessmentTarget);
    if (!target || !runId || !currentRun || !report || attestation.trim().length < 20) return;
    await act("Saving your scoped evidence attestation", async () => {
      await sendCommand(`assessment:${runId}:${target.target_hash}:${currentRun.version}`, `/api/runs/${runId}/assessments`, {
        expected_run_version: currentRun.version, target_type: target.target_type, option_id: target.option_id,
        requirement_id: target.requirement_id, target_hash: target.target_hash, reference_ids: target.reference_ids,
        result: target.target_type === "option_costs" ? "supported" : assessmentResult,
        attestation: attestation.trim(), valid_until: assessmentUntil || null,
      });
      setReadEpoch(value => value + 1);
      setNotice("Your attestation was saved with its exact scope. The updated report retains it as your assessment.");
    });
  }
  async function saveOutcome(saved: DecisionReport) {
    if (!meeting) return;
    await act("Saving the observed outcome", async () => {
      const result = await sendCommand<OutcomeRecord>(`outcome:${meeting.id}:${saved.id}`, `/api/meetings/${meeting.id}/outcomes`, outcomePayload(saved, outcomeNote));
      setOutcomes(current => [...current, result]);
      setNotice("Outcome saved. The original report and its assumptions are preserved.");
    });
  }

  function validChoice(saved: DecisionReport) {
    return !!snapshot?.brief && snapshot.report?.id === saved.id && (choiceOption === "defer" || saved.comparison.some(item => item.option_id === choiceOption));
  }

  function outcomePayload(saved: DecisionReport, observed: string) {
    const selected = saved.comparison.find(item => item.option_id === choiceOption);
    if (!snapshot?.brief || snapshot.report?.id !== saved.id || choiceOption !== "defer" && !selected) throw new ApiError("The saved brief and a selection are required before recording your choice.");
    return { run_id: saved.run_id, report_version: saved.version,
      choice: choiceOption === "defer" ? "deferred" : choiceOption === saved.recommendation?.option_id ? "accepted" : "modified",
      reason: `${selected ? `Selected ${selected.label}.` : "Decision deferred."} ${choiceNote}`.trim(),
      success_criterion: snapshot.brief.goal.slice(0, 500), observed_outcome: observed, revisit_date: null };
  }

  return <div className="live-app">
    <a className="live-skip" href="#live-main">Skip to comparison</a>
    <aside className="live-sidebar"><a className="live-brand" href="/">◉ <span>round robin<small>Make the next move clearer.</small></span></a><button type="button" className="live-primary" disabled={blocked} onClick={reset}>＋ New comparison</button><span className="live-eyebrow">Your saved decisions</span><nav aria-label="Saved comparisons">{meetings.length === 0 && <p className="live-muted">Your comparisons will appear here after you save a draft.</p>}{meetings.map(item => <button type="button" aria-current={meeting?.id === item.id ? "page" : undefined} key={item.id} disabled={blocked} onClick={() => { if (canLeaveDraft()) selectMeeting(item); }}><strong>{item.title}</strong><small>Revision {item.draft_revision} · {item.runs.length} saved {item.runs.length === 1 ? "review" : "reviews"}</small></button>)}{nextMeetingCursor && <button type="button" disabled={blocked} onClick={() => void act("Reading more saved comparisons", async () => { const more = await readApi<{ items: MeetingView[]; next_cursor: string | null }>(`/api/meetings?cursor=${encodeURIComponent(nextMeetingCursor)}`); setMeetings(current => [...current, ...more.items.filter(item => !current.some(saved => saved.id === item.id))]); setNextMeetingCursor(more.next_cursor); })}>Load more comparisons</button>}</nav><div className="live-sidebar-foot"><a href="/">Open the fictional sample ↗</a><p>{sessionProfile === "invited_hosted" ? "Invited workspace" : "Local workspace"}<br />{liveAvailable ? "Live review requires explicit preflight." : "Paid provider access is locked."}</p></div></aside>
    <div className="live-body"><header className="live-topbar"><span><span className={`live-connection ${connected ? "on" : "off"}`} aria-hidden="true" />{connected === null ? "Connecting to the local service" : connected ? sessionProfile === "invited_hosted" ? "Workspace connected" : "Local service connected" : "Connection unavailable"}</span><span>{displayMode === "simulated" ? "Simulated adviser review · $0 provider spend" : report ? `Live review · ${report.usage.actual_cost === null ? "cost unconfirmed" : money(report.usage.actual_cost)}` : savedStateUnavailable || ledgerUnavailable ? "Live review · cost may be unconfirmed" : "Live review · explicit authorization required"}</span>{sessionProfile === "invited_hosted" && <button type="button" className="live-link" disabled={blocked} onClick={() => void act("Signing out", async () => { await signOut(); window.location.assign("/live"); })}>Sign out</button>}</header>
      <main id="live-main" className="live-main"><div className="live-page-heading"><div><span className="live-eyebrow">Project option comparison</span><h1>{meeting?.title || "Make a considered next move."}</h1><p>Save your facts, compare the approaches and keep the evidence behind your decision.</p></div>{meeting && runId && <button type="button" className="live-secondary" disabled={blocked || running} onClick={() => { editorScopeGeneration.current += 1; setAutosaveStatus("idle"); setEditor(!editor); setPreflight(null); }}> {editor ? "View saved review" : "Edit draft"}</button>}</div>
      <div className="live-simulation">{displayMode === "simulated" ? <><strong>Adviser responses are simulated.</strong> Your drafts and reports are stored by the service. No external provider is contacted.</> : <><strong>Live provider review.</strong> Starting sends the displayed frozen inputs to the named providers within the authorized allowance.</>} A useful result still depends on checking the supporting facts.</div>{signInRequired && <section className="live-alert"><strong>Sign in to continue.</strong><p>Previously submitted actions are retained for reconciliation. Signing in never starts another review.</p><a href={`${API_URL}/auth/login`}>Sign in to the invited workspace</a></section>}
      {connectionNote && <p className="live-alert" role="status">{connectionNote}</p>}
      {error && <div className="live-error" role="alert"><strong>The action needs attention.</strong><p>{error}</p>{!connected && !pending && <button type="button" className="live-secondary" onClick={() => void act("Reconnecting", initialize)}>Reconnect to local service</button>}</div>}
      {notice && <p className="live-notice" role="status">{notice}</p>}
      {pending && <section className="live-alert"><strong>{pending.intent.startsWith("cancel:") ? "Stop not confirmed" : "Action not confirmed"}</strong><p>The original action receipt is retained in this tab. Confirm its saved outcome before submitting another action.</p><div className="live-actions"><button type="button" className="live-secondary" disabled={!!busy} onClick={() => void recover("reconcile")}>Reconcile pending action</button><button type="button" className="live-link" disabled={!!busy} onClick={() => void recover("retry")}>Retry the same action</button><button type="button" className="live-link" disabled={!!busy} onClick={() => void recover("abandon")}>Abandon unconfirmed action</button></div><p>Abandoning prevents an uncommitted action from executing later. An already saved action is recovered instead.</p></section>}
      {busy && <p className="live-progress" role="status">{busy}…</p>}

      {editor && <form onSubmit={event => void save(event)} className="live-editor"><section className="live-card"><span className="live-eyebrow">01 · Frame the decision</span><h2>What are you comparing?</h2><div className="live-form-grid"><label className="live-span">Comparison title<input required maxLength={200} value={draft.title} onChange={event => edit({ title: event.target.value })} placeholder="For example, replace the workshop label system" disabled={blocked} /></label><label className="live-span">The decision you need to make<textarea required maxLength={4000} value={draft.question} onChange={event => edit({ question: event.target.value })} placeholder="Which approach best fits your budget, deadline and practical constraints?" disabled={blocked} /></label><label className="live-span">What a good result must achieve<input required maxLength={4000} value={draft.goal} onChange={event => edit({ goal: event.target.value })} disabled={blocked} /></label><label>Budget ({draft.currency})<input inputMode="decimal" pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?" value={draft.budget ?? ""} onChange={event => setBudget(event.target.value)} placeholder="Unknown" disabled={blocked} /></label><label>Common cost horizon (months)<input required type="number" min={1} max={120} value={draft.horizon_months ?? ""} onChange={event => edit({ horizon_months: event.target.value ? Number(event.target.value) : null })} disabled={blocked} /></label><label>Deadline<input type="date" value={draft.deadline ?? ""} onChange={event => setDeadline(event.target.value)} disabled={blocked} /></label><label>Location or site context<input maxLength={500} value={draft.location ?? ""} onChange={event => edit({ location: event.target.value })} disabled={blocked} /></label>{draft.hard_requirements.some(item => item.metric !== "manual" && item.id !== "requirement_budget") && <section className="live-span"><h3>Saved numeric requirements</h3><ul>{draft.hard_requirements.filter(item => item.metric !== "manual" && item.id !== "requirement_budget").map(item => <li key={item.id}>{item.description} · {item.metric === "total_cost" ? "Total cost" : "Lead time"} {({ lte: "at most", gte: "at least", eq: "exactly", manual: "" })[item.operator]} {item.threshold} {item.metric === "total_cost" ? draft.currency : "days"}</li>)}</ul><p>These saved numeric checks are preserved when you edit the manual requirements below.</p></section>}<label className="live-span">Hard requirements — one per line<textarea ref={requirementsInput} maxLength={10019} value={requirementsText} aria-invalid={!!requirementsError} aria-describedby="live-requirements-help" onChange={event => editRequirements(event.target.value)} placeholder="For example, keep the business open during installation" disabled={blocked} /><span id="live-requirements-help" role={requirementsError ? "alert" : undefined}>{requirementsError || `Up to ${requirementLimit} manual requirements, each within 500 characters.`}</span></label><label className="live-span">Preferences — highest priority first<textarea ref={preferencesInput} maxLength={5009} value={preferencesText} aria-invalid={!!preferencesError} aria-describedby="live-preferences-help" onChange={event => { setPreferencesText(event.target.value); edit({ preferences: event.target.value.split("\n").filter(line => line.trim()).map((description, i) => ({ id: `preference_${i}`, description, rank: i + 1, origin: "user_supplied" })) }); }} placeholder="For example, lower upfront payment, then simpler maintenance" disabled={blocked} /><span id="live-preferences-help" role={preferencesError ? "alert" : undefined}>{preferencesError || "Up to 10 preferences, each within 500 characters."}</span></label></div></section>
      <section className="live-card"><span className="live-eyebrow">02 · Put the alternatives side by side</span><h2>Options and current approach</h2><p>Enter two to four candidate options plus your status quo. A blank amount means unknown. Enter 0 only when a cost is explicitly zero.</p><div className="live-option-grid">{draft.options.map((item, index) => <fieldset key={item.id} className="live-option"><legend>{item.role === "status_quo" ? "Status quo" : `Candidate ${draft.options.slice(0, index + 1).filter(o => o.role === "candidate").length}`}</legend><label>Approach name<input required maxLength={500} value={item.label} onChange={event => editOption(index, event.target.value)} disabled={blocked} /></label>{item.cost_components.map((cost, componentIndex) => <div key={cost.id}><label>{cost.kind === "recurring" ? `Monthly recurring cost (${cost.currency})` : `${cost.label} (${cost.currency})`}<input inputMode="decimal" pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?" value={cost.amount ?? ""} onChange={event => editCost(index, componentIndex, event.target.value)} placeholder="Unknown" disabled={blocked || cost.status !== "included"} /></label><label>{cost.label} treatment<select value={cost.status} onChange={event => editCostStatus(index, componentIndex, event.target.value as typeof cost.status)} disabled={blocked}><option value="included">Include this cost</option><option value="excluded">Excluded from the quote — unresolved cost</option><option value="not_applicable">Not applicable — omit from total</option></select></label>{cost.status !== "included" && <small>{cost.status === "not_applicable" ? "This component is omitted from the total. Choose Include this cost before entering an applicable price." : "This component is an unresolved cost. Choose Include this cost when its price is supplied."}</small>}</div>)}{item.role === "candidate" && draft.options.filter(o => o.role === "candidate").length > 2 && <button type="button" className="live-link" disabled={blocked} onClick={() => edit({ options: draft.options.filter((_, i) => i !== index) })}>Remove this candidate</button>}</fieldset>)}</div><button type="button" className="live-secondary" disabled={blocked || draft.options.filter(item => item.role === "candidate").length >= 4} onClick={() => { const id = `candidate_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`; const items = [...draft.options]; const baseline = items.pop()!; edit({ options: [...items, option(id, "Another approach", "candidate", draft.currency), baseline] }); }}>＋ Add candidate option</button></section>
      <section className="live-card"><span className="live-eyebrow">03 · Keep the source conditions</span><label>Review execution<select value={selectedMode} disabled={blocked} onChange={event => { setSelectedMode(event.target.value as "simulated" | "live"); setPreflight(null); setConsent(false); }}><option value="simulated">Simulated review — no provider spending</option>{(liveAvailable || selectedMode === "live") && <option value="live" disabled={!liveAvailable}>Live provider review — explicit allowance required</option>}</select></label><h2>Quotes, facts and remaining gaps</h2><label>Selected supporting text<textarea maxLength={12000} rows={5} value={draft.background ?? ""} onChange={event => edit({ background: event.target.value })} placeholder="Paste relevant quote terms, dates, exclusions and what still needs checking. This text is private to your local comparison." disabled={blocked} /></label><label>Approved public research terms — optional, one per line<textarea ref={publicTermsInput} value={publicTermsText} maxLength={1000} disabled={blocked || clarifying} onChange={event => { const text = event.target.value; const terms = text.split("\n").filter(line => line.trim()); setPublicTermsText(text); edit({ public_research_terms: terms.map((term, index) => ({ id: `public_term_${index}`, text: term })) }); }} placeholder="Use only phrases you explicitly approve for public search." /></label><p>Leave this blank to keep public research off. Only these approved terms may be sent to public search. Private supporting text and your site/location field are never copied into a query automatically. Selected run inputs use the declared shared three-adviser scope.</p><label>Upload selected text or Markdown<input type="file" accept=".txt,.md,text/plain,text/markdown" disabled={blocked || !meeting || dirty || clarifying} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void upload(file); }} /></label><p>Save the draft first. Up to five UTF-8 files, one MiB each. Only ready, explicitly selected files are included.</p>{uploads.map(item => <label key={item.id} className="live-consent"><input type="checkbox" checked={draft.selected_attachment_ids.includes(item.id)} disabled={blocked || item.status !== "ready" || clarifying} onChange={event => edit({ selected_attachment_ids: event.target.checked ? [...draft.selected_attachment_ids, item.id] : draft.selected_attachment_ids.filter(id => id !== item.id) })} />{item.filename} · {words(item.status)} · {item.byte_size} bytes · shared three-adviser scope</label>)}<div className="live-actions"><button className="live-primary" type="submit" disabled={blocked || autosaveStatus === "saving"}>{clarifying ? "Confirm updated brief" : parentRunId ? "Save linked follow-up draft" : "Save draft"}</button><span role="status">{autosaveStatus === "saving" ? "Saving changes…" : autosaveStatus === "error" ? "Save not confirmed; edits retained in this tab" : dirty ? "Unsaved changes" : meeting ? `Saved revision ${meeting.draft_revision}` : "Not saved yet"}</span>{!clarifying && <button className="live-secondary" type="button" disabled={blocked || !meeting || dirty} onClick={() => void reviewPreflight()}>Review preflight</button>}</div></section></form>}

      {preflight && <section className="live-card live-preflight"><span className="live-eyebrow">Before the review starts</span><h2>Check the recipients, input and allowance.</h2><p>This preflight is bound to saved draft revision {preflight.draft_revision}. It expires {when(preflight.expires_at)} (Arizona time).</p><div className="live-two"><div><h3>Who receives the selected input</h3><ul>{preflight.recipients.map(provider => <li key={provider}>{PROVIDERS[provider] ?? provider} · {preflight.mode === "simulated" ? "simulated adapter" : String((preflight.configuration.models as Record<string, unknown> | undefined)?.[provider] ?? "configured model")}</li>)}</ul>{preflight.approved_public_terms.length ? <><h3>Explicitly approved for public search</h3><ul>{preflight.approved_public_terms.map(term => <li key={term.id}>{term.text}</li>)}</ul><p>Only these terms and server query templates may be used; provider/search funding remains separately locked.</p></> : <p>No public research is authorized in this review.</p>}</div><div><h3>Bounded work</h3><p>{words(preflight.protocol)}</p><dl>{Object.entries(preflight.limits).map(([key, value]) => <div key={key}><dt>{words(key)}</dt><dd>{String(value)}</dd></div>)}</dl><p>{preflight.mode === "simulated" ? "This execution uses simulated responses and no paid provider access." : `Live requests are bounded by the displayed allowance; unknown charges remain counted. Paid access ${preflight.paid_access_enabled ? "is configured" : "is currently blocked"}.`}</p></div></div>{preflight.parent_run_id && <section className="live-evidence"><h3>Choose earlier evidence and claims to reuse</h3><p>Select only the exact saved versions needed for this follow-up. Nothing is imported automatically. Time-sensitive inputs are rechecked against the freshness policy. At most 24 items may be selected.</p>{parentCandidates.map(item => <label className="live-consent" key={item.id}><input type="checkbox" checked={selectedParentItems.some(selected => selected.id === item.id)} disabled={blocked || selectedParentItems.length >= 24 && !selectedParentItems.some(selected => selected.id === item.id)} onChange={event => { setSelectedParentItems(current => event.target.checked ? [...current, { kind: item.kind, id: item.id, version: item.version, sha256: item.sha256 }] : current.filter(selected => selected.id !== item.id)); setReuseDirty(true); setConsent(false); }} /><span>{item.kind === "claim" ? "Claim" : "Evidence"} · {item.label}<small>Version {item.version} · {words(item.status)} · recorded {when(item.verifiedAt)}{item.scope && ` · ${item.scope}`}</small></span></label>)}{parentSourceCursor && <button type="button" className="live-link" disabled={blocked} onClick={() => void act("Reading earlier evidence", async () => { const more = await readApi<{ items: EvidencePassage[]; next_cursor: string | null }>(`/api/runs/${preflight.parent_run_id}/sources?cursor=${encodeURIComponent(parentSourceCursor)}`); setParentCandidates(current => [...current, ...more.items.filter(item => !current.some(saved => saved.id === item.id)).map(item => ({ kind: "evidence" as const, id: item.id, version: 1, sha256: item.sha256, label: `${item.source_title}: ${item.text.slice(0, 400)}`, status: item.verification_status, verifiedAt: item.verified_at }))]); setParentSourceCursor(more.next_cursor); })}>Load more parent evidence</button>}<p>{preflight.reuse_manifest?.length ?? 0} saved parent items are included in this displayed preflight.</p>{reuseDirty && <p className="live-alert">Refresh the preflight to bind your changed selection before starting.</p>}<button type="button" className="live-secondary" disabled={blocked || !reuseDirty} onClick={() => void reviewPreflight()}>Refresh preflight with selected parent items</button></section>}{preflight.parent_replacement && <p className="live-alert">Starting this follow-up will stop parent review {String(preflight.parent_replacement.run_id)} at version {String(preflight.parent_replacement.version)}. Already submitted provider work may still bill; saved history and unknown charges remain preserved.</p>}{preflight.warnings.map(line => <p className="live-alert" key={line}>{line}</p>)}{preflight.blockers.map(line => <p className="live-error" key={line}>{line}</p>)}{expired && <p role="alert" className="live-error">This preflight expired. Review a fresh preflight before starting.</p>}<label className="live-consent"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} disabled={blocked || expired} />I understand the input scope and limits, and want to start this {preflight.mode === "simulated" ? "simulated" : "live"} review.</label><div className="live-actions"><button type="button" className="live-primary" disabled={blocked || !consent || expired || reuseDirty || preflight.blockers.length > 0} onClick={() => void start()}>{preflight.parent_replacement ? "Stop parent and start follow-up" : `Start ${preflight.mode === "simulated" ? "simulated" : "live"} review`}</button><button type="button" className="live-secondary" disabled={blocked} onClick={() => { setPreflight(null); setEditor(true); }}>Return to draft</button></div></section>}

      {!editor && !preflight && runId && <>
        <section className="live-runbar"><div><span className="live-eyebrow">Saved review</span><strong>{currentRun ? words(currentRun.status) : "Reading saved state"}</strong><span>{currentRun ? words(currentRun.stage) : ""}</span></div><div className="live-actions">{running && <button type="button" className="live-secondary" disabled={blocked} onClick={() => void stop()}>Stop review</button>}{report && (["json", "markdown", "pdf"] as const).map(format => <button type="button" className="live-secondary" disabled={blocked} key={format} onClick={() => void act(`Preparing ${format.toUpperCase()} export`, () => downloadSavedExport(runId, report.id, report.version, format))}>Export {format === "markdown" ? "Markdown" : format.toUpperCase()}</button>)}</div></section>
        {(savedStateUnavailable || ledgerUnavailable) && <section className="live-alert" role="alert"><h2>The current report is unavailable.</h2>{savedStateUnavailable && <p>Saved review state could not be validated, so the current report is unavailable.</p>}{ledgerUnavailable && <p>Saved usage could not be validated, so the current report and remaining request count are unavailable.</p>}<p>{currentRun?.execution_mode === "live" ? "Already submitted work may still incur charges." : "This review is labeled simulated."}</p></section>}
        {snapshot?.evidence === null && <section className="live-alert" role="alert"><h2>{ledgerUnavailable || savedStateUnavailable ? "Stored evidence is unavailable." : "Stored evidence is unavailable in this report."}</h2><p>{ledgerUnavailable || savedStateUnavailable ? "The stored evidence could not be validated. The original records are preserved for review." : "It could not be validated when the report was created. The original records are preserved for review."}</p></section>}
        {snapshot && !snapshot.brief && <section className="live-alert" role="alert"><h2>Saved review inputs are unavailable.</h2><p>The saved inputs could not be validated. Any report shown below is a saved record and does not validate those inputs. Unresolved charges remain counted. Start a new comparison with reviewed inputs to continue.</p></section>}
        {currentRun?.status === "awaiting_input" && <section className="live-alert"><h2>Your input is needed before the review can continue.</h2><ul>{snapshot?.questions.map((question, index) => <li key={index}>{typeof question === "string" ? question : question && typeof question === "object" ? String((question as Record<string, unknown>).question ?? (question as Record<string, unknown>).text ?? "Confirm the missing brief information.") : "Confirm the missing brief information."}</li>)}</ul><button type="button" className="live-secondary" disabled={blocked || !snapshot?.brief} onClick={clarify}>Answer and confirm brief</button></section>}{snapshot?.retry_eligible && <section className="live-alert"><strong>A final review stage can be retried within the original limits.</strong><div className="live-actions">{snapshot.retryable_stages.map(stage => <button type="button" key={stage} className="live-secondary" disabled={blocked} onClick={() => void retryStage(stage)}>Retry {stage}</button>)}<button type="button" className="live-link" disabled={blocked} onClick={() => void stop(true)}>Discard remaining retry</button></div></section>}<div role="tablist" aria-label="Saved review views" className="live-tabs">{(["overview", "evidence", "activity"] as const).map((value, index) => <button key={value} type="button" role="tab" id={`live-tab-${value}`} aria-selected={tab === value} aria-controls={`live-panel-${value}`} tabIndex={tab === value ? 0 : -1} onClick={() => setTab(value)} onKeyDown={event => { if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); const tabs: Tab[] = ["overview", "evidence", "activity"]; const next = tabs[(index + (event.key === "ArrowRight" ? 1 : 2)) % 3]; setTab(next); document.getElementById(`live-tab-${next}`)?.focus(); } }}>{value[0].toUpperCase() + value.slice(1)}</button>)}</div>
        {currentRun?.status === "awaiting_input" && !!snapshot?.proposed_changes?.length && <section className="live-card"><h2>Suggested brief changes to review</h2><p>Each suggestion needs your confirmation in the brief editor.</p>{snapshot.proposed_changes.map(change => <article className="live-evidence" key={change.field}><strong>{words(change.field)}</strong><p>{change.reason}</p><pre>{change.proposed_value_json}</pre><button type="button" className="live-secondary" disabled={blocked || !snapshot?.brief} onClick={() => reviewIntakeChange(change)}>Review suggested {words(change.field)}</button></article>)}</section>}
        <section role="tabpanel" id={`live-panel-${tab}`} aria-labelledby={`live-tab-${tab}`} className="live-report-panel">
          {tab === "overview" && (report ? reportPanel(report) : ledgerUnavailable || savedStateUnavailable ? null : <section className="live-card"><h2>{running ? "The review is in progress." : "The saved report is being prepared."}</h2><p>{running ? "Independent proposals, checked calculations and review are recorded as they commit. You can leave and reopen this saved review." : "No recommendation is displayed until a report has been saved."}</p><button type="button" className="live-link" onClick={() => setTab("activity")}>Inspect committed activity</button></section>)}
          {tab === "evidence" && <section className="live-card"><h2>Follow each fact back to its basis.</h2><p>Supplied facts are labeled separately from calculated arithmetic. A calculation cannot establish that a supplier quote is true.</p>{snapshot?.evidence === null ? <p>{ledgerUnavailable || savedStateUnavailable ? "Stored evidence could not be validated. The original records are preserved for review." : "Stored evidence is unavailable in this report because it could not be validated when the report was created. The original records are preserved for review."}</p> : snapshot?.evidence?.length ? snapshot.evidence.map(item => <article className="live-evidence" key={item.id}><div><strong>{item.source_title}</strong><span className="live-pill">{words(item.verification_status)}</span></div><p>{item.text}</p>{item.source_text_length != null && <p>Selected excerpt from {item.source_text_length.toLocaleString()} source characters. {item.uninspected_characters?.toLocaleString() ?? "Unknown"} characters remain outside the selected excerpts.</p>}<small>{item.scope} · {when(item.verified_at)}</small><button type="button" className="live-link" onClick={() => void inspect("passage", item.id)}>Open exact passage & provenance</button></article>) : <p>No evidence passages have been committed yet.</p>}{report?.essential_dependencies.map(item => <article className="live-evidence" key={item.id}><strong>{item.description}</strong><p>{words(item.status)}</p></article>)}</section>}
          {tab === "activity" && <section className="live-card"><h2>The saved review, as it happened.</h2><p>Only committed events appear here. Reconnecting reads this same history.</p><ol className="live-timeline">{snapshot?.events.map((item, index) => <li key={item.sequence ?? index}><span className="live-eyebrow">{item.sequence ?? index + 1} · {item.timestamp ? when(item.timestamp) : "Saved event"}</span><strong>{words(item.event_type ?? "progress")}</strong>{item.payload && <p>{String(item.payload.message ?? item.payload.stage ?? item.payload.reason ?? item.payload.status ?? "Committed to the saved review.")}</p>}</li>)}</ol>{report?.coverage.map(item => <p key={`${item.provider}:${item.role}`}><strong>{PROVIDERS[item.provider] ?? item.provider}</strong> · {item.role} · {words(item.status)}<small>{item.notes}</small></p>)}</section>}
          {tab === "evidence" && report && assessmentTargets.length > 0 && <section className="live-card"><h2>Record your evidence check</h2><p>Your attestation applies only to the selected costs or requirement and the cited passages. It remains labeled as your assessment. Recording it creates a new report version.</p><label>Scope of your check<select value={assessmentTarget} disabled={blocked} onChange={event => setAssessmentTarget(event.target.value)}><option value="">Select the exact scope</option>{assessmentTargets.map((target, index) => <option key={target.target_hash} value={index}>{target.label}</option>)}</select></label>{assessmentTargets.find((_, index) => String(index) === assessmentTarget)?.target_type === "requirement" && <label>Requirement result<select value={assessmentResult} onChange={event => setAssessmentResult(event.target.value)}><option value="pass">Pass</option><option value="fail">Fail</option><option value="unknown">Unknown</option><option value="not_applicable">Not applicable</option></select></label>}<label>What you checked and its limits<textarea maxLength={2000} value={attestation} disabled={blocked} onChange={event => setAttestation(event.target.value)} placeholder="Identify what you checked in the saved evidence, its source and any limits." /></label><label>Valid through<input type="date" value={assessmentUntil} disabled={blocked} onChange={event => setAssessmentUntil(event.target.value)} /></label><button type="button" className="live-secondary" disabled={blocked || !assessmentTarget || attestation.trim().length < 20} onClick={() => void saveAssessment()}>Save my scoped attestation</button></section>}
        </section>
      </>}
      {meeting && meeting.runs.length > 0 && <section className="live-history"><h2>Review history</h2><div>{meeting.runs.map(item => <button type="button" className="live-secondary" disabled={blocked} key={item.id} onClick={() => { editorScopeGeneration.current += 1; setAutosaveStatus("idle"); setChoiceOption(""); setChoiceNote(""); setOutcomeNote(""); setRunId(item.id); setReadEpoch(value => value + 1); setEditor(false); setPreflight(null); setSnapshot(null); setTab("overview"); sessionStorage.setItem(CURRENT_KEY, JSON.stringify({ meetingId: meeting.id, runId: item.id })); }}><strong>{when(item.created_at)}</strong><small>{words(item.status)}{item.parent_run_id ? " · linked follow-up" : ""}</small></button>)}</div>{meeting.next_run_cursor && <button type="button" className="live-link" disabled={blocked} onClick={() => void loadOlderRuns()}>Load earlier reviews</button>}</section>}
      <footer className="live-footer"><span>{sessionProfile === "invited_hosted" ? "Saved in your invited Round Robin workspace." : "Saved locally by your Round Robin service."}</span><details><summary>Connection details</summary><code>{API_URL}</code><p>Use the service on this device. Provider keys belong on the server.</p></details></footer>
      </main>
    </div>
    <dialog ref={dialog} className="live-dialog" aria-labelledby="live-inspection-heading" onClose={() => setInspection(null)} onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}><div><div className="live-dialog-heading"><h2 id="live-inspection-heading">{inspection?.title}</h2><button type="button" className="live-secondary" onClick={() => dialog.current?.close()}>Close</button></div>{inspection?.passage && <><blockquote>{inspection.passage.text}</blockquote><dl><dt>Source status</dt><dd>{words(inspection.passage.verification_status)}</dd><dt>Exact saved passage</dt><dd>{inspection.passage.locator}</dd><dt>Scope</dt><dd>{inspection.passage.scope}</dd><dt>Checked or recorded</dt><dd>{when(inspection.passage.verified_at)}</dd><dt>Passage SHA-256</dt><dd><code>{inspection.passage.sha256}</code></dd></dl></>}{inspection?.calculation && <><p className="live-formula">{inspection.calculation.formula}</p><p><strong>{inspection.calculation.output ?? "Unknown"} {inspection.calculation.unit}</strong></p>{inspection.calculation.output === null && inspection.calculation.known_subtotal !== null && <p>Known subtotal: {inspection.calculation.known_subtotal} {inspection.calculation.unit}. Missing components prevent an all-in total.</p>}<ul>{inspection.calculation.inputs.map((input, index) => <li key={`${input.source_ref}:${index}`}>{input.label}: {input.amount} {input.unit}</li>)}</ul><p>{inspection.calculation.rounding}</p>{inspection.calculation.missing_components.map(line => <p key={line}>Unknown: {line}</p>)}</>}{inspection?.text && <><pre className="live-copy-text">{inspection.text}</pre><button type="button" className="live-primary" onClick={() => void navigator.clipboard.writeText(inspection.text!).then(() => setNotice("Request copied."), () => setNotice("Select the request text to copy it."))}>Copy request</button></>}</div></dialog>
  </div>;
}
