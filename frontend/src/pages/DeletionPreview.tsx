import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { errorText, fmtDate } from "../util";
import { toast } from "../components/Toast";

interface PreviewItem {
  kind: "mailbox" | "local";
  mode: "live" | "dry_run" | "off";
  account_name: string;
  category_name: string;
  rule: string;
  message_id: string;
  received_at: string | null;
  mail_id: number | null;
  from_address: string | null;
  subject: string;
}

interface Preview {
  items: PreviewItem[];
  total: number;
  live_count: number;
  cap: number;
  over_cap: boolean;
}

export default function DeletionPreview() {
  const qc = useQueryClient();
  const preview = useQuery({ queryKey: ["deletions"], queryFn: () => api.get<Preview>("/api/deletions/preview") });
  const rescue = useMutation({
    mutationFn: (id: number) => api.patch(`/api/mails/${id}`, { keep: true }),
    onSuccess: () => {
      toast("Rescued: this mail will never be deleted.");
      qc.invalidateQueries({ queryKey: ["deletions"] });
    },
    onError: (e) => toast(errorText(e)),
  });
  const p = preview.data;
  return (
    <div className="content">
      <h1>Deletion preview</h1>
      <p className="muted">
        What the next run will delete. Mailbox deletion moves mail to the provider's Trash (recoverable until the
        provider purges it). Starred, Undecided and rescued mail is never deleted.
      </p>
      {p && (
        <div className="kpis">
          <div className="kpi"><div className="label">Will be deleted for real</div><div className="value">{p.live_count}</div></div>
          <div className="kpi"><div className="label">Daily cap</div><div className="value">{p.cap}</div></div>
          <div className="kpi"><div className="label">Planned (incl. dry-run)</div><div className="value">{p.total}</div></div>
        </div>
      )}
      {p?.over_cap && (
        <div className="banner danger">
          Over the daily cap: the next run will delete nothing and send an alert. Rescue mail, fix timers or raise the cap.
        </div>
      )}
      {preview.isError && <div className="error">{errorText(preview.error)}</div>}
      {p && p.items.length === 0 && <div className="empty">Nothing is due for deletion.</div>}
      {p && p.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Where</th><th>Mode</th><th>Account</th><th>Received</th><th>From / subject</th><th>Rule</th><th /></tr>
            </thead>
            <tbody>
              {p.items.map((i) => (
                <tr key={`${i.kind}-${i.message_id}-${i.account_name}`}>
                  <td>{i.kind === "mailbox" ? "Mailbox → Trash" : "Local copy"}</td>
                  <td>
                    <span className={`badge ${i.mode === "live" ? "danger" : ""}`}>
                      {i.mode === "live" ? "will delete" : "dry-run (logged only)"}
                    </span>
                  </td>
                  <td>{i.account_name}</td>
                  <td>{fmtDate(i.received_at)}</td>
                  <td className="wrap">
                    <div>{i.subject}</div>
                    <div className="faint">{i.from_address}</div>
                  </td>
                  <td className="faint">{i.rule}</td>
                  <td>
                    {i.mail_id && (
                      <button className="small" onClick={() => rescue.mutate(i.mail_id!)} disabled={rescue.isPending}>
                        Rescue
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
