import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Account, api, Category } from "../api";
import { errorText, fmtDate } from "../util";
import { toast } from "../components/Toast";
import { Modal } from "../components/Modal";

interface Provider { key: string; label: string; host: string; port: number; auth_method: string; notes: string }
interface TestResult {
  ok: boolean; error?: string; roles?: Record<string, string | null>; capabilities?: string[];
  inbox_messages?: number; warnings?: string[]; folder_count?: number;
}

const mode = (enabled: boolean, dry: boolean) => (!enabled ? "off" : dry ? "dry-run" : "LIVE");

export default function Accounts() {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const [adding, setAdding] = useState(false);
  const [timers, setTimers] = useState<Account | null>(null);
  const [tests, setTests] = useState<Record<number, TestResult | "running">>({});
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<Account> & { password?: string } }) =>
      api.patch<Account>(`/api/accounts/${id}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["deletions"] });
      toast("Saved. Takes effect at the next run.");
    },
    onError: (e) => toast(errorText(e)),
  });
  const test = async (id: number) => {
    setTests((t) => ({ ...t, [id]: "running" }));
    try {
      const r = await api.post<TestResult>(`/api/accounts/${id}/test`);
      setTests((t) => ({ ...t, [id]: r }));
    } catch (e) {
      setTests((t) => ({ ...t, [id]: { ok: false, error: errorText(e) } }));
    }
  };
  const remove = async (a: Account) => {
    const typed = prompt(`This removes every LOCAL copy of ${a.name}'s mail (the mailbox is not touched). Type the account name to confirm:`);
    if (typed !== a.name) return;
    try {
      await api.del(`/api/accounts/${a.id}?confirm=${encodeURIComponent(a.name)}`);
      qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      toast(errorText(e));
    }
  };
  const toggle = (a: Account, key: keyof Account, value: boolean) => {
    if (key === "deletion_dry_run" && !value &&
        !confirm(`Turn on LIVE deletion for ${a.name}? Mail will be moved to Trash when its timer expires.`)) return;
    if (key === "writeback_dry_run" && !value &&
        !confirm(`Turn on LIVE write-back for ${a.name}? Inbox mail will be moved into category folders.`)) return;
    patch.mutate({ id: a.id, body: { [key]: value } });
  };

  return (
    <div className="content">
      <div className="row" style={{ marginBottom: 10 }}>
        <h1 style={{ margin: 0 }}>Accounts</h1>
        <span className="spacer" />
        <button className="primary" onClick={() => setAdding(true)}>Add account</button>
      </div>
      <p className="muted">
        New accounts are read-only. Turn write-back and deletion on in dry-run mode first, review the planned actions in
        Activity after a run, then switch them to live.
      </p>
      {accounts.data?.length === 0 && <div className="empty">No accounts yet.</div>}
      {accounts.data?.map((a) => {
        const t = tests[a.id];
        return (
          <div key={a.id} className="card">
            <div className="row">
              <h2 style={{ margin: 0 }}>{a.name}</h2>
              <span className="badge">{a.provider}</span>
              <span className="faint">{a.email} · {a.host}:{a.port} · {a.mail_count} mails stored</span>
              <span className="spacer" />
              <span className={`badge ${a.last_sync_ok === false ? "danger" : a.last_sync_ok ? "ok" : ""}`}>
                {a.last_sync_ok === false ? `failing (${a.consecutive_failures}x)` : a.last_sync_ok ? "syncing" : "not synced yet"}
              </span>
            </div>
            {a.last_sync_error && <div className="error" style={{ fontSize: 12, marginTop: 6 }}>{a.last_sync_error}</div>}
            <div className="faint" style={{ marginTop: 4 }}>Last sync {fmtDate(a.last_sync_at)} · folders under “{a.sorted_parent}”</div>
            <div className="row" style={{ marginTop: 12, gap: 18 }}>
              <label className="switch"><input type="checkbox" checked={a.sync_enabled}
                     onChange={(e) => toggle(a, "sync_enabled", e.target.checked)} /> Sync</label>
              <label className="switch"><input type="checkbox" checked={a.backfill_enabled}
                     onChange={(e) => toggle(a, "backfill_enabled", e.target.checked)} /> Import full history</label>
              <span>
                Write-back: <strong>{mode(a.writeback_enabled, a.writeback_dry_run)}</strong>{" "}
                <label className="switch"><input type="checkbox" checked={a.writeback_enabled}
                       onChange={(e) => toggle(a, "writeback_enabled", e.target.checked)} /> on</label>{" "}
                <label className="switch"><input type="checkbox" checked={a.writeback_dry_run}
                       onChange={(e) => toggle(a, "writeback_dry_run", e.target.checked)} /> dry-run</label>
              </span>
              <span>
                Deletion: <strong className={a.deletion_enabled && !a.deletion_dry_run ? "error" : ""}>
                  {mode(a.deletion_enabled, a.deletion_dry_run)}</strong>{" "}
                <label className="switch"><input type="checkbox" checked={a.deletion_enabled}
                       onChange={(e) => toggle(a, "deletion_enabled", e.target.checked)} /> on</label>{" "}
                <label className="switch"><input type="checkbox" checked={a.deletion_dry_run}
                       onChange={(e) => toggle(a, "deletion_dry_run", e.target.checked)} /> dry-run</label>
              </span>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="small" onClick={() => test(a.id)} disabled={t === "running"}>
                {t === "running" ? "Testing…" : "Test connection"}
              </button>
              <button className="small" onClick={() => setTimers(a)}>Timer overrides ({Object.keys(a.timer_overrides).length})</button>
              {a.auth_method === "password" && (
                <button className="small" onClick={() => {
                  const pw = prompt("New app password");
                  if (pw) patch.mutate({ id: a.id, body: { password: pw } });
                }}>Change password</button>
              )}
              <span className="spacer" />
              <button className="small danger" onClick={() => remove(a)}>Remove</button>
            </div>
            {t && t !== "running" && (
              <div className={`banner ${t.ok ? "info" : "danger"}`}>
                {t.ok ? (
                  <>
                    Connected. {t.inbox_messages} messages in INBOX, {t.folder_count} folders.
                    <div className="mono" style={{ marginTop: 4 }}>
                      {Object.entries(t.roles ?? {}).map(([k, v]) => `${k}: ${v ?? "not found"}`).join(" · ")}
                    </div>
                    {t.warnings?.map((w, i) => <div key={i} className="error">{w}</div>)}
                  </>
                ) : `Connection failed: ${t.error}`}
              </div>
            )}
          </div>
        );
      })}
      {adding && <AddAccount onClose={() => { setAdding(false); qc.invalidateQueries({ queryKey: ["accounts"] }); }} />}
      {timers && <TimerOverrides account={timers} onClose={() => setTimers(null)}
                                 onSave={(o) => { patch.mutate({ id: timers.id, body: { timer_overrides: o } }); setTimers(null); }} />}
    </div>
  );
}

