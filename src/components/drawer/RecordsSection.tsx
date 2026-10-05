import { useEffect, useState } from "react";
import { getJson } from "../../api";
import { formatTime, labelCase } from "../../utils/format";
import { ListButton, PanelState, Section } from "../intel/common";
import type { Selection } from "../../types";
import { INFO } from "../../data/infoNotes";

// ---------------------------------------------------------------------------
// DECISIONS and AUDIT TRAIL.
//   decisions: every confirm / reject / relabel, newest first
//   audit:     the hash-chained log of analyst and system actions, with a
//              button that re-verifies the whole chain
// ---------------------------------------------------------------------------
type Decision = { id: number; target_id: string; decision: string; new_label: string | null; analyst: string; reason: string | null; decided_at: string; change_class: string | null };
type AuditEntry = { id: number; at: string; actor: string; action: string; target: string | null; hash: string };
type Verify = { ok: boolean; entries: number; broken_at?: number; reason?: string; duplicate_entries?: number[]; note?: string };

export function DecisionsSection({ refreshKey, onSelect }: { refreshKey: number; onSelect: (selection: Selection) => void }) {
  const [rows, setRows] = useState<Decision[] | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    getJson<Decision[]>("/api/decisions").then(setRows).catch(() => setError(true));
  }, [refreshKey]);
  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!rows) return <PanelState state="loading" />;
  return (
    <Section title="ANALYST DECISIONS" meta={`${rows.length}`}>
      {rows.length === 0 && <div className="empty-note">No decisions yet. Open a change event and confirm, reject or relabel it.</div>}
      {rows.map((row) => (
        <ListButton key={row.id}
          title={<><span className={`result-kind result-kind--${row.decision}`}>{row.decision.toUpperCase()}</span>{labelCase(row.change_class)}{row.new_label ? ` → ${row.new_label}` : ""}</>}
          meta={`${formatTime(row.decided_at)} · ${row.analyst}${row.reason ? ` · “${row.reason}”` : ""}`}
          onClick={() => onSelect({ kind: "change", id: row.target_id })} />
      ))}
    </Section>
  );
}

export function AuditSection({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [verify, setVerify] = useState<Verify | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    getJson<AuditEntry[]>("/api/audit?limit=150").then(setRows).catch(() => setError(true));
  }, [refreshKey]);
  if (error) return <PanelState state="error" text="LUMON API UNREACHABLE" />;
  if (!rows) return <PanelState state="loading" />;
  return (
    <>
      <Section title="CHAIN INTEGRITY">
        <button type="button" className="action-button" onClick={() => getJson<Verify>("/api/audit/verify").then(setVerify).catch(() => setVerify(null))}>VERIFY HASH CHAIN</button>
        {verify && (verify.ok
          ? <div className="notice notice--ok">
              INTACT · {verify.entries} entries verified
              {verify.duplicate_entries?.length ? ` · ${verify.duplicate_entries.length} exact duplicate write(s) noted (ids ${verify.duplicate_entries.join(", ")})` : ""}
            </div>
          : <div className="notice notice--error">BROKEN at entry {verify.broken_at}: {verify.reason}</div>)}
      </Section>
      <Section title="AUDIT LOG" meta={`latest ${rows.length}`}
        info={{ ...INFO.audit, state: verify ? (verify.ok ? `chain intact, ${verify.entries} entries${verify.duplicate_entries?.length ? `, duplicates noted: ${verify.duplicate_entries.join(", ")}` : ""}` : `BROKEN at entry ${verify.broken_at}`) : `${rows.length} latest entries shown; press VERIFY to check the chain` }}>
        <table className="table">
          <thead><tr><th>TIME</th><th>ACTOR</th><th>ACTION</th><th>TARGET</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} title={`hash ${row.hash}`}>
                <td>{formatTime(row.at)}</td><td>{row.actor}</td><td>{row.action}</td><td className="table__note">{row.target ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </>
  );
}
