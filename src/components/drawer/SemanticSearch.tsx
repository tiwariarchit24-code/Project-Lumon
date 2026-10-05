import { useCallback, useEffect, useState } from "react";
import { ApiError, getJson, postJson } from "../../api";
import { formatDate } from "../../utils/format";
import { Facts, Section } from "../intel/common";
import { INFO } from "../../data/infoNotes";
import type { QueryResultItem, SemanticRunInfo, SemanticStatus } from "../../types";

// ---------------------------------------------------------------------------
// SEMANTIC IMAGE SEARCH (RemoteCLIP) inside the Imagery section.
//
//  - status: NOT STAGED / INDEXING / READY / PARTIAL / UNAVAILABLE, the model
//    name and version, and how many scenes and chips are indexed
//  - a text box: the description is embedded by the model and every indexed
//    chip is ranked by cosine similarity
//  - results: chip thumbnails with date, rank and score; clicking one opens
//    it in the intelligence panel and shows its scene on the map
//
// The score is MODEL SIMILARITY: not a probability, confidence or detection.
// When the model is not usable, the search is refused; nothing else runs
// in its place.
// ---------------------------------------------------------------------------
type SemanticSearchProps = { onOpenResult: (item: QueryResultItem) => void };
type SearchResponse = SemanticRunInfo & { results: QueryResultItem[] };

const EXAMPLES = ["airport runway", "river", "bare soil construction site"];

// Tone of the status dot for each retrieval state.
const TONE: Record<string, string> = { READY: "ok", PARTIAL: "pending", INDEXING: "pending", "NOT STAGED": "pending", UNAVAILABLE: "error" };

export default function SemanticSearch({ onOpenResult }: SemanticSearchProps) {
  const [status, setStatus] = useState<SemanticStatus | null>(null);
  const [text, setText] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadStatus = useCallback(() => {
    getJson<SemanticStatus>("/api/semantic/status").then(setStatus).catch(() => setStatus(null));
  }, []);
  useEffect(loadStatus, [loadStatus]);

  async function search(query: string) {
    if (!query.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      setResult(await postJson<SearchResponse>("/api/semantic/search", { text: query, limit: 24 }));
    } catch (error) {
      // 409 NOT STAGED / 503 UNAVAILABLE: say so, show nothing else.
      const detail = error instanceof ApiError ? (error.detail as { state?: string; reasons?: string[] } | null) : null;
      setResult(null);
      setMessage(detail?.state ? `${detail.state}: ${(detail.reasons ?? []).join("; ")}` : `Search failed: ${(error as Error).message}`);
    } finally {
      setBusy(false);
      loadStatus();
    }
  }

  async function index() {
    try {
      const job = await postJson<{ job_id: number }>("/api/semantic/index", {});
      setMessage(`Indexing queued (job ${job.job_id}). Only chips not already cached are embedded.`);
      loadStatus();
    } catch {
      setMessage("Indexing not queued: the model is not staged or the API is unreachable.");
    }
  }

  const state = status?.state ?? "UNAVAILABLE";
  const usable = state === "READY" || state === "PARTIAL";

  return (
    <Section title="SEMANTIC IMAGE SEARCH" meta={`AI/ML · ${state}`}
      info={{ ...INFO.semanticSearch, state: status ? `${state}; ${status.scenes_indexed}/${status.scenes_eligible} eligible scenes, ${status.chips_indexed} chips indexed` : "status unavailable" }}>
      <div className="semantic-status">
        <span className={`status-dot status-dot--${TONE[state] ?? "neutral"}`} aria-hidden="true" />
        <span className="semantic-status__state">{state}</span>
        {status && <span className="semantic-status__model">{status.model_name}</span>}
      </div>
      {status && (
        <Facts rows={[
          ["MODEL", `${status.architecture} · weights ${status.weights_sha256.slice(0, 12)}… · ${status.license ?? "licence ?"}`],
          ["RUNTIME", `${status.framework}${status.model_loaded ? " · loaded" : " · loads on first search"}`],
          ["INDEXED", `${status.scenes_indexed} of ${status.scenes_eligible} eligible scenes · ${status.chips_indexed} chips (${status.chip_sizes_px.map((p) => `${p * 10 / 1000} km`).join(" + ")})`],
          ["EXCLUDED", `${status.scenes_excluded_by_quality} scenes by quality status · ${status.scenes_excluded_by_indexer.length} by the indexer · ${status.chips_excluded} chips > ${Math.round(status.max_invalid_fraction * 100)}% cloud/no-data`],
        ]} />
      )}
      {status?.reasons.map((reason) => <div key={reason} className="notice notice--partial">{reason}</div>)}

      <form className="semantic-form" onSubmit={(e) => { e.preventDefault(); search(text); }}>
        <input className="semantic-form__input" value={text} onChange={(e) => setText(e.target.value)}
          placeholder="Describe what to look for, e.g. airport runway" aria-label="Image description" disabled={!usable} />
        <button type="submit" className="action-button action-button--primary" disabled={!usable || busy || !text.trim()}>
          {busy ? "SEARCHING…" : "SEARCH"}
        </button>
      </form>
      {usable && (
        <div className="semantic-examples">
          {EXAMPLES.map((example) => (
            <button key={example} type="button" className="text-button" onClick={() => { setText(example); search(example); }}>{example}</button>
          ))}
        </div>
      )}
      <div className="toolbar-row">
        <button type="button" className="action-button" onClick={index} disabled={state === "INDEXING" || (status?.reasons ?? []).some((r) => r.includes("Weights") || r.includes("package"))}>
          INDEX ARCHIVE
        </button>
        <button type="button" className="action-button" onClick={loadStatus}>RELOAD STATUS</button>
      </div>
      {message && <div className="notice">{message}</div>}

      {result && (
        <div className="fade-in">
          <div className="notice">
            Ranked by cosine similarity of RemoteCLIP embeddings: model similarity, not a probability or a detection.
            A high rank does not prove the described object is present; check the images.
          </div>
          <Facts rows={[
            ["QUERY", `“${result.query}”`],
            ["SEARCHED", `${result.chips_searched} chips from ${result.scenes_searched} scenes`],
            ["QUERY TIME", `${Math.round(result.timing_ms.text_encode + result.timing_ms.rank)} ms (text ${Math.round(result.timing_ms.text_encode)} + ranking ${Math.round(result.timing_ms.rank)})${result.timing_ms.model_load > 50 ? ` · model load ${(result.timing_ms.model_load / 1000).toFixed(1)} s` : ""}`],
            ["SCORES", result.score_distribution ? `top ${result.score_distribution.max.toFixed(3)} · median ${result.score_distribution.median.toFixed(3)} (compare within one query only)` : "—"],
          ]} />
          <div className="chip-grid">
            {result.results.map((item) => (
              <button key={item.id} type="button" className="chip-card" onClick={() => onOpenResult(item)}
                title={`${item.scene_id}\n${item.chip_km} km chip · cosine ${item.score?.toFixed(4)}`}>
                <img src={`/api/scenes/${item.scene_id}/quicklook.png?bbox=${item.bbox!.join(",")}`} alt={`Chip ${item.rank} from ${formatDate(item.acquired_at)}`} loading="lazy" />
                <span className="chip-card__meta">#{item.rank} · {item.score?.toFixed(3)}</span>
                <span className="chip-card__meta">{formatDate(item.acquired_at)} · {item.chip_km} km</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </Section>
  );
}