function AddAccount({ onClose }: { onClose: () => void }) {
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => api.get<Provider[]>("/api/accounts/providers") });
  const [f, setF] = useState({ name: "", provider: "gmail", email: "", host: "", port: 993, security: "ssl",
    username: "", password: "", oauth_client_id: "", oauth_tenant: "", sorted_parent: "Sorted" });
  const [error, setError] = useState<string | null>(null);
  const p = providers.data?.find((x) => x.key === f.provider);
  const submit = async () => {
    setError(null);
    try {
      await api.post("/api/accounts", {
        ...f, host: f.host || undefined, username: f.username || undefined,
        password: f.password || undefined, oauth_client_id: f.oauth_client_id || undefined,
        oauth_tenant: f.oauth_tenant || undefined,
      });
      toast(p?.auth_method === "oauth2"
        ? "Added. Finish the login with: docker compose exec worker mailcat account oauth " + f.name
        : "Added in read-only mode.");
      onClose();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <Modal title="Add account" onClose={onClose}>
      <div className="grid-form" style={{ gridTemplateColumns: "140px 1fr" }}>
        <label>Provider</label>
        <select value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value })}>
          {providers.data?.map((x) => <option key={x.key} value={x.key}>{x.label}</option>)}
        </select>
        {p?.notes && <div className="help">{p.notes}</div>}
        <label>Name</label>
        <input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. Personal Gmail" />
        <label>Email</label>
        <input type="text" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
        <label>IMAP host</label>
        <input type="text" value={f.host} placeholder={p?.host || "imap.example.com"} onChange={(e) => setF({ ...f, host: e.target.value })} />
        <label>Port / security</label>
        <div className="row">
          <input type="number" value={f.port} style={{ width: 90 }} onChange={(e) => setF({ ...f, port: Number(e.target.value) })} />
          <select value={f.security} onChange={(e) => setF({ ...f, security: e.target.value })}>
            <option value="ssl">SSL/TLS</option><option value="starttls">STARTTLS</option>
          </select>
        </div>
        <label>Username</label>
        <input type="text" value={f.username} placeholder="defaults to the email" onChange={(e) => setF({ ...f, username: e.target.value })} />
        {p?.auth_method === "oauth2" ? (
          <>
            <label>Azure client id</label>
            <input type="text" value={f.oauth_client_id} onChange={(e) => setF({ ...f, oauth_client_id: e.target.value })} />
            <label>Tenant</label>
            <input type="text" value={f.oauth_tenant} placeholder="common" onChange={(e) => setF({ ...f, oauth_tenant: e.target.value })} />
          </>
        ) : (
          <>
            <label>App password</label>
            <input type="password" value={f.password} autoComplete="new-password" onChange={(e) => setF({ ...f, password: e.target.value })} />
          </>
        )}
        <label>Folder parent</label>
        <input type="text" value={f.sorted_parent} onChange={(e) => setF({ ...f, sorted_parent: e.target.value })} />
      </div>
      {error && <p className="error">{error}</p>}
      <div className="row" style={{ marginTop: 14 }}>
        <span className="faint">Credentials are stored encrypted.</span>
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={submit} disabled={!f.name || !f.email}>Add</button>
      </div>
    </Modal>
  );
}

