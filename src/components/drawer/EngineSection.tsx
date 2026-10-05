import { useEffect, useState } from "react";
import { getJson, postJson } from "../../api";
import { formatTime } from "../../utils/format";
import { Facts, PanelState, Section } from "../intel/common";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// CHANGE ENGINE: how candidates are produced, the parameters of the last
// run (read from its provenance record), and a button to re-run it.
// ---------------------------------------------------------------------------
type Job = { id: number; kind: string; status: string; created_at: string; finished_at: string | null; result: Record<string, unknown> | null; error: string | null };
type Candidate = { id: string };
type ChangeRecord = { provenance: { processing_version: string; created_at?: string; retrieved_at: string; parameters: Record<string, unknown>; input_ref: string } | null };

export default function EngineSection({ onRan }: { onRan: () => void }) {
  const [provenance, setProvenance] = useState<ChangeRecord["provenance"]>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  function load() {
    getJson<Candidate[]>("/api/review/queue?include_suppressed=true")
      .then(async (queue) => {
        if (queue.length) setProvenance((await getJson<ChangeRecord>(`/api/changes/${queue[0].id}`)).provenance);
      })
      .catch(() => undefined)
      .finally(() => setLoading(false));
    getJson<Job[]>("/api/jobs?limit=10").then((all) => setJobs(all.filter((j) => j.kind === "change-analysis"))).catch(() => undefined);
  }

  useEffect(load, []);

  async function run() {
    try {
      const aois = await getJson<{ id: string }[]>("/api/aois");
      const jobIds = [];
      for (const aoi of aois) jobIds.push((await postJson<{ job_id: number }>(`/api/aois/${aoi.id}/analyse`, {})).job_id);
      setMessage(`Change analysis queued (jobs ${jobIds.join(", ")}). Reviewed candidates are never overwritten.`);
      window.setTimeout(() => {
        load();
        onRan();
      }, 4000);
    } catch {
      setMessage("Not queued: Lumon API unreachable.");
    }
  }

  if (loading) return <PanelState state="loading" />;

  return (
    <>
      <Section title="RULE-BASED PIPELINE" meta="baseline · not ML" info={{ ...INFO.changeExplorer, state: provenance ? `last run ${formatTime(provenance.retrieved_at)} · ${provenance.input_ref}` : "no run yet" }}>
        <ol className="workflow">
          <li>Usable observations (cloud/shadow masked by the scene's own SCL layer)</li>
          <li>Jan–May median composite per 100 m tile per year</li>
          <li>Persistent class transitions (≥ 2 years before and after)</li>
          <li>Group neighbouring tiles into candidate events</li>
          <li>Six false-alarm gates: quality, geometry, radiometric, season, size/shape, persistence</li>
          <li>Uncalibrated score → analyst review queue</li>
        </ol>
      </Section>
      <Section title="LAST RUN">
        {provenance ? (
          <Facts rows={[
            ["ENGINE", provenance.processing_version],
            ["RUN AT", formatTime(provenance.retrieved_at)],
            ["INPUT", provenance.input_ref],
            ...Object.entries(provenance.parameters).map(([key, value]) => [key.replace(/_/g, " ").toUpperCase(), JSON.stringify(value)] as [string, string]),
          ]} />
        ) : <div className="empty-note">No run yet.</div>}
        <button type="button" className="action-button" onClick={run}>RE-RUN CHANGE ANALYSIS</button>
        {message && <div className="notice">{message}</div>}
      </Section>
      <Section title="RECENT JOBS">
        {jobs.length === 0 && <div className="empty-note">No change-analysis jobs queued from the UI yet.</div>}
        {jobs.map((job) => (
          <div key={job.id} className="job-row">#{job.id} · {job.status.toUpperCase()} · {formatTime(job.finished_at ?? job.created_at)}{job.error ? ` · ${job.error.split("\n")[0]}` : ""}</div>
        ))}
      </Section>
      <Section title="LIMITS">
        <div className="empty-note">Classes come from spectral-index thresholds and a relative colour rule for built vs soil. They are not validated against ground truth; precision is measured only from analyst decisions (System → status → evaluation).</div>
      </Section>
    </>
  );
}
