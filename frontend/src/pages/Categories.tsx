import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Category } from "../api";
import { days, errorText } from "../util";
import { toast } from "../components/Toast";
import { Modal } from "../components/Modal";
import SenderTimers from "./SenderTimers";

interface Impact {
  before: { mailbox: number; local: number; live: number };
  after: { mailbox: number; local: number; live: number };
  additional: number;
  additional_total: number;
}

type Draft = Partial<Category>;

export default function CategoriesPage() {
  const qc = useQueryClient();
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });
  const [editing, setEditing] = useState<Category | null>(null);
  const [creating, setCreating] = useState(false);
  const [merging, setMerging] = useState<Category | null>(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["categories"] });
    qc.invalidateQueries({ queryKey: ["deletions"] });
  };
  const reorder = useMutation({
    mutationFn: (ids: number[]) => api.post("/api/categories/reorder", { ids }),
    onSuccess: refresh,
    onError: (e) => toast(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/api/categories/${id}`),
    onSuccess: refresh,
    onError: (e) => toast(errorText(e)),
  });
  const reclassify = useMutation({
    mutationFn: (scope: { scope: string; category_id?: number }) => api.post<{ queued: number }>("/api/reclassify", scope),
    onSuccess: (r) => toast(`${r.queued} mails will be re-classified at the next run.`),
  });
  const list = cats.data ?? [];
  const move = (index: number, delta: number) => {
    const ids = list.map((c) => c.id);
    const [id] = ids.splice(index, 1);
    ids.splice(index + delta, 0, id);
    reorder.mutate(ids);
  };

  return (
    <div className="content">
      <div className="row" style={{ marginBottom: 10 }}>
        <h1 style={{ margin: 0 }}>Categories & timers</h1>
        <span className="spacer" />
        <button onClick={() => reclassify.mutate({ scope: "all" })}>Re-classify all at next run</button>
        <button className="primary" onClick={() => setCreating(true)}>Add category</button>
      </div>
      <p className="muted">
        Each mail gets exactly one category. Timers count from the date a mail was received. "Never" means the timer is
        off. Accounts can override timers on the Accounts page.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Order</th><th>Name</th><th className="num">Mails</th><th>Move after</th><th>Delete from mailbox</th>
              <th>Delete local copy</th><th>Write-back</th><th>Model</th><th />
            </tr>
          </thead>
          <tbody>
            {list.map((c, i) => (
              <tr key={c.id}>
                <td>
                  <button className="small" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Up">↑</button>{" "}
                  <button className="small" disabled={i === list.length - 1} onClick={() => move(i, 1)} aria-label="Down">↓</button>
                </td>
                <td>
                  <span className="dot" style={{ background: c.color ?? undefined, display: "inline-block", marginRight: 6 }} />
                  <strong>{c.name}</strong>
                  {c.system_key && <span className="badge" style={{ marginLeft: 6 }}>built-in</span>}
                  <div className="faint">{c.description}</div>
                </td>
                <td className="num">{c.mail_count}</td>
                <td>{days(c.move_days)}</td>
                <td>{days(c.mailbox_delete_days)}</td>
                <td>{days(c.local_delete_days)}</td>
                <td>{c.writeback_enabled ? "on" : "off"}</td>
                <td>{c.model_enabled ? "yes" : "rules only"}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="small" onClick={() => setEditing(c)}>Edit</button>{" "}
                  {!c.system_key && <button className="small" onClick={() => setMerging(c)}>Merge</button>}{" "}
                  {!c.system_key && c.mail_count === 0 && (
                    <button className="small danger" onClick={() => confirm(`Delete ${c.name}?`) && remove.mutate(c.id)}>Delete</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <SenderTimers />
      {editing && <EditCategory category={editing} onClose={() => { setEditing(null); refresh(); }} />}
      {creating && <EditCategory onClose={() => { setCreating(false); refresh(); }} />}
      {merging && (
        <MergeCategory source={merging} categories={list} onClose={() => { setMerging(null); refresh(); }} />
      )}
    </div>
  );
}

function numOrNull(v: string): number | null {
  return v === "" ? null : Math.max(0, Math.round(Number(v)));
}

function EditCategory({ category, onClose }: { category?: Category; onClose: () => void }) {
  const [d, setD] = useState<Draft>(category ?? {
    name: "", description: "", move_days: 1, mailbox_delete_days: null, local_delete_days: null,
    writeback_enabled: true, model_enabled: true, color: "#64748b",
  });
  const [impact, setImpact] = useState<Impact | null>(null);
  const [error, setError] = useState<string | null>(null);
  const save = async (confirmImpact = false) => {
    setError(null);
    const body = {
      name: d.name, description: d.description, move_days: d.move_days, mailbox_delete_days: d.mailbox_delete_days,
      local_delete_days: d.local_delete_days, writeback_enabled: d.writeback_enabled, model_enabled: d.model_enabled,
      color: d.color,
    };
    try {
      if (category) await api.patch(`/api/categories/${category.id}`, { ...body, confirm_impact: confirmImpact });
      else await api.post("/api/categories", body);
      toast("Saved. Takes effect at the next run.");
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && (e.detail as { impact?: Impact })?.impact) {
        setImpact((e.detail as { impact: Impact }).impact);
      } else {
        setError(errorText(e));
      }
    }
  };
  if (impact) {
    return (
      <Modal title="Confirm shorter delete timer" onClose={() => setImpact(null)}>
        <p>With this change the next run will delete <strong>{impact.additional}</strong> more mail(s) for real
          ({impact.after.mailbox} from mailboxes, {impact.after.local} local copies in total for this category,
          including dry-run accounts).</p>
        <p className="faint">Check the deletion preview after saving; you have until the next run to rescue anything.</p>
        <div className="row">
          <span className="spacer" />
          <button onClick={() => setImpact(null)}>Back</button>
          <button className="primary danger" onClick={() => save(true)}>Yes, apply</button>
        </div>
      </Modal>
    );
  }
  const isUndecided = category?.system_key === "undecided";
  return (
    <Modal title={category ? `Edit ${category.name}` : "New category"} onClose={onClose}>
      <div className="grid-form" style={{ gridTemplateColumns: "160px 1fr" }}>
        <label>Name</label>
        <input type="text" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} maxLength={64} />
        <label>Meaning for the model</label>
        <textarea rows={2} value={d.description} onChange={(e) => setD({ ...d, description: e.target.value })} />
        <label>Move after (days)</label>
        <input type="number" min={0} value={d.move_days ?? 1} onChange={(e) => setD({ ...d, move_days: Number(e.target.value) })} />
        <label>Delete from mailbox</label>
        <input type="number" min={0} placeholder="Never" value={d.mailbox_delete_days ?? ""}
               onChange={(e) => setD({ ...d, mailbox_delete_days: numOrNull(e.target.value) })} />
        <label>Delete local copy</label>
        <input type="number" min={0} placeholder="Never" value={d.local_delete_days ?? ""}
               onChange={(e) => setD({ ...d, local_delete_days: numOrNull(e.target.value) })} />
        <label>Write-back</label>
        <label className="switch"><input type="checkbox" checked={!!d.writeback_enabled}
               onChange={(e) => setD({ ...d, writeback_enabled: e.target.checked })} /> move to its folder</label>
        <label>Model may choose it</label>
        <label className="switch"><input type="checkbox" checked={!!d.model_enabled} disabled={isUndecided}
               onChange={(e) => setD({ ...d, model_enabled: e.target.checked })} /> {isUndecided ? "never (fallback)" : "yes"}</label>
        <label>Colour</label>
        <input type="color" value={d.color ?? "#64748b"} onChange={(e) => setD({ ...d, color: e.target.value })} />
      </div>
      {error && <p className="error">{error}</p>}
      <div className="row" style={{ marginTop: 14 }}>
        <span className="faint">Empty delete fields mean Never.</span>
        <span className="spacer" />
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={() => save(false)} disabled={!d.name}>Save</button>
      </div>
    </Modal>
  );
}

function MergeCategory({ source, categories, onClose }: { source: Category; categories: Category[]; onClose: () => void }) {
  const [into, setInto] = useState<number>(categories.find((c) => c.id !== source.id)?.id ?? 0);
  const [error, setError] = useState<string | null>(null);
  const merge = async () => {
    try {
      const r = await api.post<{ mails_moved: number }>(`/api/categories/${source.id}/merge`, { into_id: into });
      toast(`${r.mails_moved} mails merged. The next run moves them to the new folder.`);
      onClose();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <Modal title={`Merge ${source.name}`} onClose={onClose}>
      <p>All mail, rules and corrections of <strong>{source.name}</strong> move to:</p>
      <select value={into} onChange={(e) => setInto(Number(e.target.value))}>
        {categories.filter((c) => c.id !== source.id).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select>
      <p className="faint">{source.name} is removed. The next run moves its mail to the new folder; the old, empty folder is left in place.</p>
      {error && <p className="error">{error}</p>}
      <div className="row"><span className="spacer" /><button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={merge}>Merge</button></div>
    </Modal>
  );
}
