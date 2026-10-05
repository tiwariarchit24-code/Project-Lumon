import { useCallback, useEffect, useState } from "react";
import { ApiError, postJson } from "../../api";
import { formatDate } from "../../utils/format";
import { Facts } from "./common";
import type { QueryResultItem } from "../../types";

// ---------------------------------------------------------------------------
// Image-to-image similarity (RemoteCLIP): image chips that look like an
// example chip. The example is either a chip id or "the chip under this map
// point". Uses the cached embeddings, so no model runs at query time.
//
// Three scopes:
//   OTHER PLACES  look-alike places elsewhere (best date per place)
//   SAME PLACE    every date at the example's place, most alike first
//   ALL           both
// "Not like this" on a result adds it as a negative example and re-ranks.
//
// Scores are model similarity, never a probability or a confirmed match.
// When the model is not staged the request is refused and nothing else runs;
// the non-semantic "similar tiles" tool is separate and labelled as such.
// ---------------------------------------------------------------------------
type SimilarResponse = {
  examples: QueryResultItem[]; scope: string; score_kind: string; chips_searched: number;
  score_distribution: { median: number; max: number; min: number } | null; results: QueryResultItem[];
  timing_ms: { total: number }; note: string;
};

type SimilarChipsProps = {
  chipId?: string; // the example chip ...
  at?: { lon: number; lat: number }; // ... or the chip under this point (latest indexed scene)
  onOpenChip: (item: QueryResultItem) => void;
};

const SCOPES: [string, string][] = [["other-places", "OTHER PLACES"], ["same-place", "SAME PLACE"], ["all", "ALL"]];

export default function SimilarChips({ chipId, at, onOpenChip }: SimilarChipsProps) {
  const [scope, setScope] = useState("other-places");
  const [negatives, setNegatives] = useState<string[]>([]);
  const [result, setResult] = useState<SimilarResponse | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false);

  // A new example starts a fresh search.
  useEffect(() => {
    setResult(null);
    setNegatives([]);
    setMessage(null);
    setRequested(false);
  }, [chipId, at?.lon, at?.lat]);

  const run = useCallback(async (nextScope: string, nextNegatives: string[]) => {
    setBusy(true);
    setMessage(null);
    try {
      const body = { chip_ids: chipId ? [chipId] : [], at: chipId ? null : at, negative_ids: nextNegatives, scope: nextScope, limit: 12 };
      setResult(await postJson<SimilarResponse>("/api/semantic/similar", body));
    } catch (error) {
      setResult(null);
      const detail = error instanceof ApiError ? (error.detail as { state?: string; reasons?: string[] } | string | null) : null;
      if (detail && typeof detail === "object" && detail.state) setMessage(`${detail.state}: ${(detail.reasons ?? []).join("; ")}`);
      else setMessage(error instanceof ApiError && error.status === 404 ? "No indexed image chip at this location." : `Not available: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [chipId, at]);

  function start() {
    setRequested(true);
    run(scope, negatives);
  }
  function changeScope(next: string) {
    setScope(next);
    if (requested) run(next, negatives);
  }
  function notLikeThis(id: string) {
    const next = [...negatives, id];
    setNegatives(next);
    run(scope, next);
  }

  const example = result?.examples[0];
  return (
    <div className="similar-chips">
      <div className="toolbar-row">
        {SCOPES.map(([value, label]) => (
          <button key={value} type="button" className={scope === value ? "action-button action-button--active" : "action-button"}
            aria-pressed={scope === value} onClick={() => changeScope(value)}>{label}</button>
        ))}
      </div>
      {!requested && <button type="button" className="action-button" onClick={start} disabled={busy}>FIND SIMILAR IMAGE CHIPS</button>}
      {busy && <div className="empty-note">Ranking…</div>}
      {message && <div className="notice notice--partial">{message}</div>}
      {result && (
        <div className="fade-in">
          <div className="notice">{result.note}</div>
          <Facts rows={[
            ["EXAMPLE", example ? `${formatDate(example.acquired_at)} · ${example.chip_km} km · ${example.scene_id}` : "—"],
            ["SEARCHED", `${result.chips_searched} chips of the same size · ${Math.round(result.timing_ms.total)} ms (cached embeddings)`],
            ["SCORES", result.score_distribution ? `top ${result.score_distribution.max.toFixed(3)} · median ${result.score_distribution.median.toFixed(3)} (cosine, not a probability)` : "—"],
            ["NOT LIKE", negatives.length ? `${negatives.length} chip${negatives.length > 1 ? "s" : ""}` : null],
          ]} />
          {result.results.length === 0 && <div className="empty-note">No other chips in this scope.</div>}
          <div className="chip-grid">
            {result.results.map((item) => (
              <div key={item.id} className="chip-card chip-card--with-action">
                <button type="button" className="chip-card__open" onClick={() => onOpenChip({ ...item, query: `image like ${example?.id ?? "example"}` })}
                  title={`${item.scene_id}\n${item.chip_km} km chip · image cosine ${item.score?.toFixed(4)}`}>
                  <img src={`/api/scenes/${item.scene_id}/quicklook.png?bbox=${item.bbox!.join(",")}`} alt={`Similar chip ${item.rank} from ${formatDate(item.acquired_at)}`} loading="lazy" />
                  <span className="chip-card__meta">#{item.rank} · {item.score?.toFixed(3)}</span>
                  <span className="chip-card__meta">{formatDate(item.acquired_at)}</span>
                </button>
                <button type="button" className="chip-card__reject" onClick={() => notLikeThis(item.id)} title="Not like this: use as a negative example and re-rank">✕ NOT LIKE</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
