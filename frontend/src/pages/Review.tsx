import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Category, MailPage, qs } from "../api";
import { errorText, fmtShort, pct } from "../util";
import { toast } from "../components/Toast";
import MailView from "../components/MailView";

/** Undecided and low-confidence mail, with one-click correction. */
export default function Review() {
  const qc = useQueryClient();
  const [open, setOpen] = useState<number | null>(null);
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });
  const mails = useQuery({
    queryKey: ["mails", "review"],
    queryFn: () => api.get<MailPage>(`/api/mails${qs({ review: true, page_size: 100 })}`),
  });
  const fix = useMutation({
    mutationFn: ({ id, category_id }: { id: number; category_id: number }) =>
      api.patch(`/api/mails/${id}`, { category_id }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["categories"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) => toast(errorText(e)),
  });
  const reclassify = useMutation({
    mutationFn: () => api.post<{ queued: number }>("/api/reclassify", { scope: "undecided" }),
    onSuccess: (r) => toast(`${r.queued} mails will be re-classified at the next run.`),
  });
  const cats = (categories.data ?? []).filter((c) => c.system_key !== "undecided");
  const catById = new Map(categories.data?.map((c) => [c.id, c]));

  if (open) {
    return (
      <div className="content">
        <MailView id={open} onClose={() => setOpen(null)} />
      </div>
    );
  }
  return (
    <div className="content">
      <div className="row">
        <h1 style={{ margin: 0 }}>Review queue</h1>
        <span className="faint">{mails.data?.total ?? 0} mails</span>
        <span className="spacer" />
        <button onClick={() => reclassify.mutate()}>Re-classify Undecided at next run</button>
      </div>
      <p className="muted">
        Undecided mail and low-confidence guesses. Pick the right category; each choice is saved as a correction and
        used to measure (and later tune) the model.
      </p>
      {mails.data?.items.length === 0 && <div className="empty">Nothing to review. 🎉</div>}
      <div className="stack">
        {mails.data?.items.map((m) => {
          const current = m.category_id ? catById.get(m.category_id) : undefined;
          return (
            <div key={m.id} className="card" style={{ marginBottom: 0 }}>
              <div className="row">
                <strong style={{ overflowWrap: "anywhere" }}>{m.subject || "(no subject)"}</strong>
                <span className="spacer" />
                <span className="faint">{fmtShort(m.received_at)} · {m.account_name}</span>
              </div>
              <div className="faint">{m.from_name} &lt;{m.from_address}&gt;</div>
              <div className="muted" style={{ margin: "6px 0", fontSize: 13 }}>{m.snippet}</div>
              <div className="row">
                <span className="badge">{current?.name ?? "—"} {pct(m.confidence)}</span>
                {m.malicious && <span className="badge danger">malicious</span>}
                <button className="small link" onClick={() => setOpen(m.id)}>Open</button>
                <span className="spacer" />
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                {cats.map((c) => (
                  <button key={c.id} className="small" disabled={fix.isPending}
                          onClick={() => fix.mutate({ id: m.id, category_id: c.id })}>
                    {c.name}
                  </button>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
