import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import { days, errorText } from "../util";
import { toast } from "../components/Toast";
import { Modal } from "../components/Modal";

interface SenderTimer {
  id: number;
  pattern: string;
  mailbox_delete_days: number | null;
  local_delete_days: number | null;
  enabled: boolean;
  note: string;
}

type Draft = Omit<SenderTimer, "id"> & { id?: number };

const empty: Draft = { pattern: "", mailbox_delete_days: 3, local_delete_days: null, enabled: true, note: "" };

/** Per-sender timers win over category and account timers; protected mail is still never deleted. */
export default function SenderTimers() {
  const qc = useQueryClient();
  const timers = useQuery({ queryKey: ["sender-timers"], queryFn: () => api.get<SenderTimer[]>("/api/sender-timers") });
  const [editing, setEditing] = useState<Draft | null>(null);
  const [confirm, setConfirm] = useState<{ message: string; retry: () => Promise<void> } | null>(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["sender-timers"] });
    qc.invalidateQueries({ queryKey: ["deletions"] });
  };

  // Runs a request; a 409 with an impact asks for confirmation and repeats it with the flag set.
  const withImpact = async (run: (confirmed: boolean) => Promise<unknown>, done: () => void) => {
    try {
      await run(false);
      done();
    } catch (e) {
      const impact = e instanceof ApiError && e.status === 409 ? (e.detail as { impact?: { additional: number } })?.impact : null;
      if (impact) {
        setConfirm({
          message: `The next run will delete ${impact.additional} more mail(s) for real.`,
          retry: async () => {
            try {
              await run(true);
              done();
            } catch (err) {
              toast(errorText(err));
            }
          },
        });
      } else {
        toast(errorText(e));
      }
    }
  };

  const save = (d: Draft) =>
    withImpact(
      (confirmed) => {
        const body = { ...d, confirm_impact: confirmed };
        return d.id ? api.put(`/api/sender-timers/${d.id}`, body) : api.post("/api/sender-timers", body);
      },
      () => {
        setEditing(null);
        refresh();
        toast("Saved. Takes effect at the next run.");
      },
    );
  const remove = (t: SenderTimer) =>
    withImpact(
      (confirmed) => api.del(`/api/sender-timers/${t.id}${confirmed ? "?confirm_impact=true" : ""}`),
      refresh,
    );

  return (
    <div className="card" style={{ marginTop: 20 }}>
      <div className="row">
        <h2 style={{ margin: 0 }}>Sender timers</h2>
        <span className="spacer" />
        <button onClick={() => setEditing({ ...empty })}>Add sender timer</button>
      </div>
      <p className="faint">
        Delete everything from one sender or domain on its own timer, whatever its category. An address
        (news@shop.com) beats a domain (@shop.com, includes subdomains). Starred, Undecided and rescued mail is still
        never deleted. Empty means the category timer applies.
      </p>
      {timers.data?.length === 0 && <div className="faint">No sender timers.</div>}
      {!!timers.data?.length && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Sender</th><th>Delete from mailbox</th><th>Delete local copy</th><th>Note</th><th /></tr></thead>
            <tbody>
              {timers.data.map((t) => (
                <tr key={t.id} style={{ opacity: t.enabled ? 1 : 0.5 }}>
                  <td className="mono">{t.pattern}{!t.enabled && <span className="badge" style={{ marginLeft: 6 }}>off</span>}</td>
                  <td>{t.mailbox_delete_days === null ? "category" : days(t.mailbox_delete_days)}</td>
                  <td>{t.local_delete_days === null ? "category" : days(t.local_delete_days)}</td>
                  <td className="faint">{t.note}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="small" onClick={() => setEditing({ ...t })}>Edit</button>{" "}
                    <button className="small danger" onClick={() => confirm_(`Remove the timer for ${t.pattern}?`) && remove(t)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <Modal title={editing.id ? `Edit ${editing.pattern}` : "New sender timer"} onClose={() => setEditing(null)}>
          <div className="grid-form" style={{ gridTemplateColumns: "160px 1fr" }}>
            <label>Sender or @domain</label>
            <input type="text" value={editing.pattern} placeholder="@shop.com"
                   onChange={(e) => setEditing({ ...editing, pattern: e.target.value })} />
            <label>Delete from mailbox</label>
            <input type="number" min={0} placeholder="category timer" value={editing.mailbox_delete_days ?? ""}
                   onChange={(e) => setEditing({ ...editing, mailbox_delete_days: e.target.value === "" ? null : Number(e.target.value) })} />
            <label>Delete local copy</label>
            <input type="number" min={0} placeholder="category timer" value={editing.local_delete_days ?? ""}
                   onChange={(e) => setEditing({ ...editing, local_delete_days: e.target.value === "" ? null : Number(e.target.value) })} />
            <label>Note</label>
            <input type="text" value={editing.note} onChange={(e) => setEditing({ ...editing, note: e.target.value })} />
            <label>Enabled</label>
            <label className="switch"><input type="checkbox" checked={editing.enabled}
                   onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} /> on</label>
          </div>
          <div className="row" style={{ marginTop: 14 }}>
            <span className="spacer" />
            <button onClick={() => setEditing(null)}>Cancel</button>
            <button className="primary" disabled={!editing.pattern} onClick={() => save(editing)}>Save</button>
          </div>
        </Modal>
      )}
      {confirm && (
        <Modal title="Confirm deletion change" onClose={() => setConfirm(null)}>
          <p>{confirm.message}</p>
          <p className="faint">Check the deletion preview afterwards; you have until the next run to rescue anything.</p>
          <div className="row">
            <span className="spacer" />
            <button onClick={() => setConfirm(null)}>Back</button>
            <button className="primary danger" onClick={async () => { const c = confirm; setConfirm(null); await c.retry(); }}>
              Yes, apply
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

const confirm_ = (msg: string) => window.confirm(msg);
