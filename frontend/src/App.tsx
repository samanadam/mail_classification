import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Category, Status } from "./api";
import { fmtDate } from "./util";
import { Toast, useToast } from "./components/Toast";
import Login from "./pages/Login";
import Inbox from "./pages/Inbox";
import Review from "./pages/Review";
import DeletionPreview from "./pages/DeletionPreview";
import Activity from "./pages/Activity";
import SettingsPage from "./pages/Settings";
import CategoriesPage from "./pages/Categories";
import Accounts from "./pages/Accounts";
import Rules from "./pages/Rules";

export default function App() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api.get<{ username: string }>("/api/auth/me"),
    retry: false,
  });
  useEffect(() => {
    const onLogout = () => qc.setQueryData(["me"], null);
    window.addEventListener("mailcat:logout", onLogout);
    return () => window.removeEventListener("mailcat:logout", onLogout);
  }, [qc]);

  if (me.isLoading) return <div className="empty">Loading…</div>;
  const loggedOut = !me.data || (me.error instanceof ApiError && me.error.status === 401);
  if (loggedOut) return <Login onLogin={() => qc.invalidateQueries()} />;
  return <Shell username={me.data!.username} />;
}

function Shell({ username }: { username: string }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const [menuOpen, setMenuOpen] = useState(false);
  const [search, setSearch] = useState(() => new URLSearchParams(location.search).get("q") ?? "");

  const categories = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });
  const status = useQuery({
    queryKey: ["status"],
    queryFn: () => api.get<Status>("/api/status"),
    refetchInterval: (q) => (q.state.data?.running || q.state.data?.sync_requested ? 4000 : 60000),
  });
  const wasRunning = status.data?.running;
  useEffect(() => {
    if (!wasRunning) qc.invalidateQueries({ queryKey: ["mails"] });
  }, [wasRunning, qc]);
  useEffect(() => setMenuOpen(false), [location.pathname]);

  const sync = useMutation({
    mutationFn: () => api.post("/api/runs/sync"),
    onSuccess: () => {
      toast.show("Sync requested; it starts within a few seconds.");
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) => toast.show(`Sync failed: ${(e as Error).message}`),
  });
  const logout = async () => {
    await api.post("/api/auth/logout");
    qc.setQueryData(["me"], null);
  };

  const s = status.data;
  const failing = s?.accounts.filter((a) => a.sync_enabled && a.last_sync_ok === false) ?? [];

  return (
    <div className="app">
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <div className="brand">
          <img src="/favicon.svg" width={22} height={22} alt="" /> Mail Categorizer
        </div>
        <NavLink end to="/" className="nav-item">All mail</NavLink>
        <NavLink to="/review" className="nav-item">
          Review queue <span className="count">{s?.review_count ?? ""}</span>
        </NavLink>
        <div className="nav-section">Categories</div>
        {categories.data?.map((c) => (
          <NavLink key={c.id} to={`/c/${c.id}`} className="nav-item">
            <span className="dot" style={{ background: c.color ?? undefined }} />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
            <span className="count">{c.unread_count || ""}</span>
          </NavLink>
        ))}
        <div className="nav-section">Views</div>
        <NavLink to="/view/needs-action" className="nav-item">Needs action</NavLink>
        <NavLink to="/view/malicious" className="nav-item">Flagged malicious</NavLink>
        <NavLink to="/view/misfiled" className="nav-item">Provider spam, maybe real</NavLink>
        <NavLink to="/view/sent" className="nav-item">Sent</NavLink>
        <div className="nav-section">Manage</div>
        <NavLink to="/deletions" className="nav-item">Deletion preview</NavLink>
        <NavLink to="/activity" className="nav-item">Activity</NavLink>
        <NavLink to="/categories" className="nav-item">Categories & timers</NavLink>
        <NavLink to="/rules" className="nav-item">Rules</NavLink>
        <NavLink to="/accounts" className="nav-item">Accounts</NavLink>
        <NavLink to="/settings" className="nav-item">Settings</NavLink>
        <div className="spacer" />
        <div className="faint" style={{ padding: "10px" }}>
          Signed in as {username} · <button className="link" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <button className="menu-btn" onClick={() => setMenuOpen((v) => !v)} aria-label="Menu">☰</button>
          <form
            style={{ flex: 1, display: "flex" }}
            onSubmit={(e) => {
              e.preventDefault();
              nav(search.trim() ? `/search?q=${encodeURIComponent(search.trim())}` : "/");
            }}
          >
            <input
              type="search"
              placeholder="Search all accounts…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ flex: 1 }}
              aria-label="Search"
            />
          </form>
          <button className="primary" onClick={() => sync.mutate()} disabled={!!s?.running || !!s?.sync_requested}>
            {s?.running ? "Syncing…" : s?.sync_requested ? "Queued…" : "Sync now"}
          </button>
          <div className="status">
            {s?.last_run ? (
              <>
                Last run {fmtDate(s.last_run.finished_at ?? s.last_run.started_at)} ·{" "}
                <span className={s.last_run.status === "ok" ? "ok-text" : "error"}>{s.last_run.status}</span>
              </>
            ) : (
              "No run yet"
            )}
            <br />
            {s?.runs_paused ? "Scheduled runs paused" : s?.next_run_at ? `Next ${fmtDate(s.next_run_at)}` : ""}
          </div>
        </div>
        {s?.read_only && (
          <div className="banner warn" style={{ margin: "10px 18px 0" }}>
            Read-only mode (MAILCAT_READ_ONLY): mailboxes are never changed. Moves and deletions only show what
            they would do.
          </div>
        )}
        {failing.length > 0 && (
          <div className="banner danger" style={{ margin: "10px 18px 0" }}>
            Sync failing for {failing.map((a) => a.name).join(", ")}: {failing[0].last_sync_error}
          </div>
        )}
        <Routes>
          <Route path="/" element={<Inbox key="all" filter={{}} title="All mail" />} />
          <Route path="/c/:categoryId" element={<CategoryInbox />} />
          <Route path="/search" element={<SearchInbox />} />
          <Route path="/view/needs-action" element={<Inbox key="na" filter={{ needs_action: true }} title="Needs action" />} />
          <Route path="/view/malicious" element={<Inbox key="mal" filter={{ malicious: true }} title="Flagged malicious" />} />
          <Route path="/view/misfiled" element={<Inbox key="mis" filter={{ misfiled: true }} title="Provider spam that looks real" />} />
          <Route path="/view/sent" element={<Inbox key="sent" filter={{ role: "sent" }} title="Sent" />} />
          <Route path="/mail/:mailId" element={<Inbox key="single" filter={{}} title="All mail" />} />
          <Route path="/review" element={<Review />} />
          <Route path="/deletions" element={<DeletionPreview />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/categories" element={<CategoriesPage />} />
          <Route path="/rules" element={<Rules />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      <Toast message={toast.message} />
    </div>
  );
}

function CategoryInbox() {
  const id = Number(useLocation().pathname.split("/")[2]);
  const qc = useQueryClient();
  const cats = qc.getQueryData<Category[]>(["categories"]);
  const name = cats?.find((c) => c.id === id)?.name ?? "Category";
  return <Inbox key={`c${id}`} filter={{ category_id: id }} title={name} />;
}

function SearchInbox() {
  const q = new URLSearchParams(useLocation().search).get("q") ?? "";
  return <Inbox key={`q${q}`} filter={{ q }} title={`Search: ${q}`} />;
}
