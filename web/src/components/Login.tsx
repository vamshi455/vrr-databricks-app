/**
 * Sign-in. Shown when a write needs a token and there isn't one.
 *
 * It is NOT a wall in front of the whole app: reads stay public, so you can look at the
 * portfolio, the attribution and the lineage without an account. You sign in to *act* —
 * ask the agent, draft a change, move it along the approval chain.
 *
 * The role you get is the role your account has — you cannot choose it here, which is
 * the point of the screen existing at all.
 *
 * The account hints below name the demo USERNAMES only. A password rendered in the login
 * page would be published by every deployment of it; `make users` prints the one it set,
 * which is a place only the operator sees.
 */
import { useState } from "react";
import { api, type Identity } from "../api";
import { Banner } from "./ui";

const DEMO = [
  ["analyst.demo", "drafts, first sign-off"],
  ["rm.demo", "second sign-off"],
  ["site.demo", "the only role that may execute"],
];

export function Login({ onSignedIn, onCancel, reason }: {
  onSignedIn: (who: Identity) => void;
  onCancel?: () => void;
  reason?: string;
}) {
  const [username, setUsername] = useState("analyst.demo");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.login(username, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <form onSubmit={submit}
            className="w-full max-w-sm rounded-lg border border-surface-border bg-surface-card p-6 shadow-lg">
        <h2 className="text-base font-semibold">🛢️ Sign in</h2>
        <p className="mt-1 text-label leading-relaxed text-content-muted">
          {reason ?? "Reading is open. Asking the agent and moving an approval need an account — your role comes from it."}
        </p>

        {error && <div className="mt-3"><Banner tone="bad" title={error} /></div>}

        <label className="mt-4 block text-label font-medium text-content-secondary">Username</label>
        <input
          className="mt-1 w-full rounded border border-surface-border px-2 py-1.5 text-body"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
          autoComplete="username"
        />

        <label className="mt-3 block text-label font-medium text-content-secondary">Password</label>
        <input
          type="password"
          className="mt-1 w-full rounded border border-surface-border px-2 py-1.5 text-body"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />

        <div className="mt-4 flex gap-2">
          <button
            type="submit"
            disabled={busy || !password}
            className="rounded bg-brand-500 px-3 py-1.5 text-body font-medium text-surface-base disabled:opacity-40"
          >
            {busy ? "signing in…" : "Sign in"}
          </button>
          {onCancel && (
            <button type="button" onClick={onCancel}
                    className="rounded border border-surface-border px-3 py-1.5 text-body">
              Keep reading
            </button>
          )}
        </div>

        <div className="mt-5 border-t border-surface-divider pt-3">
          <p className="text-micro font-medium text-content-secondary">Demo accounts</p>
          <ul className="mt-1 space-y-0.5 text-micro text-content-muted">
            {DEMO.map(([name, what]) => (
              <li key={name}>
                <button type="button" onClick={() => setUsername(name)}
                        className="font-mono underline">{name}</button> — {what}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-micro text-content-muted">
            Seed them with <code className="font-mono">make users</code>, which prints the
            password it set.
          </p>
        </div>
      </form>
    </div>
  );
}
