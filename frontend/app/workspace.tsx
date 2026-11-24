"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  sample,
  validateDraft,
  type Calculation,
  type DecisionReport,
  type DraftBrief,
  type EvidencePassage,
  type Scenario,
} from "./contracts";

type View = "report" | "draft" | "preflight" | "running";
type Tab = "overview" | "evidence" | "activity";
type Inspection =
  | { kind: "passage"; value: EvidencePassage }
  | { kind: "calculation"; value: Calculation }
  | { kind: "request"; text: string }
  | { kind: "readiness"; report: DecisionReport };
type PreviewQuote = {
  scenarioId: string;
  draft: string;
  revision: number;
  expiresAt: number;
  consumed: boolean;
  commandId: string;
};
const STORAGE_KEY = "round-robin:fictional-walkthrough:v1";
const providerNames = {
  openai: "OpenAI",
  anthropic: "Anthropic Claude",
  google: "Google Gemini",
};
const readinessLabels = {
  ready_to_choose: "Ready within this fictional scope",
  conditional: "Conditional decision",
  no_feasible_option: "No feasible option",
  insufficient_evidence: "More evidence needed",
};
const clone = <T,>(value: T): T => structuredClone(value);
const equivalent = (a: DraftBrief, b: DraftBrief) =>
  JSON.stringify({ ...a, revision: 0 }) ===
  JSON.stringify({ ...b, revision: 0 });

function evidenceForReport(report: DecisionReport, brief: DraftBrief) {
  const references = new Set([
    ...brief.options.flatMap((option) =>
      option.cost_components.map((component) => component.source_ref),
    ),
    ...report.essential_dependencies.flatMap(
      (dependency) => dependency.reference_ids,
    ),
    ...report.calculations.flatMap((calculation) =>
      calculation.inputs.map((input) => input.source_ref),
    ),
  ]);
  return sample.evidence.filter((passage) => references.has(passage.id));
}

function Icon({
  name,
  ...props
}: {
  name: string;
  className?: string;
  "aria-hidden"?: boolean;
}) {
  const paths: Record<string, ReactNode> = {
    plus: <path d="M12 5v14M5 12h14" />,
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    doc: (
      <>
        <path d="M14 2H5v20h14V7zM14 2v6h5M8 12h8M8 16h6" />
      </>
    ),
    arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
    download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
    edit: (
      <>
        <path d="m14 4 6 6M4 20l5-1L21 7l-5-5L4 14z" />
      </>
    ),
    shield: (
      <>
        <path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6z" />
        <path d="m8 11 3 3 5-6" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 6v6l4 2" />
      </>
    ),
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 10v7M12 6v1" />
      </>
    ),
    copy: (
      <>
        <rect x="8" y="8" width="12" height="13" rx="2" />
        <path d="M15 8V3H3v13h5" />
      </>
    ),
  };
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name] ?? paths.doc}
    </svg>
  );
}

