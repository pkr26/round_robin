"use client";

import { useEffect, useState } from "react";
import { readApi, type DraftBrief, type EvidencePassage } from "./api-client";

type Props = {
  runId: string;
  references: string[];
  initial: EvidencePassage[];
  brief: DraftBrief | null;
  selected: string[];
  disabled: boolean;
  onChange: (ids: string[]) => void;
};

export function RequirementPassages({ runId, references, initial, brief, selected, disabled, onChange }: Props) {
  const [extra, setExtra] = useState<EvidencePassage[]>([]);
  const [unavailable, setUnavailable] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    const missing = references.filter(id => !initial.some(passage => passage.id === id && passage.run_id === runId));
    setUnavailable(false);
    // Snapshot evidence is bounded. Resolve remaining exact identities, including
    // passages on later source pages, before making them available to select.
    if (missing.length) void Promise.allSettled(missing.map(async id => {
      const passage = await readApi<EvidencePassage>(`/api/runs/${encodeURIComponent(runId)}/sources/${encodeURIComponent(id)}`);
      if (passage.id !== id || passage.run_id !== runId) throw new Error("Source identity changed");
      return passage;
    })).then(results => {
      if (!active) return;
      setExtra(results.flatMap(item => item.status === "fulfilled" ? [item.value] : []));
      setUnavailable(results.some(item => item.status === "rejected"));
    });
    return () => { active = false; };
  }, [runId, references, initial, attempt]);

  const passages = new Map([...initial, ...extra].filter(item => item.run_id === runId).map(item => [item.id, item]));
  return <fieldset disabled={disabled}>
    <legend>Passages supporting this requirement</legend>
    <p>Read the exact wording and select each passage you checked.</p>
    {references.map(id => {
      const source = passages.get(id);
      if (!source) return null;
      const suppliers = brief?.options.filter(option => option.cost_components.some(cost =>
        cost.source_ref === id || cost.source_ref === source.original_alias)).map(option => option.label) ?? [];
      const attribution = suppliers.length ? `Offer: ${suppliers.join(", ")}.` : "Supplier was not recorded.";
      return <label className="live-passage-choice" key={id}>
        <input type="checkbox" checked={selected.includes(id)} onChange={event =>
          onChange(event.target.checked ? [...selected, id] : selected.filter(item => item !== id))} />
        <strong>{source.source_title}</strong>
        <span>{attribution} {source.url && `Source: ${source.url}. `}{source.locator} · Recorded {new Date(source.verified_at).toISOString().slice(0, 10)} UTC</span>
        <span className="live-passage-text">{source.text}</span>
      </label>;
    })}
    {!references.length && <p>No current passages are available for this requirement.</p>}
    {references.some(id => !passages.has(id)) && !unavailable && <p role="status">Loading source passages…</p>}
    {unavailable && <p role="status">Some passages could not be read. <button type="button" onClick={() => setAttempt(value => value + 1)}>Retry reading passages</button></p>}
  </fieldset>;
}
