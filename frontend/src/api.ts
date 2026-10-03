export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      "X-Requested-With": "mailcat",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    window.dispatchEvent(new Event("mailcat:logout"));
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail ?? res.statusText;
    throw new ApiError(res.status, Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export interface Category {
  id: number;
  name: string;
  description: string;
  position: number;
  system_key: string | null;
  move_days: number;
  mailbox_delete_days: number | null;
  local_delete_days: number | null;
  writeback_enabled: boolean;
  model_enabled: boolean;
  color: string | null;
  mail_count: number;
  unread_count: number;
}

export interface MailSummary {
  id: number;
  account_id: number;
  account_name: string;
  from_name: string;
  from_address: string;
  subject: string;
  snippet: string;
  received_at: string;
  seen: boolean;
  flagged: boolean;
  category_id: number | null;
  confidence: number | null;
  decided_by: string | null;
  malicious: boolean;
  needs_action: boolean;
  has_attachments: boolean;
  folder_role: string;
  folder: string | null;
  keep: boolean;
}

export interface MailPage {
  items: MailSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface LinkInfo {
  id: number;
  shown_text: string;
  url: string;
  host: string;
  verdict: "ok" | "suspicious" | "malicious";
  reasons: string[];
}

export interface AttachmentInfo {
  id: number;
  filename: string;
  content_type: string;
  size: number;
  scan_status: string;
  scan_detail: string | null;
  risky: boolean;
  locked: boolean;
  sha256: string;
}

export interface MailDetail {
  id: number;
  account_id: number;
  account_name: string;
  message_id: string;
  from_name: string;
  from_address: string;
  reply_to: string | null;
  to: string;
  cc: string;
  subject: string;
  received_at: string;
  size: number;
  body_text: string;
  has_html: boolean;
  seen: boolean;
  flagged: boolean;
  folder_role: string;
  folder: string | null;
  category_id: number | null;
  confidence: number | null;
  decided_by: string | null;
  decision_detail: string | null;
  probabilities: Record<string, number> | null;
  category_locked: boolean;
  classifier_version: string | null;
  malicious: boolean;
  malicious_reasons: string[];
  phishing_score: number | null;
  needs_action: boolean;
  keep: boolean;
  hold_writeback: boolean;
  moved_at: string | null;
  mailbox_deleted_at: string | null;
  auth: { spf: string | null; dkim: string | null; dmarc: string | null; warnings: string[] };
  list_unsubscribe: boolean;
  links: LinkInfo[];
  attachments: AttachmentInfo[];
  thread: { id: number; subject: string; from_address: string; received_at: string; folder_role: string }[];
}

export interface Account {
  id: number;
  name: string;
  provider: string;
  email: string;
  host: string;
  port: number;
  security: string;
  username: string;
  auth_method: string;
  sync_enabled: boolean;
  writeback_enabled: boolean;
  writeback_dry_run: boolean;
  deletion_enabled: boolean;
  deletion_dry_run: boolean;
  backfill_enabled: boolean;
  sorted_parent: string;
  timer_overrides: Record<string, Record<string, number | null>>;
  folder_overrides: Record<string, string>;
  last_sync_at: string | null;
  last_sync_ok: boolean | null;
  last_sync_error: string | null;
  consecutive_failures: number;
  mail_count: number;
}

export interface RunInfo {
  id: number;
  kind: string;
  triggered_by: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  counts: Record<string, number>;
  errors: { step: string; message: string; account: string | null; at: string }[];
}

export interface Status {
  last_run: RunInfo | null;
  running: RunInfo | null;
  sync_requested: boolean;
  next_run_at: string | null;
  runs_paused: boolean;
  accounts: { id: number; name: string; last_sync_at: string | null; last_sync_ok: boolean | null;
    last_sync_error: string | null; sync_enabled: boolean }[];
  blocklists: Record<string, { updated_at: string; entries: number }>;
  review_count: number;
}

export interface Rule {
  id: number;
  name: string;
  position: number;
  enabled: boolean;
  conditions: { field: string; op: string; value: string | string[] | null }[];
  action: "assign" | "restrict";
  category_id: number | null;
  candidate_category_ids: number[];
}

export interface SettingSpec {
  default: unknown;
  kind: string;
  label: string;
  help: string;
  min: number | null;
  max: number | null;
  group: string;
}