function money(value: string | null | undefined, currency = "USD") {
  if (value == null) return "Unknown";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function downloadReport(
  report: DecisionReport,
  format: "json" | "md",
  brief: DraftBrief,
) {
  const text =
    format === "json"
      ? JSON.stringify(report, null, 2)
      : [
          `# ${brief.title}`,
          "",
          "**Fictional sample — simulated contributions. No provider calls or measured product results.**",
          "",
          `Report: ${report.id}, version ${report.version}, event cutoff ${report.event_cutoff}`,
          `Execution: ${report.execution_status}. Readiness: ${report.readiness ?? "unavailable"}.`,
          "",
          report.recommendation?.summary ??
            report.readiness_unavailable_reason ??
            "No recommendation established.",
          ...(report.recommendation?.conditions ?? []).map(
            (v) => `Condition: ${v}`,
          ),
          "",
          ...report.readiness_reasons.map((v) => `- ${v}`),
          "",
          ...report.comparison.map(
            (v) =>
              `- ${v.label}: ${money(v.total, v.currency)} over ${v.horizon_months} months. Known subtotal: ${money(v.known_subtotal, v.currency)}. ${v.caveat} ${v.constraint_results.map((c) => `${c.requirement_id}: ${c.result} (${c.reason}; references ${c.basis_refs.join(", ")})`).join("; ")}`,
          ),
          "",
          `Tradeoff: ${report.tradeoff}`,
          `Largest uncertainty: ${report.largest_uncertainty}`,
          `What changes the answer: ${report.what_would_change_answer}`,
          "",
          `Next action: ${report.next_action}`,
          "",
          ...report.next_evidence.map(
            (v) => `${v.question}\n${v.verification_method}\n${v.request_text}`,
          ),
          "",
          ...report.calculations.map(
            (v) =>
              `${v.label}: ${v.formula} = ${v.output ?? "unknown"} ${v.unit}. ${v.rounding}`,
          ),
          "",
          ...evidenceForReport(report, brief).map(
            (v) =>
              `${v.source_title} (${v.id}, ${v.locator}, SHA256 ${v.sha256}):\n> ${v.text}\nFictional evidence; ${v.scope}`,
          ),
          "",
          `Dissent: ${report.dissent.join("; ") || "None recorded in this fixture."}`,
          `Stopping reason: ${report.stop_reason}`,
          "",
          ...report.coverage.map(
            (v) =>
              `${providerNames[v.provider]}: ${v.role}, ${v.status}. ${v.notes}`,
          ),
        ].join("\n");
  const blob = new Blob([text], {
    type:
      format === "json" ? "application/json" : "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `fictional-${report.id}-v${report.version}.${format}`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Inspector({
  inspection,
  close,
}: {
  inspection: Inspection | null;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [copyStatus, setCopyStatus] = useState("");
  useEffect(() => {
    setCopyStatus("");
    if (inspection && !dialog.current?.open) dialog.current?.showModal();
    else if (!inspection) dialog.current?.close();
  }, [inspection]);
  const title =
    inspection?.kind === "passage"
      ? inspection.value.source_title
      : inspection?.kind === "calculation"
        ? inspection.value.label
        : inspection?.kind === "request"
          ? "Your quote / site-check brief"
          : "Why this readiness?";
  return (
    <dialog ref={dialog} onClose={close} aria-labelledby="inspector-title">
      <div className="dialog-heading">
        <div>
          <p className="eyebrow">Fictional saved record</p>
          <h2 id="inspector-title">{title}</h2>
        </div>
        <button className="close" onClick={close} aria-label="Close details">
          ×
        </button>
      </div>
      <div className="dialog-body">
        {inspection?.kind === "passage" && (
          <>
            <p>Exact stored passage · {inspection.value.locator}</p>
            <blockquote>{inspection.value.text}</blockquote>
            <dl>
              <dt>Origin and scope</dt>
              <dd>
                {inspection.value.origin} · {inspection.value.scope}
              </dd>
              <dt>Snapshot recorded</dt>
              <dd>
                {new Date(inspection.value.verified_at).toLocaleString(
                  "en-US",
                  { timeZone: "America/Phoenix" },
                )}{" "}
                (America/Phoenix). This is a fictional fixture, with no external
                verification.
              </dd>
              <dt>Source version</dt>
              <dd>{inspection.value.source_version}</dd>
              <dt>Passage SHA-256</dt>
              <dd>{inspection.value.sha256}</dd>
              <dt>Record</dt>
              <dd>{inspection.value.id}</dd>
            </dl>
          </>
        )}
        {inspection?.kind === "calculation" && (
          <>
            <p>
              {inspection.value.horizon_months
                ? `${inspection.value.horizon_months}-month common horizon`
                : "Common cost basis"}{" "}
              · Python Decimal precision {inspection.value.decimal_precision}
            </p>
            <pre>
              {inspection.value.formula}
              {"\n"}= {inspection.value.output ?? "Unknown"}{" "}
              {inspection.value.unit}
            </pre>
            <dl>
              <dt>Inputs and provenance</dt>
              {inspection.value.inputs.map((v, i) => (
                <dd key={i}>
                  {v.label}: {v.amount} {v.unit} · {v.source_ref}
                </dd>
              ))}
              <dt>Rounding</dt>
              <dd>{inspection.value.rounding}</dd>
              {inspection.value.missing_components.length > 0 && (
                <>
                  <dt>Missing components</dt>
                  <dd>
                    {inspection.value.missing_components.join(", ")}. Known
                    subtotal: {money(inspection.value.known_subtotal)}. A
                    subtotal is not an all-in total.
                  </dd>
                </>
              )}
              <dt>Calculation ID</dt>
              <dd>{inspection.value.id}</dd>
            </dl>
          </>
        )}
        {inspection?.kind === "request" && (
          <>
            <p>
              Copy this fictional example to see how a decisive unknown becomes
              a practical next step.
            </p>
            <pre>{inspection.text}</pre>
            <button
              className="btn primary"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(inspection.text);
                  setCopyStatus("Copied to clipboard.");
                } catch {
                  setCopyStatus(
                    "Clipboard unavailable. Select and copy the text above.",
                  );
                }
              }}
            >
              <Icon name="copy" />
              Copy brief
            </button>
            <p role="status">{copyStatus}</p>
          </>
        )}
        {inspection?.kind === "readiness" && (
          <>
            <p>
              Readiness is separate from execution status. These judgments are
              fixture inputs to the application rule, with no real provider
              review.
            </p>
            <h3>
              {inspection.report.readiness
                ? readinessLabels[inspection.report.readiness]
                : "Readiness unavailable"}
            </h3>
            <ul>
              {inspection.report.readiness_reasons.map((v) => (
                <li key={v}>{v}</li>
              ))}
            </ul>
            <p>{inspection.report.readiness_unavailable_reason}</p>
            {inspection.report.essential_dependencies.map((v) => (
              <div className="fact-row" key={v.id}>
                <span>{v.description}</span>
                <strong>{v.status}</strong>
              </div>
            ))}
          </>
        )}
      </div>
    </dialog>
  );
}

