import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, SettingSpec } from "../api";
import { errorText } from "../util";
import { toast } from "../components/Toast";

type Values = Record<string, unknown>;

export default function SettingsPage() {
  const qc = useQueryClient();
  const data = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<{ values: Values; specs: Record<string, SettingSpec> }>("/api/settings"),
  });
  const [draft, setDraft] = useState<Values>({});
  useEffect(() => {
    if (data.data) setDraft(data.data.values);
  }, [data.data]);
  const save = useMutation({
    mutationFn: (changes: Values) => api.put<{ values: Values }>("/api/settings", changes),
    onSuccess: (r) => {
      qc.setQueryData(["settings"], { ...data.data!, values: r.values });
      qc.invalidateQueries({ queryKey: ["status"] });
      toast("Saved. Takes effect at the next run.");
    },
    onError: (e) => toast(errorText(e)),
  });
  const pw = usePasswordChange();

  if (!data.data) return <div className="content">{data.isError ? errorText(data.error) : "Loading…"}</div>;
  const { specs, values } = data.data;
  const groups = new Map<string, string[]>();
  for (const [key, spec] of Object.entries(specs)) {
    groups.set(spec.group, [...(groups.get(spec.group) ?? []), key]);
  }
  const changed = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(values[k]));

  return (
    <div className="content">
      <div className="row" style={{ marginBottom: 12 }}>
        <h1 style={{ margin: 0 }}>Settings</h1>
        <span className="spacer" />
        {changed.length > 0 && <span className="faint">{changed.length} unsaved change(s)</span>}
        <button onClick={() => setDraft(values)} disabled={!changed.length}>Discard</button>
        <button className="primary" disabled={!changed.length || save.isPending}
                onClick={() => save.mutate(Object.fromEntries(changed.map((k) => [k, draft[k]])))}>
          Save
        </button>
      </div>
      <p className="muted">Every value applies at the next run, without a restart. Every change is logged.
        Category timers live under <a href="/categories">Categories & timers</a>.</p>
      {[...groups.entries()].map(([group, keys]) => (
        <div key={group} className="card">
          <h2>{group}</h2>
          <div className="grid-form">
            {keys.map((key) => (
              <Field key={key} spec={specs[key]} value={key in draft ? draft[key] : values[key]}
                     onChange={(v) => setDraft({ ...values, ...draft, [key]: v })} />
            ))}
          </div>
        </div>
      ))}
      <div className="card">
        <h2>Dashboard password</h2>
        <div className="row">
          <input type="password" placeholder="Current password" value={pw.current} onChange={(e) => pw.setCurrent(e.target.value)} />
          <input type="password" placeholder="New password (10+ characters)" value={pw.next} onChange={(e) => pw.setNext(e.target.value)} />
          <button onClick={pw.submit} disabled={pw.next.length < 10 || !pw.current}>Change</button>
        </div>
      </div>
    </div>
  );
}

function usePasswordChange() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const submit = async () => {
    try {
      await api.post("/api/auth/password", { current_password: current, new_password: next });
      toast("Password changed.");
      setCurrent("");
      setNext("");
    } catch (e) {
      toast(errorText(e));
    }
  };
  return { current, setCurrent, next, setNext, submit };
}

function Field({ spec, value, onChange }: { spec: SettingSpec; value: unknown; onChange: (v: unknown) => void }) {
  const label = <label>{spec.label}</label>;
  let input;
  switch (spec.kind) {
    case "bool":
      input = <label className="switch"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
        {value ? "On" : "Off"}</label>;
      break;
    case "int":
    case "float":
      input = (
        <input type="number" value={value as number} step={spec.kind === "float" ? 0.05 : 1}
               min={spec.min ?? undefined} max={spec.max ?? undefined} style={{ width: 140 }}
               onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))} />
      );
      break;
    case "time":
      input = <input type="time" value={value as string} onChange={(e) => onChange(e.target.value)} style={{ width: 140 }} />;
      break;
    case "str_list":
      input = (
        <textarea rows={3} value={(value as string[]).join("\n")} placeholder="one per line, e.g. boss@company.com or @bank.com"
                  onChange={(e) => onChange(e.target.value.split("\n"))} />
      );
      break;
    case "dict":
      input = (
        <div className="row">
          {Object.entries((value ?? {}) as Record<string, boolean>).map(([k, v]) => (
            <label key={k} className="switch">
              <input type="checkbox" checked={v} onChange={(e) => onChange({ ...((value ?? {}) as object), [k]: e.target.checked })} /> {k}
            </label>
          ))}
        </div>
      );
      break;
    default:
      input = <input type="text" value={value as string} onChange={(e) => onChange(e.target.value)} />;
  }
  return (
    <>
      {label}
      <div>{input}</div>
      {spec.help && <div className="help">{spec.help}</div>}
    </>
  );
}
