import { FormEvent, useState } from "react";
import { api } from "../api";
import { errorText } from "../util";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/auth/login", { username, password });
      onLogin();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form onSubmit={submit}>
        <h1>Mail Categorizer</h1>
        <div className="faint">Private dashboard. Sign in to continue.</div>
        <input type="text" placeholder="Username" autoComplete="username" value={username}
               onChange={(e) => setUsername(e.target.value)} autoFocus required />
        <input type="password" placeholder="Password" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} required />
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
