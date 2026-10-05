import { useEffect, useState } from "react";
import { getJson, postJson } from "../../api";
import { formatAge, formatTime } from "../../utils/format";
import { Facts, Freshness, PanelState, Section } from "../intel/common";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// SOURCES: one card per OSINT / reference source with health, last success
// and failure, data age, coverage, licence and cache status. Refreshing a
// source queues a job; in air-gapped mode the job re-reads the last staged
// snapshot instead of using the network.
// ---------------------------------------------------------------------------
type Source = {
  id: string; name: string; category: string; provider: string; coverage: string; license: string; attribution: string;
  health: string; connection: string; last_success: string | null; last_failure: string | null; last_error: string | null;
  last_data_time: string | null; record_count: number; cache_status: string; requires_key: boolean; key_env?: string;
  adapter: string | null; docs_url: string; notes?: string; offline_cache_supported: boolean;
  last_run: { status: string; records_in: number; records_kept: number; records_outside: number; records_invalid: number } | null;
  freshness_status: string; freshness_label: string; stale_after_hours: number | null;
  rate_limit_remaining: number | null; next_allowed_at: string | null; consecutive_failures: number;
};
// Automatic polling status from /api/live/status (aircraft and vessels).
type LiveFeed = { source_id: string; state: string; problems: string[]; interval_s?: number; daily_credits?: number; last_result?: string | null; next_poll_at?: string | null };

// "POLLING every 108 s" or "NOT CONFIGURED: reason; reason".
function liveLine(feed: LiveFeed | undefined): string | null {
  if (!feed) return null;
  if (feed.state === "POLLING" || feed.state === "BACKING OFF") {
    return `${feed.state} every ${feed.interval_s} s (budget ${feed.daily_credits} credits/day)${feed.next_poll_at ? ` · next ${formatTime(feed.next_poll_at)}` : ""}`;
  }
  return `${feed.state}${feed.problems.length ? `: ${feed.problems.join("; ")}` : ""}`;
}

export default function SourcesSection() {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [error, setError] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [live, setLive] = useState<Record<string, LiveFeed>>({});

  function load() {
    getJson<Source[]>("/api/sources").then(setSources).catch(() => setError(true));
    getJson<Record<string, LiveFeed>>("/api/live/status")
      .then((status) => setLive(Object.fromEntries(Object.values(status).map((feed) => [feed.source_id, feed]))))
      .catch(() => setLive({}));
  }
  useEffect(load, []);

  async function refresh(id: string | null) {
    try {
      const job = id ? await postJson<{ job_id: number; mode: string }>(`/api/sources/${id}/refresh`, {}) : await postJson<{ job_id: number }>("/api/jobs", { kind: "refresh-all", params: {} });
      setMessage(`Queued job ${job.job_id}. Reload this panel in a moment to see the result.`);
    } catch {
      setMessage("Not queued: Lumon API unreachable.");
    }
  }

  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!sources) return <PanelState state="loading" />;

  return (
    <>
      <Section title="SOURCE REGISTRY" meta={`${sources.length} sources`}
        info={{ ...INFO.sources, state: ["LIVE", "DELAYED", "SNAPSHOT", "STALE", "KEY REQUIRED", "NOT IMPLEMENTED"].map((s) => [s, sources.filter((x) => x.freshness_status === s).length] as const).filter(([, n]) => n).map(([s, n]) => `${n} ${s}`).join(" · ") }}>
        <div className="toolbar-row">
          <button type="button" className="action-button" onClick={() => refresh(null)}>REFRESH ALL</button>
          <button type="button" className="action-button" onClick={load}>RELOAD</button>
        </div>
        {message && <div className="notice">{message}</div>}
        {sources.map((source) => (
          <div key={source.id} className="source-card">
            <button type="button" className="source-card__head" onClick={() => setOpen(open === source.id ? null : source.id)} aria-expanded={open === source.id}>
              <span className="source-card__name">{source.name}</span>
              <span className="source-card__sub"><Freshness status={source.freshness_status} label={source.freshness_label} /> · {source.category} · {source.record_count} records</span>
            </button>
            {open === source.id && (
              <div className="source-card__body fade-in">
                <Facts rows={[
                  ["MODE", source.connection],
                  ["FRESH FOR", source.stale_after_hours !== null ? `${source.stale_after_hours} h after download, then STALE` : null],
                  ["LAST SUCCESS", source.last_success ? `${formatTime(source.last_success)} (${formatAge(source.last_success)})` : null],
                  ["AUTO REFRESH", liveLine(live[source.id]) ?? "none (manual refresh only)"],
                  ["LAST FAILURE", source.last_failure ? `${formatTime(source.last_failure)} — ${source.last_error}` : null],
                  ["FAILURES IN A ROW", source.consecutive_failures ? String(source.consecutive_failures) : null],
                  ["PROVIDER CREDITS", source.rate_limit_remaining !== null && source.rate_limit_remaining !== undefined ? `${source.rate_limit_remaining} remaining (reported by provider)` : null],
                  ["RETRY ALLOWED", source.next_allowed_at ? formatTime(source.next_allowed_at) : null],
                  ["DATA AGE", source.last_data_time ? `newest record ${formatAge(source.last_data_time)}` : null],
                  ["LAST RUN", source.last_run ? `${source.last_run.status}: ${source.last_run.records_kept} kept, ${source.last_run.records_outside} outside India, ${source.last_run.records_invalid} quarantined` : null],
                  ["COVERAGE", source.coverage],
                  ["PROVIDER", source.provider],
                  ["LICENSE", source.license],
                  ["CREDIT", source.attribution],
                  ["CACHE", `${source.cache_status}${source.offline_cache_supported ? " · offline snapshot supported" : ""}`],
                  ["KEY", source.requires_key ? `required (${source.key_env} in .env.local)` : "not required"],
                  ["DOCS", source.docs_url],
                ]} />
                {source.notes && <div className="empty-note">{source.notes}</div>}
                {source.adapter ? <button type="button" className="action-button" onClick={() => refresh(source.id)}>REFRESH</button> : <div className="notice">NOT IMPLEMENTED</div>}
              </div>
            )}
          </div>
        ))}
      </Section>
    </>
  );
}
