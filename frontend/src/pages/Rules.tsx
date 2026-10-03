import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Category, Rule } from "../api";
import { errorText } from "../util";
import { toast } from "../components/Toast";
import { Modal } from "../components/Modal";

type Cond = Rule["conditions"][number];
const FIELD_LABELS: Record<string, string> = {
  from_address: "Sender address", from_domain: "Sender domain", from_name: "Sender name", subject: "Subject",
  to: "To", body: "Body", account: "Account name",
};
const OP_LABELS: Record<string, string> = {
  equals: "is", contains: "contains", starts_with: "starts with", ends_with: "ends with", regex: "matches regex",
  domain_in: "is one of the domains", in: "is one of", exists: "exists", not_exists: "does not exist",
};

const condText = (c: Cond) =>
  `${FIELD_LABELS[c.field] ?? c.field} ${OP_LABELS[c.op] ?? c.op}${c.value === null ? "" : ` ${Array.isArray(c.value) ? c.value.join(", ") : c.value}`}`;

export default function Rules() {
  const qc = useQueryClient();
  const rules = useQuery({ queryKey: ["rules"], queryFn: () => api.get<{ rules: Rule[]; ops: string[] }>("/api/rules") });
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => api.get<Category[]>("/api/categories") });
  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/rules/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
    onError: (e) => toast(errorText(e)),
  });
  const catName = new Map(cats.data?.map((c) => [c.id, c.name]));
  return (
    <div className="content">
      <div className="row" style={{ marginBottom: 10 }}>
        <h1 style={{ margin: 0 }}>Rules</h1>
        <span className="spacer" />
        <button className="primary" onClick={() => setEditing("new")}>Add rule</button>
      </div>
      <p className="muted">Rules run before the model, in order; the first match wins. “Assign” sets the category directly,
        “Let the model choose” limits the model to a few categories (e.g. bulk mail).</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Order</th><th>Name</th><th>When all of</th><th>Then</th><th /></tr></thead>
          <tbody>
            {rules.data?.rules.map((r) => (
              <tr key={r.id} style={{ opacity: r.enabled ? 1 : 0.5 }}>
                <td>{r.position}</td>
                <td><strong>{r.name}</strong>{!r.enabled && <span className="badge" style={{ marginLeft: 6 }}>off</span>}</td>
                <td className="wrap">{r.conditions.map((c, i) => <div key={i} className="faint">{condText(c)}</div>)}</td>
                <td>{r.action === "assign" ? `→ ${catName.get(r.category_id!) ?? "?"}`
                  : `model picks from ${r.candidate_category_ids.map((i) => catName.get(i)).join(", ")}`}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="small" onClick={() => setEditing(r)}>Edit</button>{" "}
                  <button className="small danger" onClick={() => confirm(`Delete rule ${r.name}?`) && del.mutate(r.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editing && (
        <RuleEditor rule={editing === "new" ? null : editing} categories={cats.data ?? []} ops={rules.data?.ops ?? []}
                    onClose={() => { setEditing(null); qc.invalidateQueries({ queryKey: ["rules"] }); }} />
      )}
    </div>
  );
}

function RuleEditor({ rule, categories, ops, onClose }: { rule: Rule | null; categories: Category[]; ops: string[]; onClose: () => void }) {
  const [name, setName] = useState(rule?.name ?? "");
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [position, setPosition] = useState<number | "">(rule?.position ?? "");
  const [action, setAction] = useState<Rule["action"]>(rule?.action ?? "assign");
  const [categoryId, setCategoryId] = useState<number>(rule?.category_id ?? categories[0]?.id ?? 0);
  const [candidates, setCandidates] = useState<number[]>(rule?.candidate_category_ids ?? []);
  const [conds, setConds] = useState<Cond[]>(rule?.conditions ?? [{ field: "from_domain", op: "domain_in", value: [] }]);
  const [test, setTest] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fieldOptions = [...Object.keys(FIELD_LABELS), "header:"];
  const setCond = (i: number, patch: Partial<Cond>) => setConds(conds.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const valueText = (c: Cond) => (Array.isArray(c.value) ? c.value.join(", ") : c.value ?? "");
  const normalized = () => conds.map((c) => ({
    ...c,
    value: c.op === "exists" || c.op === "not_exists" ? null
      : c.op === "domain_in" || c.op === "in" ? String(valueText(c)).split(",").map((s) => s.trim()).filter(Boolean)
      : valueText(c),
  }));
  const runTest = async () => {
    try {
      const r = await api.post<{ checked: number; matched: number; examples: { subject: string; from_address: string }[] }>(
        "/api/rules/test", { conditions: normalized() });
      setTest(`${r.matched} of the last ${r.checked} mails match.` +
        (r.examples.length ? ` e.g. “${r.examples[0].subject}” from ${r.examples[0].from_address}` : ""));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  };
  const save = async () => {
    const body = { name, enabled, conditions: normalized(), action, position: position === "" ? null : position,
      category_id: action === "assign" ? categoryId : null, candidate_category_ids: action === "restrict" ? candidates : [] };
    try {
      if (rule) await api.put(`/api/rules/${rule.id}`, body);
      else await api.post("/api/rules", body);
      toast("Rule saved. Applies to new mail; re-classify to apply it to stored mail.");
      onClose();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <Modal title={rule ? `Edit rule ${rule.name}` : "New rule"} onClose={onClose}>
      <div className="stack">
        <div className="row">
          <input type="text" placeholder="Rule name" value={name} onChange={(e) => setName(e.target.value)} style={{ flex: 1 }} />
          <input type="number" placeholder="Order" value={position} style={{ width: 80 }}
                 onChange={(e) => setPosition(e.target.value === "" ? "" : Number(e.target.value))} />
          <label className="switch"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> on</label>
        </div>
        <h3>When all of these match</h3>
        {conds.map((c, i) => {
          const isHeader = c.field.startsWith("header:");
          return (
            <div key={i} className="row">
              <select value={isHeader ? "header:" : c.field} onChange={(e) => setCond(i, { field: e.target.value })}>
                {fieldOptions.map((f) => <option key={f} value={f}>{f === "header:" ? "Header…" : FIELD_LABELS[f]}</option>)}
              </select>
              {isHeader && (
                <input type="text" placeholder="list-unsubscribe" style={{ width: 130 }} value={c.field.slice(7)}
                       onChange={(e) => setCond(i, { field: `header:${e.target.value}` })} />
              )}
              <select value={c.op} onChange={(e) => setCond(i, { op: e.target.value })}>
                {ops.map((o) => <option key={o} value={o}>{OP_LABELS[o] ?? o}</option>)}
              </select>
              {c.op !== "exists" && c.op !== "not_exists" && (
                <input type="text" style={{ flex: 1 }} value={String(valueText(c))}
                       placeholder={c.op === "domain_in" || c.op === "in" ? "comma separated" : "value"}
                       onChange={(e) => setCond(i, { value: e.target.value })} />
              )}
              <button className="small" onClick={() => setConds(conds.filter((_, j) => j !== i))} disabled={conds.length === 1}>×</button>
            </div>
          );
        })}
        <button className="small" onClick={() => setConds([...conds, { field: "subject", op: "contains", value: "" }])}>+ condition</button>
        <h3>Then</h3>
        <div className="row">
          <select value={action} onChange={(e) => setAction(e.target.value as Rule["action"])}>
            <option value="assign">Assign category</option>
            <option value="restrict">Let the model choose between</option>
          </select>
          {action === "assign" && (
            <select value={categoryId} onChange={(e) => setCategoryId(Number(e.target.value))}>
              {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          )}
        </div>
        {action === "restrict" && (
          <div className="row">
            {categories.filter((c) => c.system_key !== "undecided").map((c) => (
              <label key={c.id} className="switch">
                <input type="checkbox" checked={candidates.includes(c.id)}
                       onChange={(e) => setCandidates(e.target.checked ? [...candidates, c.id] : candidates.filter((x) => x !== c.id))} />
                {c.name}
              </label>
            ))}
          </div>
        )}
        {test && <div className="banner info">{test}</div>}
        {error && <div className="error">{error}</div>}
        <div className="row">
          <button onClick={runTest}>Test on recent mail</button>
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save} disabled={!name}>Save</button>
        </div>
      </div>
    </Modal>
  );
}
