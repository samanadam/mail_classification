import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Category, MailDetail } from "../api";
import { errorText, fmtBytes, fmtDate, pct } from "../util";
import { toast } from "./Toast";

const DECIDED_BY: Record<string, string> = {
  rule: "Rule", model: "Model", thread: "Thread (reply inherits)", security: "Security checks",
  correction: "Your correction", threshold: "Below confidence threshold", fallback: "No model answer",
};

/** Sandboxed: no scripts, no same-origin, no forms, no navigation. Remote content is already stripped. */
function HtmlView({ id }: { id: number }) {
  const html = useQuery({
    queryKey: ["mail-html", id],
    queryFn: () => api.get<{ html: string; blocked_images: number }>(`/api/mails/${id}/html`),
  });
  if (html.isLoading) return <div className="faint">Loading HTML…</div>;
  if (html.isError) return <div className="error">{errorText(html.error)}</div>;
  const doc = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<style>body{font:14px/1.5 system-ui,sans-serif;color:#111;margin:14px;overflow-wrap:anywhere}
.mc-link{color:#1f4f99;text-decoration:underline dotted}.mc-link-dest{color:#888;font-size:11px}
.mc-img-blocked{color:#999;font-size:11px;border:1px dashed #ccc;padding:0 4px}
table{max-width:100%}</style></head><body>${html.data!.html}</body></html>`;
  return (
    <>
      {html.data!.blocked_images > 0 && (
        <div className="faint" style={{ marginTop: 8 }}>{html.data!.blocked_images} remote image(s) blocked.</div>
      )}
      <iframe className="html-frame" sandbox="" srcDoc={doc} title="Mail HTML" referrerPolicy="no-referrer" />
    </>
  );
}

export default function MailView({ id, onClose }: { id: number; onClose: () => void }) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"text" | "html" | "links" | "details">("text");
  const [confirmAtt, setConfirmAtt] = useState<number | null>(null);
  const mail = useQuery({ queryKey: ["mail", id], queryFn: () => api.get<MailDetail>(`/api/mails/${id}`) });
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });

  const update = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch(`/api/mails/${id}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mail", id] });
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["categories"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) => toast(errorText(e)),
  });

  if (mail.isLoading) return <div className="faint">Loading…</div>;
  if (mail.isError) return <div className="error">{errorText(mail.error)}</div>;
  const m = mail.data!;
  const cats = categories.data ?? [];
  const probs = Object.entries(m.probabilities ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const badLinks = m.links.filter((l) => l.verdict !== "ok");

  const download = (attId: number, risky: boolean) => {
    if (risky && confirmAtt !== attId) {
      setConfirmAtt(attId);
      return;
    }
    setConfirmAtt(null);
    window.location.href = `/api/mails/attachments/${attId}/download${risky ? "?confirm_risky=true" : ""}`;
  };

  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <button className="small" onClick={onClose}>← Back</button>
        <span className="spacer" />
        <span className="badge">{m.account_name}</span>
        <span className="badge">{m.folder ?? m.folder_role}</span>
        {m.flagged && <span className="badge accent">★ starred</span>}
        {!m.seen && <span className="badge">unread in mailbox</span>}
      </div>
      <h1 className="subject">{m.subject || "(no subject)"}</h1>

      {m.malicious && (
        <div className="banner danger" role="alert">
          <strong>Warning: this mail looks malicious.</strong> Attachments are locked and downloads are blocked.
          <ul>{m.malicious_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}
      {!m.malicious && m.auth.warnings.length > 0 && (
        <div className="banner warn">Sender checks: {m.auth.warnings.join("; ")}</div>
      )}
      {m.mailbox_deleted_at && (
        <div className="banner info">Deleted from the mailbox (moved to Trash) on {fmtDate(m.mailbox_deleted_at)}.</div>
      )}
      {m.hold_writeback && (
        <div className="banner info">Held in place after an undo. Change the category (or release) to let write-back move it.{" "}
          <button className="small" onClick={() => update.mutate({ hold_writeback: false })}>Release</button>
        </div>
      )}

      <div className="meta">
        <span>From</span><b>{m.from_name ? `${m.from_name} <${m.from_address}>` : m.from_address}</b>
        {m.reply_to && m.reply_to !== m.from_address && (<><span>Reply-To</span><b>{m.reply_to}</b></>)}
        <span>To</span><b>{m.to || "—"}</b>
        {m.cc && (<><span>Cc</span><b>{m.cc}</b></>)}
        <span>Date</span><b>{fmtDate(m.received_at)}</b>
        <span>Checks</span>
        <b>
          SPF {m.auth.spf ?? "?"} · DKIM {m.auth.dkim ?? "?"} · DMARC {m.auth.dmarc ?? "?"}
          {m.phishing_score !== null && ` · phishing ${pct(m.phishing_score)}`}
        </b>
      </div>

      {m.folder_role !== "sent" && (
        <div className="classify-box">
          <label className="row">
            Category
            <select
              value={m.category_id ?? ""}
              onChange={(e) => update.mutate({ category_id: Number(e.target.value) })}
              disabled={update.isPending}
              aria-label="Category"
            >
              {m.category_id === null && <option value="">—</option>}
              {cats.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <span className="badge">{pct(m.confidence)}</span>
          {m.needs_action && <span className="badge warn">needs action</span>}
          {m.category_locked && <span className="badge ok">corrected</span>}
          <span className="spacer" />
          <label className="switch" title="Never delete this mail, whatever the timers say">
            <input type="checkbox" checked={m.keep} onChange={(e) => update.mutate({ keep: e.target.checked })} />
            Keep forever
          </label>
          <div className="reason">
            {DECIDED_BY[m.decided_by ?? ""] ?? m.decided_by ?? "Not classified yet"}
            {m.decision_detail ? ` — ${m.decision_detail}` : ""}
          </div>
          {probs.length > 0 && (
            <div style={{ flexBasis: "100%" }} className="stack">
              {probs.map(([name, p]) => (
                <div key={name} className="prob-bar">
                  <span>{name}</span>
                  <span className="bar"><span style={{ width: `${Math.round(p * 100)}%` }} /></span>
                  <span className="faint">{pct(p)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {m.attachments.length > 0 && (
        <div className="card" style={{ padding: "10px 14px" }}>
          <h3 style={{ marginTop: 0 }}>Attachments</h3>
          {m.attachments.map((a) => (
            <div key={a.id} className="row" style={{ padding: "4px 0" }}>
              <span style={{ overflowWrap: "anywhere" }}>{a.filename}</span>
              <span className="faint">{a.content_type} · {fmtBytes(a.size)}</span>
              {a.scan_status === "infected" && <span className="badge danger">virus: {a.scan_detail}</span>}
              {a.scan_status === "clean" && <span className="badge ok">scanned clean</span>}
              {(a.scan_status === "error" || a.scan_status === "skipped" || a.scan_status === "pending") && (
                <span className="badge warn">not scanned</span>
              )}
              {a.risky && <span className="badge warn">risky type</span>}
              <span className="spacer" />
              {a.locked ? (
                <span className="badge danger">locked</span>
              ) : (
                <button className={`small ${confirmAtt === a.id ? "danger" : ""}`} onClick={() => download(a.id, a.risky || a.scan_status !== "clean")}>
                  {confirmAtt === a.id
                    ? `${a.risky ? "Risky file type" : "Not virus-scanned"} — click again to download`
                    : "Download"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="tabs" role="tablist">
        <button className={tab === "text" ? "active" : ""} onClick={() => setTab("text")}>Text</button>
        {m.has_html && <button className={tab === "html" ? "active" : ""} onClick={() => setTab("html")}>HTML (safe)</button>}
        <button className={tab === "links" ? "active" : ""} onClick={() => setTab("links")}>
          Links ({m.links.length}){badLinks.length ? ` · ${badLinks.length} flagged` : ""}
        </button>
        <button className={tab === "details" ? "active" : ""} onClick={() => setTab("details")}>Details</button>
      </div>

      {tab === "text" && <div className="body-text">{m.body_text || <span className="faint">(empty body)</span>}</div>}
      {tab === "html" && <HtmlView id={m.id} />}
      {tab === "links" && (
        <div style={{ marginTop: 12 }}>
          <p className="faint">Links are never clickable here. Copy a destination only if you trust it.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Shown as</th><th>Real destination</th><th>Check</th></tr></thead>
              <tbody>
                {m.links.map((l) => (
                  <tr key={l.id}>
                    <td className="wrap">{l.shown_text || <span className="faint">(plain URL)</span>}</td>
                    <td className="wrap mono">{l.url}</td>
                    <td>
                      <span className={`badge ${l.verdict === "malicious" ? "danger" : l.verdict === "suspicious" ? "warn" : "ok"}`}>
                        {l.verdict}
                      </span>
                      {l.reasons.map((r, i) => <div key={i} className="faint">{r}</div>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {tab === "details" && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="meta" style={{ gridTemplateColumns: "140px 1fr" }}>
            <span>Message-ID</span><b className="mono">{m.message_id}</b>
            <span>Size</span><b>{fmtBytes(m.size)}</b>
            <span>Classifier</span><b>{m.classifier_version ?? "—"}</b>
            <span>Moved</span><b>{fmtDate(m.moved_at)}</b>
            <span>Newsletter</span><b>{m.list_unsubscribe ? "has List-Unsubscribe" : "no"}</b>
          </div>
          {m.thread.length > 0 && (
            <>
              <h3>Same conversation</h3>
              {m.thread.map((t) => (
                <div key={t.id} className="faint">
                  {fmtDate(t.received_at)} · {t.from_address} · {t.subject} {t.folder_role === "sent" && "(sent)"}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