type Overrides = Record<string, Record<string, number | null>>;
const KEYS: [string, string][] = [["move_days", "Move"], ["mailbox_delete_days", "Mailbox delete"], ["local_delete_days", "Local delete"]];

function TimerOverrides({ account, onClose, onSave }: { account: Account; onClose: () => void; onSave: (o: Overrides) => void }) {
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });
  const [o, setO] = useState<Overrides>(account.timer_overrides);
  const set = (cid: number, key: string, raw: string) => {
    const id = String(cid);
    const entry: Record<string, number | null> = { ...(o[id] ?? {}) };
    if (raw === "") delete entry[key];
    else if (raw === "never") entry[key] = null;
    else if (/^\d+$/.test(raw)) entry[key] = Number(raw);
    else return;
    const next: Overrides = { ...o, [id]: entry };
    if (!Object.keys(entry).length) delete next[id];
    setO(next);
  };
  const shown = (cid: number, key: string) => {
    const v = o[String(cid)]?.[key];
    return v === undefined ? "" : v === null ? "never" : String(v);
  };
  return (
    <Modal title={`Timer overrides: ${account.name}`} onClose={onClose}>
      <p className="faint">Empty = use the category default. Type a number of days or “never”.</p>
      <div className="table-wrap" style={{ maxHeight: 380, overflowY: "auto" }}>
        <table>
          <thead><tr><th>Category</th>{KEYS.map(([, l]) => <th key={l}>{l}</th>)}</tr></thead>
          <tbody>
            {cats.data?.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                {KEYS.map(([k]) => (
                  <td key={k}>
                    <input type="text" style={{ width: 70 }} value={shown(c.id, k)}
                           placeholder={String((c as unknown as Record<string, number | null>)[k] ?? "never")}
                           onChange={(e) => set(c.id, k, e.target.value.trim())} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={() => onSave(o)}>Save</button>
      </div>
    </Modal>
  );
}
