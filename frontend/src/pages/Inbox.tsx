import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Account, api, Category, MailPage, qs } from "../api";
import { fmtShort } from "../util";
import MailView from "../components/MailView";

export type MailFilter = Partial<{
  category_id: number;
  needs_action: boolean;
  malicious: boolean;
  misfiled: boolean;
  review: boolean;
  role: string;
  q: string;
}>;

export default function Inbox({ filter, title }: { filter: MailFilter; title: string }) {
  const [params, setParams] = useSearchParams();
  const { mailId } = useParams();
  const nav = useNavigate();
  const selected = Number(params.get("open") ?? mailId ?? 0) || null;
  const [page, setPage] = useState(1);
  const [account, setAccount] = useState<string>("");
  const [read, setRead] = useState<string>("");
  const [flag, setFlag] = useState<string>("");

  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api.get<Account[]>("/api/accounts") });
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });
  const query = {
    ...filter,
    account_id: account || undefined,
    seen: read === "" ? undefined : read === "read",
    flagged: flag === "flagged" ? true : undefined,
    has_attachments: flag === "attachments" ? true : undefined,
    page,
    page_size: 50,
  };
  const mails = useQuery({
    queryKey: ["mails", query],
    queryFn: () => api.get<MailPage>(`/api/mails${qs(query)}`),
    placeholderData: (prev) => prev,
  });
  const catById = new Map(categories.data?.map((c) => [c.id, c]));
  const open = (id: number) => {
    const next = new URLSearchParams(params);
    next.set("open", String(id));
    setParams(next, { replace: false });
  };
  const close = () => {
    if (mailId) nav("/");
    const next = new URLSearchParams(params);
    next.delete("open");
    setParams(next);
  };
  const data = mails.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className={`content split ${selected ? "has-selection" : ""}`}>
      <div className="mail-list">
        <div className="filters">
          <strong style={{ flexBasis: "100%" }}>
            {title} <span className="faint">{data ? `${data.total} mails` : ""}</span>
          </strong>
          <select value={account} onChange={(e) => { setAccount(e.target.value); setPage(1); }} aria-label="Account">
            <option value="">All accounts</option>
            {accounts.data?.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
          <select value={read} onChange={(e) => { setRead(e.target.value); setPage(1); }} aria-label="Read state">
            <option value="">Read and unread</option>
            <option value="unread">Unread</option>
            <option value="read">Read</option>
          </select>
          <select value={flag} onChange={(e) => { setFlag(e.target.value); setPage(1); }} aria-label="Flags">
            <option value="">Any</option>
            <option value="flagged">Starred</option>
            <option value="attachments">With attachments</option>
          </select>
        </div>
        {mails.isError && <div className="empty error">{(mails.error as Error).message}</div>}
        {data && data.items.length === 0 && <div className="empty">No mail here.</div>}
        {data?.items.map((m) => {
          const cat = m.category_id ? catById.get(m.category_id) : undefined;
          return (
            <a key={m.id} className={`mail-row ${m.seen ? "" : "unread"} ${selected === m.id ? "selected" : ""}`}
               onClick={() => open(m.id)} role="button" tabIndex={0}
               onKeyDown={(e) => e.key === "Enter" && open(m.id)}>
              <div className="line1">
                <span className="from">{m.from_name || m.from_address}</span>
                <span className="date">{fmtShort(m.received_at)}</span>
              </div>
              <div className="subject">{m.subject || "(no subject)"}</div>
              <div className="snippet">{m.snippet}</div>
              <div className="tags">
                {cat && !filter.category_id && (
                  <span className="badge"><span className="dot" style={{ background: cat.color ?? undefined }} />{cat.name}</span>
                )}
                {m.malicious && <span className="badge danger">malicious</span>}
                {m.needs_action && <span className="badge warn">needs action</span>}
                {m.flagged && <span className="badge accent">★</span>}
                {m.has_attachments && <span className="badge">📎</span>}
                {m.folder_role === "spam" && <span className="badge">provider spam</span>}
                {m.folder_role === "missing" && <span className="badge">moved by hand</span>}
                {m.folder_role === "trash" && <span className="badge">in trash</span>}
                <span className="badge">{m.account_name}</span>
              </div>
            </a>
          );
        })}
        {data && pages > 1 && (
          <div className="pager">
            <button className="small" disabled={page <= 1} onClick={() => setPage(page - 1)}>Newer</button>
            Page {page} of {pages}
            <button className="small" disabled={page >= pages} onClick={() => setPage(page + 1)}>Older</button>
          </div>
        )}
      </div>
      <div className="mail-view">
        {selected ? (
          <MailView id={selected} onClose={close} />
        ) : (
          <div className="empty">Select a mail to read it. <br /><Link to="/review">Open the review queue</Link></div>
        )}
      </div>
    </div>
  );
}