export default function Workspace() {
  const first = sample.scenarios[0];
  const [scenarioId, setScenarioId] = useState<string>(first.id);
  const [view, setView] = useState<View>("report");
  const [tab, setTab] = useState<Tab>("overview");
  const [draft, setDraft] = useState<DraftBrief>(() => clone(first.brief));
  const [savedDraft, setSavedDraft] = useState<DraftBrief>(() =>
    clone(first.brief),
  );
  const [persist, setPersist] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [quote, setQuote] = useState<PreviewQuote | null>(null);
  const [consent, setConsent] = useState(false);
  const [now, setNow] = useState(0);
  const [step, setStep] = useState(0);
  const [partial, setPartial] = useState(false);
  const [cancelled, setCancelled] = useState(false);
  const [stopRequested, setStopRequested] = useState(false);
  const [savedVariants, setSavedVariants] = useState<
    Record<string, "completed" | "partial" | "cancelled">
  >({});
  const [savedScenarios, setSavedScenarios] = useState<string[]>([first.id]);
  const [ready, setReady] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const scenario = sample.scenarios.find((s) => s.id === scenarioId) ?? first;
  const currentReport = cancelled
    ? scenario.cancelled_report
    : partial
      ? scenario.partial_report
      : scenario.report;
  const report = currentReport ?? scenario.partial_report;
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedDraft);
  const supported = sample.scenarios.find((s) =>
    equivalent(s.brief, savedDraft),
  );
  const parentScenario = sample.scenarios.find(
    (s) => s.report?.id === report.parent_report_id,
  );
  const draftParent = sample.scenarios.find(
    (s) => s.report?.id === supported?.report?.parent_report_id,
  );
  const activeEvidence = evidenceForReport(report, scenario.brief);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const stored: unknown = JSON.parse(raw);
        if (
          typeof stored === "object" &&
          stored !== null &&
          "draft" in stored &&
          validateDraft(stored.draft) &&
          "scenarioId" in stored &&
          typeof stored.scenarioId === "string"
        ) {
          const restored = sample.scenarios.find(
            (s) => s.id === stored.scenarioId,
          );
          if (restored) {
            setDraft(stored.draft);
            setSavedDraft(stored.draft);
            setScenarioId(restored.id);
            setPersist(true);
            setView("draft");
            setFeedback("Reopened the fictional draft saved in this browser.");
          }
        }
      }
    } catch {
      setError(
        "Browser storage is unavailable or contains an invalid draft. Your original sample is available.",
      );
    }
    setReady(true);
  }, []);

  useEffect(() => {
    if (view !== "preflight") return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [view]);

  useEffect(() => {
    if (view !== "running" || stopRequested) return;
    if (step >= 5) {
      setView("report");
      setTab("overview");
      setPartial(false);
      setSavedVariants((variants) => ({
        ...variants,
        [scenarioId]: "completed",
      }));
      setSavedScenarios((ids) =>
        ids.includes(scenarioId) ? ids : [...ids, scenarioId],
      );
      setFeedback(
        "Simulated report saved in this session. No provider requests were made.",
      );
      return;
    }
    const timer = setTimeout(() => setStep((v) => v + 1), 700);
    return () => clearTimeout(timer);
  }, [view, step, scenarioId, stopRequested]);

  useEffect(() => {
    if (!stopRequested || view !== "running") return;
    const timer = setTimeout(() => {
      setCancelled(true);
      setSavedVariants((variants) => ({
        ...variants,
        [scenarioId]: "cancelled",
      }));
      setSavedScenarios((ids) =>
        ids.includes(scenarioId) ? ids : [...ids, scenarioId],
      );
      setPartial(false);
      setStopRequested(false);
      setTab("overview");
      setView("report");
      setFeedback(
        "Simulation cancellation confirmed. Saved fixture work is preserved; recommendation and readiness remain unavailable.",
      );
    }, 900);
    return () => clearTimeout(timer);
  }, [stopRequested, view, scenarioId]);

  useEffect(() => {
    if (ready) heading.current?.focus();
  }, [view]); // Focus the new screen after an explicit navigation.

  function go(next: View) {
    setError("");
    setFeedback("");
    setStopRequested(false);
    setView(next);
  }
  function save() {
    if (!validateDraft(draft)) {
      setError(
        "Please correct the draft. Use plain nonnegative decimal amounts, a title, and a horizon from 1 to 120 months.",
      );
      return false;
    }
    const next = clone(draft);
    next.revision = savedDraft.revision + 1;
    setDraft(next);
    setSavedDraft(next);
    setQuote(null);
    setError("");
    if (persist) {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ draft: next, scenarioId }),
        );
      } catch {
        setError(
          "Draft saved in this session. Browser storage failed; it will not survive a refresh.",
        );
        return false;
      }
    }
    setFeedback(
      persist
        ? "Fictional draft saved in this browser. No inference performed."
        : "Draft saved for this session. No inference performed.",
    );
    return true;
  }
  function chooseFixture(next: Scenario) {
    const nextDraft = clone(next.brief);
    nextDraft.revision = savedDraft.revision + 1;
    setScenarioId(next.id);
    setDraft(nextDraft);
    setSavedDraft(clone(nextDraft));
    setQuote(null);
    setPartial(false);
    setCancelled(false);
    setError("");
    setFeedback(
      "Fictional input scenario selected and saved for this session.",
    );
    if (persist) {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ draft: nextDraft, scenarioId: next.id }),
        );
      } catch {
        setError(
          "Browser storage failed. The scenario is available in this session.",
        );
      }
    }
  }
  function reviewPreflight() {
    if (dirty) {
      setError("Save the current draft before reviewing its preflight.");
      return;
    }
    if (!supported) {
      setError(
        "This offline prototype has fixed fictional reports. Your edits are saved, but have no corresponding report. Select a fictional scenario below to rehearse the review.",
      );
      return;
    }
    setScenarioId(supported.id);
    setConsent(false);
    setQuote({
      scenarioId: supported.id,
      draft: JSON.stringify(savedDraft),
      revision: savedDraft.revision,
      expiresAt: Date.now() + 600000,
      consumed: false,
      commandId: crypto.randomUUID(),
    });
    go("preflight");
  }
  function start() {
    if (
      !consent ||
      !quote ||
      quote.consumed ||
      quote.expiresAt <= Date.now() ||
      quote.draft !== JSON.stringify(savedDraft) ||
      quote.revision !== savedDraft.revision ||
      dirty
    ) {
      setError(
        "This preflight is expired, changed, or already used. Review a fresh preflight to continue.",
      );
      return;
    }
    setQuote({ ...quote, consumed: true });
    setStep(0);
    setPartial(false);
    setCancelled(false);
    setStopRequested(false);
    go("running");
  }
  function followUp() {
    if (scenarioId !== first.id || partial || cancelled) return;
    const changed =
      sample.scenarios.find((s) => s.brief.horizon_months === 24) ?? first;
    chooseFixture(changed);
    go("draft");
  }
  function openSaved(id: string, explicitVariant?: "completed") {
    const variant = explicitVariant ?? savedVariants[id] ?? "completed";
    setScenarioId(id);
    setPartial(variant === "partial");
    setCancelled(variant === "cancelled");
    if (explicitVariant)
      setSavedVariants((variants) => ({ ...variants, [id]: explicitVariant }));
    setTab("overview");
    go("report");
  }
  const inspectCalculation = (id: string) => {
    const value = report.calculations.find((c) => c.id === id);
    if (value) setInspection({ kind: "calculation", value });
  };
  const requestText =
    report.next_evidence[0]?.request_text ?? report.next_action;

  return (
    <>
      <a className="skip" href="#main">
        Skip to decision
      </a>
      <fieldset
        className="hydration-boundary"
        disabled={!ready}
        aria-busy={!ready}
      >
        <legend className="visually-hidden">
          Fictional comparison walkthrough
        </legend>
        <div className="shell">
          <aside className="sidebar" aria-label="Workspace navigation">
            <button
              className="brand"
              onClick={() => openSaved(first.id)}
              aria-label="Round Robin sample home"
            >
              <span className="brand-mark" aria-hidden="true">
                <i />
                <i />
              </span>
              round robin
            </button>
            <button
              className="btn primary"
              onClick={() => {
                go("draft");
              }}
            >
              <Icon name="plus" />
              Edit a comparison
            </button>
            <nav>
              <p className="nav-label">Your workspace</p>
              <button
                className={`nav-item ${view === "report" ? "active" : ""}`}
                onClick={() => openSaved(scenarioId)}
              >
                <Icon name="grid" />
                Decision overview
              </button>
              <button
                className={`nav-item ${view === "draft" ? "active" : ""}`}
                onClick={() => go("draft")}
              >
                <Icon name="edit" />
                Draft brief {dirty && "•"}
              </button>
              <button
                className="nav-item"
                onClick={() => {
                  setTab("evidence");
                  go("report");
                }}
              >
                <Icon name="doc" />
                Evidence library
              </button>
            </nav>
            <div className="recent">
              <p className="nav-label">Saved fictional examples</p>
              {savedScenarios.map((id) => {
                const s = sample.scenarios.find((v) => v.id === id);
                return (
                  s && (
                    <button
                      key={id}
                      className="nav-item"
                      onClick={() => openSaved(id)}
                    >
                      <Icon name="clock" />
                      <span>{s.label}</span>
                    </button>
                  )
                );
              })}
              <p className="small" style={{ padding: "10px 12px" }}>
                Saved in this session
              </p>
            </div>
            <div className="sidebar-note">
              <p>
                Independent perspectives.
                <br />
                One inspectable decision.
              </p>
              <div className="avatars" aria-hidden="true">
                <span className="avatar">O</span>
                <span className="avatar">A</span>
                <span className="avatar">G</span>
              </div>
              <p>
                Phase 0 · Offline prototype
                <br />
                Provider access disabled
              </p>
            </div>
          </aside>
          <div className="workspace">
            <header className="topbar">
              <div className="topbar-left">
                <span>Personal workspace</span>
                <span aria-hidden="true">/</span>
                <span>Project comparison</span>
              </div>
              <div className="topbar-right">
                <span className="small">
                  <span className="dot" />
                  Offline simulation
                </span>
                <span className="avatar" aria-label="Local walkthrough">
                  You
                </span>
              </div>
            </header>
            <main id="main" className="main">
              <div className="notice">
                <Icon name="info" />
                <span>
                  <strong>Fictional sample.</strong> All contributions and
                  evidence are simulated. No provider calls, research, or
                  spending.
                {" "}<a href="/live">Open saved comparisons →</a>
                </span>
              </div>
              <div className="title-row">
                <div>
                  <p className="eyebrow">Project option comparison</p>
                  <h1 ref={heading} tabIndex={-1}>
                    {view === "draft"
                      ? "Start with what matters."
                      : view === "preflight"
                        ? "Review before you begin."
                        : view === "running"
                          ? "A few perspectives. One brief."
                          : scenario.brief.title}
                  </h1>
                  <p className="subtitle">
                    {view === "draft"
                      ? "Clarify the options, keep the constraints, and make the unknowns explicit."
                      : view === "preflight"
                        ? "See exactly what this fictional walkthrough will share and simulate."
                        : view === "running"
                          ? "Walking through saved fixture contributions. No models are running."
                          : "Compare the full cost, the practical tradeoff, and the evidence behind your next move."}
                  </p>
                </div>
                {view === "report" && (
                  <div className="actions">
                    <button
                      className="btn secondary"
                      onClick={() => {
                        const next = clone(scenario.brief);
                        setDraft(next);
                        setSavedDraft(clone(next));
                        go("draft");
                      }}
                    >
                      <Icon name="edit" />
                      Edit brief
                    </button>
                    <button
                      className="btn secondary"
                      onClick={() =>
                        downloadReport(report, "json", scenario.brief)
                      }
                    >
                      <Icon name="download" />
                      Export JSON
                    </button>
                  </div>
                )}
              </div>
              <div aria-live="polite">
                {feedback && (
                  <p className="feedback" role="status">
                    {feedback}
                  </p>
                )}
                {error && (
                  <p className="error" role="alert">
                    {error}
                  </p>
                )}
              </div>

              {view === "report" && (
                <>
                  <div className="meta">
                    <span>
                      {
                        scenario.brief.options.filter(
                          (o) => o.role === "candidate",
                        ).length
                      }{" "}
                      options + status quo
                    </span>
                    <span>
                      {scenario.brief.horizon_months}-month cost horizon
                    </span>
                    <span>Report v{report.version}</span>
                    <span>{report.execution_status.replaceAll("_", " ")}</span>
                  </div>
                  {parentScenario && (
                    <div className="version-banner">
                      <span>
                        Linked fictional follow-up ·{" "}
                        {report.change_summary.join(" ") ||
                          "Changed input: cost horizon. Original report preserved."}
                      </span>
                      <button
                        className="text-btn"
                        onClick={() =>
                          openSaved(parentScenario.id, "completed")
                        }
                      >
                        Open original report →
                      </button>
                    </div>
                  )}
                  <div
                    className="tabs"
                    role="tablist"
                    aria-label="Decision sections"
                  >
                    {(["overview", "evidence", "activity"] as Tab[]).map(
                      (name, index) => (
                        <button
                          id={`tab-${name}`}
                          role="tab"
                          aria-selected={tab === name}
                          aria-controls={`panel-${name}`}
                          tabIndex={tab === name ? 0 : -1}
                          className={`tab ${tab === name ? "active" : ""}`}
                          key={name}
                          onClick={() => setTab(name)}
                          onKeyDown={(event) => {
                            const names: Tab[] = [
                              "overview",
                              "evidence",
                              "activity",
                            ];
                            let next = index;
                            if (event.key === "ArrowRight")
                              next = (index + 1) % 3;
                            else if (event.key === "ArrowLeft")
                              next = (index + 2) % 3;
                            else if (event.key === "Home") next = 0;
                            else if (event.key === "End") next = 2;
                            else return;
                            event.preventDefault();
                            setTab(names[next]);
                            document
                              .getElementById(`tab-${names[next]}`)
                              ?.focus();
                          }}
                        >
                          {name[0].toUpperCase() + name.slice(1)}
                          {name === "evidence" && (
                            <span className="tab-count">
                              {activeEvidence.length +
                                report.calculations.length}
                            </span>
                          )}
                        </button>
                      ),
                    )}
                  </div>
                  <section
                    id={`panel-${tab}`}
                    role="tabpanel"
                    aria-labelledby={`tab-${tab}`}
                  >
                    {tab === "overview" && (
                      <div className="grid">
                        <div className="stack">
                          <section
                            className="card recommendation"
                            aria-label="Recommendation"
                          >
                            <div className="card-header">
                              <span className="eyebrow">
                                The recommendation
                              </span>
                              <button
                                className={`status ${report.readiness ?? "muted"}`}
                                onClick={() =>
                                  setInspection({ kind: "readiness", report })
                                }
                              >
                                {report.readiness
                                  ? readinessLabels[report.readiness]
                                  : "Readiness unavailable"}{" "}
                                ↗
                              </button>
                            </div>
                            <h2>
                              {report.recommendation
                                ? (report.comparison.find(
                                    (o) =>
                                      o.option_id ===
                                      report.recommendation?.option_id,
                                  )?.label ?? "A conditional path forward")
                                : "A choice is not established yet."}
                            </h2>
                            <p>
                              {report.recommendation?.summary ??
                                report.readiness_unavailable_reason}
                            </p>
                            <ul className="reasons">
                              {report.main_reasons.map((reason) => (
                                <li key={reason}>
                                  <span className="check" aria-hidden="true">
                                    ✓
                                  </span>
                                  {reason}
                                </li>
                              ))}
                            </ul>
                            {report.recommendation?.conditions.map((c) => (
                              <p className="caveat" key={c}>
                                {c}
                              </p>
                            ))}
                            <div className="recommendation-footer">
                              <span>
                                Application-derived readiness · fictional review
                                inputs
                              </span>
                              <button
                                className="text-btn"
                                onClick={() =>
                                  setInspection({ kind: "readiness", report })
                                }
                              >
                                Inspect the conditions →
                              </button>
                            </div>
                          </section>
                          <section className="card">
                            <div className="card-header">
                              <h2>The options, on the same terms</h2>
                              <span className="small">
                                {scenario.brief.horizon_months} months ·{" "}
                                {scenario.brief.currency}
                              </span>
                            </div>
                            <div
                              className="table-scroll"
                              role="region"
                              aria-label="Option comparison, scroll horizontally on narrow screens"
                              tabIndex={0}
                            >
                              <table>
                                <caption
                                  className="small"
                                  style={{
                                    textAlign: "left",
                                    marginBottom: 15,
                                  }}
                                >
                                  Common-horizon costs and hard requirements.
                                  Open a cost to inspect its calculation.
                                </caption>
                                <thead>
                                  <tr>
                                    <th scope="col">Option</th>
                                    <th scope="col">All-in cost</th>
                                    <th scope="col">Hard requirements</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {report.comparison.map((option) => (
                                    <tr
                                      className={
                                        report.recommendation?.option_id ===
                                        option.option_id
                                          ? "preferred"
                                          : ""
                                      }
                                      key={option.option_id}
                                    >
                                      <td>
                                        <span className="option-name">
                                          {option.label}
                                        </span>
                                        <span className="option-sub">
                                          {option.role === "status_quo"
                                            ? "Keep / defer baseline"
                                            : report.recommendation
                                                  ?.option_id ===
                                                option.option_id
                                              ? "Preferred in this scenario"
                                              : "Alternative"}
                                        </span>
                                      </td>
                                      <td>
                                        <button
                                          className="text-btn money"
                                          onClick={() =>
                                            inspectCalculation(
                                              option.calculation_id,
                                            )
                                          }
                                          aria-label={`Inspect ${option.label} cost calculation`}
                                        >
                                          {money(option.total, option.currency)}{" "}
                                          ↗
                                        </button>
                                        {option.missing_components.length >
                                          0 && (
                                          <p className="option-sub">
                                            {money(option.known_subtotal)} known
                                            subtotal
                                            <br />
                                            {option.missing_components.join(
                                              ", ",
                                            )}{" "}
                                            missing
                                          </p>
                                        )}
                                      </td>
                                      <td>
                                        {option.constraint_results.map((c) => (
                                          <div
                                            key={c.requirement_id}
                                            style={{ marginBottom: 6 }}
                                          >
                                            <span
                                              className={`status ${c.result}`}
                                            >
                                              {c.result.replaceAll("_", " ")}
                                            </span>{" "}
                                            <span className="option-sub">
                                              {
                                                scenario.brief.hard_requirements.find(
                                                  (r) =>
                                                    r.id === c.requirement_id,
                                                )?.description
                                              }
                                            </span>
                                          </div>
                                        ))}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                            <p className="table-note">
                              Fictional amounts. Unknown costs remain unknown.
                              Explicit zeroes and exclusions are recorded in the
                              brief.
                            </p>
                          </section>
                          <section className="card">
                            <div className="two-col">
                              <div>
                                <p className="detail-label">
                                  The decisive tradeoff
                                </p>
                                <p className="detail-text">{report.tradeoff}</p>
                              </div>
                              <div>
                                <p className="detail-label">
                                  The largest uncertainty
                                </p>
                                <p className="detail-text caveat">
                                  {report.largest_uncertainty}
                                </p>
                              </div>
                            </div>
                            <div
                              style={{
                                borderTop: "1px solid var(--line)",
                                marginTop: 20,
                                paddingTop: 20,
                              }}
                            >
                              <p className="detail-label">
                                What would change the answer?
                              </p>
                              <p className="detail-text">
                                {report.what_would_change_answer}
                              </p>
                              {scenarioId === first.id &&
                                !partial &&
                                !cancelled && (
                                  <button
                                    className="text-btn"
                                    onClick={followUp}
                                  >
                                    Explore a changed-input follow-up{" "}
                                    <Icon name="arrow" />
                                  </button>
                                )}
                            </div>
                            {report.dissent.length > 0 && (
                              <div className="caveat" style={{ marginTop: 12 }}>
                                <p className="detail-label">
                                  Dissent preserved
                                </p>
                                {report.dissent.map((v) => (
                                  <p className="detail-text" key={v}>
                                    {v}
                                  </p>
                                ))}
                              </div>
                            )}
                          </section>
                        </div>
                        <aside className="stack">
                          <section className="card next-action">
                            <span className="number">↗</span>
                            <h2>Your next move</h2>
                            <p>{report.next_action}</p>
                            <button
                              className="btn secondary"
                              onClick={() =>
                                setInspection({
                                  kind: "request",
                                  text: requestText,
                                })
                              }
                            >
                              <Icon name="doc" />
                              Open quote / site-check brief
                            </button>
                            <p className="small">
                              A practical step to resolve the decisive unknown.
                            </p>
                          </section>
                          <section className="card">
                            <div className="card-header">
                              <h2>Decision context</h2>
                              <Icon name="doc" />
                            </div>
                            <div className="fact-row">
                              <span>Budget</span>
                              <strong>
                                {money(
                                  scenario.brief.budget,
                                  scenario.brief.currency,
                                )}
                              </strong>
                            </div>
                            <div className="fact-row">
                              <span>Cost horizon</span>
                              <strong>
                                {scenario.brief.horizon_months} months
                              </strong>
                            </div>
                            <div className="fact-row">
                              <span>Evidence</span>
                              <strong>Fictional snapshots</strong>
                            </div>
                            <div className="fact-row">
                              <span>Provider spending</span>
                              <strong>$0.00 · no calls</strong>
                            </div>
                            <div className="fact-row">
                              <span>Report cutoff</span>
                              <strong>Event {report.event_cutoff}</strong>
                            </div>
                          </section>
                          <section className="card">
                            <h2 style={{ fontSize: 17 }}>Review coverage</h2>
                            {report.coverage.map((p, i) => (
                              <div
                                className="provider-row"
                                key={`${p.provider}-${i}`}
                              >
                                <span className="avatar" aria-hidden="true">
                                  {p.provider[0].toUpperCase()}
                                </span>
                                <span>
                                  {providerNames[p.provider]}
                                  <small>{p.role}</small>
                                </span>
                                <span
                                  className={`status ${p.status === "simulated" ? "muted" : "unknown"}`}
                                >
                                  {p.status}
                                </span>
                              </div>
                            ))}
                            <p className="small">
                              Authored fixtures demonstrate the format. They do
                              not establish independent model performance.
                            </p>
                          </section>
                        </aside>
                      </div>
                    )}
                    {tab === "evidence" && (
                      <div className="evidence-list">
                        <div className="card">
                          <h2>Follow the fact back to its basis.</h2>
                          <p className="small" style={{ marginTop: 10 }}>
                            These are fictional stored passages and
                            deterministic calculations. No external sources were
                            fetched. Dates describe the fixture snapshots.
                          </p>
                        </div>
                        {activeEvidence.map((passage) => (
                          <article className="evidence-item" key={passage.id}>
                            <p className="eyebrow">
                              Fictional supplied evidence · {passage.locator}
                            </p>
                            <h3>{passage.source_title}</h3>
                            <blockquote>{passage.text}</blockquote>
                            <button
                              className="text-btn"
                              onClick={() =>
                                setInspection({
                                  kind: "passage",
                                  value: passage,
                                })
                              }
                            >
                              Open exact passage & provenance →
                            </button>
                          </article>
                        ))}
                        {report.calculations.map((calc) => (
                          <article className="evidence-item" key={calc.id}>
                            <p className="eyebrow">Deterministic calculation</p>
                            <h3>{calc.label}</h3>
                            <p>
                              {calc.formula} = {calc.output ?? "unknown"}{" "}
                              {calc.unit}
                            </p>
                            <button
                              className="text-btn"
                              onClick={() =>
                                setInspection({
                                  kind: "calculation",
                                  value: calc,
                                })
                              }
                            >
                              Inspect inputs & rounding →
                            </button>
                          </article>
                        ))}
                      </div>
                    )}
                    {tab === "activity" && (
                      <section className="card">
                        <div className="card-header">
                          <h2>A public record of this walkthrough</h2>
                          <span className="status muted">Simulated</span>
                        </div>
                        <p className="small" style={{ marginBottom: 25 }}>
                          Fixture explanations and saved versions. This is not a
                          live transcript or hidden model reasoning.
                        </p>
                        {scenarioId !== first.id && (
                          <p className="notice">
                            The proposal excerpts below belong to the original
                            12-month fictional comparison. This changed-input
                            report is a separate authored fixture; no new model
                            review occurred.
                          </p>
                        )}
                        {sample.contributions.map((contribution, index) => (
                          <div className="activity-item" key={contribution.id}>
                            <span className="check" aria-hidden="true">
                              {index + 1}
                            </span>
                            <div>
                              <h3>
                                {providerNames[contribution.provider]} ·{" "}
                                {contribution.stage}
                              </h3>
                              <p>{contribution.public_rationale}</p>
                            </div>
                          </div>
                        ))}
                        <div className="activity-item">
                          <span className="check" aria-hidden="true">
                            ✓
                          </span>
                          <div>
                            <h3>Saved report · version {report.version}</h3>
                            <p>{report.stop_reason}</p>
                            <p>
                              {report.id} · cutoff {report.event_cutoff}
                            </p>
                          </div>
                        </div>
                      </section>
                    )}
                  </section>
                  <div className="footer-note">
                    <Icon name="shield" />
                    <span>
                      Fictional walkthrough · zero provider requests · real-user
                      and funded evaluation gates remain pending.
                    </span>
                    <button
                      className="text-btn"
                      style={{ marginLeft: "auto" }}
                      onClick={() =>
                        downloadReport(report, "md", scenario.brief)
                      }
                    >
                      Export Markdown
                    </button>
                  </div>
                </>
              )}

              {view === "draft" && (
                <div className="form">
                  <div className="meta">
                    <span>Draft revision {savedDraft.revision}</span>
                    <span>
                      {dirty ? "Unsaved edits" : "Saved for this session"}
                    </span>
                    <span>Zero inference calls</span>
                  </div>
                  {draftParent && (
                    <div className="version-banner">
                      <span>
                        Linked fictional follow-up. The original report stays
                        unchanged.
                      </span>
                      <button
                        className="text-btn"
                        onClick={() => openSaved(draftParent.id, "completed")}
                      >
                        Open original →
                      </button>
                    </div>
                  )}
                  <section className="card form-section">
                    <h2>The decision brief</h2>
                    <div className="field">
                      <label htmlFor="title">Comparison title</label>
                      <input
                        id="title"
                        maxLength={200}
                        value={draft.title}
                        onChange={(e) =>
                          setDraft({ ...draft, title: e.target.value })
                        }
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="question">
                        What do you need to decide?
                      </label>
                      <textarea
                        id="question"
                        maxLength={4000}
                        value={draft.question}
                        onChange={(e) =>
                          setDraft({ ...draft, question: e.target.value })
                        }
                      />
                    </div>
                    <div className="two-col">
                      <div className="field">
                        <label htmlFor="budget">Budget (USD)</label>
                        <input
                          id="budget"
                          inputMode="decimal"
                          value={draft.budget ?? ""}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              budget: e.target.value || null,
                            })
                          }
                        />
                        <small>
                          Plain decimal amount. Leave blank for unknown.
                        </small>
                      </div>
                      <div className="field">
                        <label htmlFor="horizon">
                          Common cost horizon (months)
                        </label>
                        <input
                          id="horizon"
                          type="number"
                          min={1}
                          max={120}
                          value={draft.horizon_months ?? ""}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              horizon_months: e.target.value
                                ? Number(e.target.value)
                                : null,
                            })
                          }
                        />
                      </div>
                    </div>
                    <div className="field">
                      <label htmlFor="goal">A good outcome looks like</label>
                      <textarea
                        id="goal"
                        maxLength={4000}
                        value={draft.goal}
                        onChange={(e) =>
                          setDraft({ ...draft, goal: e.target.value })
                        }
                      />
                    </div>
                    <div className="two-col">
                      <div className="field">
                        <label htmlFor="deadline">Deadline (optional)</label>
                        <input
                          id="deadline"
                          type="date"
                          value={draft.deadline ?? ""}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              deadline: e.target.value || null,
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label htmlFor="location">Location</label>
                        <input
                          id="location"
                          maxLength={500}
                          value={draft.location}
                          onChange={(e) =>
                            setDraft({ ...draft, location: e.target.value })
                          }
                        />
                      </div>
                    </div>
                  </section>
                  <section className="card form-section">
                    <h2>Options and the alternative to buying</h2>
                    <div className="form-options">
                      {draft.options.map((option) => (
                        <div className="option-box" key={option.id}>
                          <h3>{option.label}</h3>
                          <span className="status muted">
                            {option.role === "status_quo"
                              ? "Status quo"
                              : "Candidate"}
                          </span>
                          {option.cost_components.map((c) => (
                            <p key={c.id}>
                              {c.label}:{" "}
                              <strong>{money(c.amount, c.currency)}</strong>
                              {c.frequency === "monthly" && " / month"}
                              {c.unknown_reason && ` · ${c.unknown_reason}`}
                            </p>
                          ))}
                        </div>
                      ))}
                    </div>
                    <p className="small" style={{ marginTop: 16 }}>
                      Option facts are fixed fictional fixtures in this
                      walkthrough. Select a prepared scenario to compare a
                      changed horizon or missing installation cost.
                    </p>
                  </section>
                  <section className="card form-section">
                    <h2>Requirements and preferences</h2>
                    {draft.hard_requirements.map((req) => (
                      <div className="fact-row" key={req.id}>
                        <span>Hard requirement</span>
                        <strong>{req.description}</strong>
                      </div>
                    ))}
                    {draft.preferences.map((p) => (
                      <div className="fact-row" key={p.id}>
                        <span>Preference {p.rank}</span>
                        <strong>{p.description}</strong>
                      </div>
                    ))}
                    <p className="small" style={{ marginTop: 14 }}>
                      User-supplied facts remain distinct from external
                      verification. No attachment uploads or public searches are
                      enabled.
                    </p>
                  </section>
                  <section className="card">
                    <div className="field">
                      <label htmlFor="scenario">
                        Prepared fictional scenarios
                      </label>
                      <select
                        id="scenario"
                        value={supported?.id ?? ""}
                        onChange={(e) => {
                          const next = sample.scenarios.find(
                            (s) => s.id === e.target.value,
                          );
                          if (next) chooseFixture(next);
                        }}
                      >
                        <option value="" disabled>
                          Custom edits — no matching fixture report
                        </option>
                        {sample.scenarios.map((s) => (
                          <option value={s.id} key={s.id}>
                            {s.label}
                          </option>
                        ))}
                      </select>
                      <small>
                        Choosing a scenario replaces this fictional draft.
                        Custom edits can be saved, but a fixed report is shown
                        only for matching inputs.
                      </small>
                    </div>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={persist}
                        onChange={(e) => {
                          setPersist(e.target.checked);
                          if (!e.target.checked) {
                            try {
                              localStorage.removeItem(STORAGE_KEY);
                            } catch {
                              setError(
                                "Unable to clear browser storage. Clear it in browser settings.",
                              );
                            }
                          }
                        }}
                      />
                      <span>
                        Keep this fictional draft in this browser when I save.
                        Use fictional data only. Session memory is the default.
                      </span>
                    </label>
                  </section>
                  <div className="form-foot">
                    <button className="btn secondary" onClick={save}>
                      Save draft
                    </button>
                    <span className="save-state" role="status">
                      {dirty
                        ? "Changes are not saved"
                        : "Draft saved · no inference"}
                    </span>
                    <button className="btn primary" onClick={reviewPreflight}>
                      Review preflight <Icon name="arrow" />
                    </button>
                  </div>
                </div>
              )}

              {view === "preflight" && (
                <div className="form">
                  <div className="meta">
                    <span>Draft revision {savedDraft.revision}</span>
                    <span>Single-use walkthrough preflight</span>
                    <span>
                      Expires in{" "}
                      {quote
                        ? Math.max(
                            0,
                            Math.ceil((quote.expiresAt - now) / 60000),
                          )
                        : 0}{" "}
                      minutes
                    </span>
                  </div>
                  <div className="card form-section">
                    <h2>The scope you are approving</h2>
                    <div className="fact-row">
                      <span>Workflow</span>
                      <strong>
                        Independent proposals → synthesis → distinct audit
                        <br />
                        Provisional protocol · simulated only
                      </strong>
                    </div>
                    <div className="fact-row">
                      <span>Shared recipients in a future real run</span>
                      <strong>OpenAI · Anthropic Claude · Google Gemini</strong>
                    </div>
                    <div className="fact-row">
                      <span>Selected inputs</span>
                      <strong>
                        Fictional brief and quote snapshots
                        <br />
                        No private uploads
                      </strong>
                    </div>
                    <div className="fact-row">
                      <span>Public research</span>
                      <strong>Disabled · no outgoing search terms</strong>
                    </div>
                    <div className="fact-row">
                      <span>Actual cost in this walkthrough</span>
                      <strong>$0.00 · zero provider calls</strong>
                    </div>
                    <div className="fact-row">
                      <span>Future authorization example</span>
                      <strong>
                        $2 maximum / run · 24 calls · 600 active seconds
                        <br />
                        $10 / day · $50 / month (personal defaults)
                      </strong>
                    </div>
                    <div className="fact-row">
                      <span>Protected ending</span>
                      <strong>
                        Synthesis + distinct audit
                        <br />
                        Reserved money, 2 calls, and final-stage time
                      </strong>
                    </div>
                    <div className="fact-row">
                      <span>Initial admission</span>
                      <strong>
                        Within 30 minutes of accepted Start
                        <br />
                        This walkthrough starts immediately
                      </strong>
                    </div>
                    <p className="small" style={{ marginTop: 18 }}>
                      These are provisional authorization limits, not prices or
                      measured results. This browser rehearsal does not
                      authorize paid work.
                    </p>
                  </div>
                  <div className="card">
                    <h2>Confirm this fictional simulation</h2>
                    <p className="small" style={{ marginTop: 15 }}>
                      Example fixture fingerprint:{" "}
                      <span style={{ overflowWrap: "anywhere" }}>
                        {scenario.preflight.fingerprint}
                      </span>
                    </p>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={consent}
                        onChange={(e) => setConsent(e.target.checked)}
                      />
                      <span>
                        I understand that these are saved fictional outputs and
                        this action runs an offline simulation. No data is sent
                        to providers.
                      </span>
                    </label>
                    <div className="actions">
                      <button
                        className="btn secondary"
                        onClick={() => go("draft")}
                      >
                        Back to brief
                      </button>
                      <button
                        className="btn primary"
                        disabled={!consent || !quote || now >= quote.expiresAt}
                        onClick={start}
                      >
                        Start simulated review <Icon name="arrow" />
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {view === "running" && (
                <div className="card running">
                  <span className="status muted">Simulated activity</span>
                  <h2>Looking at the same facts from different angles.</h2>
                  <p>
                    The following steps reveal saved fictional contributions.
                    Progress is a walkthrough animation, not measured model
                    execution.
                  </p>
                  {stopRequested && (
                    <p className="notice" role="status">
                      Stop not confirmed. This rehearsal waits for a simulated
                      cancellation acknowledgement before showing a cancelled
                      result.
                    </p>
                  )}
                  <div
                    className="progress-track"
                    role="progressbar"
                    aria-valuenow={step}
                    aria-valuemin={0}
                    aria-valuemax={5}
                    aria-label="Fictional walkthrough steps"
                  >
                    <div style={{ width: `${(step / 5) * 100}%` }} />
                  </div>
                  {[
                    "Frozen brief and shared evidence",
                    "Three independently authored proposal fixtures",
                    "Synthesis fixture",
                    "Distinct audit fixture",
                    "Saved comparison and readiness",
                  ].map((label, i) => (
                    <div className="run-step" key={label}>
                      <span className="check" aria-hidden="true">
                        {step > i ? "✓" : i + 1}
                      </span>
                      {label}
                      <span className="small">
                        {step > i ? "Revealed" : "Pending"}
                      </span>
                    </div>
                  ))}
                  <div className="actions" style={{ marginTop: 25 }}>
                    <button
                      className="btn secondary"
                      disabled={stopRequested}
                      onClick={() => {
                        setStopRequested(true);
                      }}
                    >
                      {stopRequested ? "Stop not confirmed" : "Stop simulation"}
                    </button>
                    <button
                      className="text-btn"
                      disabled={stopRequested}
                      onClick={() => {
                        setPartial(true);
                        setCancelled(false);
                        setSavedVariants((variants) => ({
                          ...variants,
                          [scenarioId]: "partial",
                        }));
                        setSavedScenarios((ids) =>
                          ids.includes(scenarioId) ? ids : [...ids, scenarioId],
                        );
                        setView("report");
                        setTab("overview");
                        setFeedback(
                          "Partial-result fixture: synthesis unavailable. Prior work is preserved.",
                        );
                      }}
                    >
                      Inspect a partial-result example →
                    </button>
                  </div>
                </div>
              )}
            </main>
          </div>
        </div>
        <Inspector inspection={inspection} close={() => setInspection(null)} />
      </fieldset>
    </>
  );
}
