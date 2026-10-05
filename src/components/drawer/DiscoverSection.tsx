import { useEffect, useState } from "react";
import { getJson, postJson } from "../../api";
import { labelCase } from "../../utils/format";
import { ListButton, PanelState, Section } from "../intel/common";
import type { Selection } from "../../types";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// DISCOVER: "confirm several -> more like these".
//
// Pick one or more change events as positive examples; tiles of events the
// analyst rejected are used automatically as negative examples. Results are
// spatially de-duplicated and explained. The features are spectral and
// change-history statistics, NOT semantic labels: no embedding model is
// staged, and the panel says so.
// ---------------------------------------------------------------------------
type Candidate = { id: string; change_class: string; review_state: string; status: string; score: number };
type Similar = { tile_id: string; similarity: number; lon: number; lat: number; why: string[] };

export default function DiscoverSection({ onSelect }: { onSelect: (selection: Selection) => void }) {
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<{ results: Similar[]; method: string; negative?: string[] } | null>(null);
  const [error, setError] = useState(false);
  // The real state of RemoteCLIP text search and discovery (never a fixed message).
  const [semanticState, setSemanticState] = useState<string | null>(null);
  const [discoveryState, setDiscoveryState] = useState<string | null>(null);

  useEffect(() => {
    getJson<Candidate[]>("/api/review/queue").then(setCandidates).catch(() => setError(true));
    getJson<{ state: string }>("/api/semantic/status").then((s) => setSemanticState(s.state)).catch(() => setSemanticState("UNAVAILABLE"));
    getJson<{ state: string }>("/api/discovery/status").then((s) => setDiscoveryState(s.state)).catch(() => setDiscoveryState("UNAVAILABLE"));
  }, []);

  function toggle(id: string) {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setChosen(next);
  }

  async function run() {
    try {
      setResult(await postJson("/api/review/more-like-these", { candidate_ids: Array.from(chosen), limit: 12 }));
    } catch {
      setResult({ results: [], method: "similarity unavailable (Lumon API unreachable)" });
    }
  }

  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!candidates) return <PanelState state="loading" />;

  return (
    <>
      <Section title="EXAMPLES" meta={`${chosen.size} selected`}
        info={{ ...INFO.dossiers, state: `${candidates.length} change events available as examples, ${candidates.filter((c) => c.review_state === "rejected").length} rejected (negatives)` }}>
        <div className="empty-note">Tick change events that show what you are looking for. Confirmed events make good examples; rejected ones are used as negatives.</div>
        {candidates.length === 0 && <div className="empty-note">No change events yet.</div>}
        {candidates.map((c) => (
          <label key={c.id} className="check-row">
            <input type="checkbox" checked={chosen.has(c.id)} onChange={() => toggle(c.id)} />
            <span>{labelCase(c.change_class)}</span>
            <span className="muted">{c.review_state} · {c.score.toFixed(2)}</span>
          </label>
        ))}
        <button type="button" className="action-button action-button--primary" disabled={chosen.size === 0} onClick={run}>MORE LIKE THESE</button>
      </Section>
      {result && (
        <Section title="SIMILAR SITES" meta={`${result.results.length}`}>
          <div className="empty-note">{result.method}</div>
          {result.results.map((item) => (
            <ListButton key={item.tile_id} title={`${item.tile_id} · similarity ${item.similarity.toFixed(2)}`} meta={item.why.join(" · ")} onClick={() => onSelect({ kind: "tile", lon: item.lon, lat: item.lat })} />
          ))}
        </Section>
      )}
      <Section title="SEMANTIC TEXT SEARCH" meta={semanticState ? `RemoteCLIP · ${semanticState}` : "…"}>
        {semanticState === "READY" || semanticState === "PARTIAL" ? (
          <div className="empty-note">
            RemoteCLIP text-to-image search, image-to-image similarity and discovery clusters ({discoveryState ?? "…"}) work on the
            staged imagery: open Imagery in the navigation. This Dossiers tool stays example-based (non-semantic features).
          </div>
        ) : (
          <div className="notice">{semanticState ?? "Checking…"} — text-to-image search needs the RemoteCLIP model and its index (see Imagery). This Dossiers tool works from examples.</div>
        )}
      </Section>
    </>
  );
}
