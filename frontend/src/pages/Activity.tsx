import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, qs, RunInfo } from "../api";
import { fmtDate } from "../util";

interface ActionRow {
  id: number;
  run_id: number | null;
  action: string;
  from_folder: string | null;
  to_folder: string | null;
  rule: string | null;
  sender: string | null;
  subject: string | null;
  mail_date: string | null;
  dry_run: boolean;
  verified: boolean | null;
  error: string | null;
  undone_at: string | null;
  created_at: string;
}

interface SettingRow {
  id: number;
  key: string;
  old_value: unknown;
  new_value: unknown;
  changed_by: string | null;
  created_at: string;
}

const show = (v: unknown) => (v === null || v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));

export default function Activity() {
  const [tab, setTab] = useState<"runs" | "actions" | "settings">("runs");
  const [action, setAction] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [runId, setRunId] = useState<number | null>(null);
  const [page, setPage] = useState(1);

  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api.get<RunInfo[]>("/api/runs?limit=60"),
                          enabled: tab === "runs" });
  const actions = useQuery({
    queryKey: ["activity", action, errorsOnly, runId, page],
    queryFn: () => api.get<{ total: number; items: ActionRow[] }>(
      `/api/activity${qs({ kind: "actions", action, errors_only: errorsOnly || undefined, run_id: runId, page })}`),
    enabled: tab === "actions",
  });
  const settings = useQuery({
    queryKey: ["activity-settings", page],
    queryFn: () => api.get<{ total: number; items: SettingRow[] }>(`/api/activity${qs({ kind: "settings", page })}`),
    enabled: tab === "settings",
  });

  return (
    <div className="content">
      <h1>Activity</h1>
      <div className="tabs" style={{ marginTop: 0, marginBottom: 14 }}>
        <button className={tab === "runs" ? "active" : ""} onClick={() => { setTab("runs"); setPage(1); }}>Runs</button>
        <button className={tab === "actions" ? "active" : ""} onClick={() => { setTab("actions"); setPage(1); }}>
          Moves & deletes
        </button>
        <button className={tab === "settings" ? "active" : ""} onClick={() => { setTab("settings"); setPage(1); }}>
          Settings changes
        </button>
      </div>

      {tab === "runs" && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Kind</th><th>Started</th><th>Finished</th><th>Status</th><th>Counts</th><th>Errors</th></tr></thead>
            <tbody>
              {runs.data?.map((r) => (
                <tr key={r.id}>
                  <td><button className="link" onClick={() => { setRunId(r.id); setTab("actions"); }}>{r.id}</button></td>
                  <td>{r.kind}{r.triggered_by ? ` (${r.triggered_by})` : ""}</td>
                  <td>{fmtDate(r.started_at)}</td>
                  <td>{fmtDate(r.finished_at)}</td>
                  <td><span className={`badge ${r.status === "ok" ? "ok" : r.status === "running" ? "accent" : "danger"}`}>{r.status}</span></td>
                  <td className="faint wrap">
                    {Object.entries(r.counts).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")}
                  </td>
                  <td className="wrap">
                    {r.errors.slice(0, 5).map((e, i) => (
                      <div key={i} className="error" style={{ fontSize: 12 }}>
                        [{e.step}{e.account ? ` ${e.account}` : ""}] {e.message}
                      </div>
                    ))}
                    {r.errors.length > 5 && <div className="faint">+{r.errors.length - 5} more</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="faint">To reverse a run's moves and deletes: <code>docker compose exec worker mailcat undo &lt;run #&gt;</code></p>
        </div>
      )}

      {tab === "actions" && (
        <>
          <div className="row" style={{ marginBottom: 10 }}>
            <select value={action} onChange={(e) => { setAction(e.target.value); setPage(1); }} aria-label="Action">
              <option value="">All actions</option>
              {["move", "delete", "local_delete", "undo_move", "undo_delete", "folder_create", "folder_rename"].map((a) => (
                <option key={a} value={a}>{a.replace("_", " ")}</option>
              ))}
            </select>
            <label className="switch">
              <input type="checkbox" checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)} /> Errors only
            </label>
            {runId && <span className="badge accent">run #{runId} <button className="link" onClick={() => setRunId(null)}>×</button></span>}
            <span className="spacer" />
            <span className="faint">{actions.data?.total ?? 0} entries</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>When</th><th>Run</th><th>Action</th><th>From → to</th><th>Mail</th><th>Rule</th><th>Result</th></tr></thead>
              <tbody>
                {actions.data?.items.map((a) => (
                  <tr key={a.id}>
                    <td>{fmtDate(a.created_at)}</td>
                    <td>{a.run_id}</td>
                    <td>{a.action}{a.dry_run && <span className="badge" style={{ marginLeft: 4 }}>dry-run</span>}</td>
                    <td className="wrap">{a.from_folder ?? "—"} → {a.to_folder ?? "—"}</td>
                    <td className="wrap"><div>{a.subject}</div><div className="faint">{a.sender} · {fmtDate(a.mail_date)}</div></td>
                    <td className="faint">{a.rule}</td>
                    <td>
                      {a.error ? <span className="error">{a.error}</span>
                        : a.dry_run ? <span className="faint">planned</span>
                        : a.verified ? <span className="ok-text">verified</span> : <span className="faint">—</span>}
                      {a.undone_at && <div className="faint">undone {fmtDate(a.undone_at)}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager page={page} total={actions.data?.total ?? 0} size={100} onPage={setPage} />
        </>
      )}

      {tab === "settings" && (
        <>
          <div className="table-wrap">
            <table>
              <thead><tr><th>When</th><th>Setting</th><th>Old</th><th>New</th><th>By</th></tr></thead>
              <tbody>
                {settings.data?.items.map((s) => (
                  <tr key={s.id}>
                    <td>{fmtDate(s.created_at)}</td>
                    <td className="mono">{s.key}</td>
                    <td className="wrap mono">{show(s.old_value)}</td>
                    <td className="wrap mono">{show(s.new_value)}</td>
                    <td>{s.changed_by}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager page={page} total={settings.data?.total ?? 0} size={100} onPage={setPage} />
        </>
      )}
    </div>
  );
}

function Pager({ page, total, size, onPage }: { page: number; total: number; size: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size));
  if (pages <= 1) return null;
  return (
    <div className="pager">
      <button className="small" disabled={page <= 1} onClick={() => onPage(page - 1)}>Newer</button>
      Page {page} of {pages}
      <button className="small" disabled={page >= pages} onClick={() => onPage(page + 1)}>Older</button>
    </div>
  );
}
