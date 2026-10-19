// Sign-in / sign-up for a multi-user backend, plus the gate that asks for it.
//
// Renders nothing unusual on a single-user backend: `/auth/status` says `multi_user:false`
// and the children (Chat, Dashboard) appear untouched. On a multi-user backend the gate
// blocks both windows until a session exists — the token it receives from /auth/login is
// then carried by every request, the WebSocket handshake, and the event stream.

import { useEffect, useState, type ReactNode } from "react";
import { getBaseUrl } from "../services/api";
import { useAuth } from "../store/authStore";
import { useDobot } from "../store/dobotStore";

/** Blocking wrapper: probing → splash, multi-user without a session → the sign-in card. */
export function AuthGate({ children }: { children: ReactNode }) {
  const phase = useAuth((state) => state.phase);
  const probe = useAuth((state) => state.probe);
  const connect = useDobot((state) => state.connect);
  const disconnect = useDobot((state) => state.disconnect);

  useEffect(() => {
    void probe();
  }, [probe]);

  useEffect(() => {
    // The event stream was opened with whatever credentials existed at window start (or
    // closed on sign-out): redial once a session exists so the handshake carries the
    // signed-in token — WS cannot pick up a token change mid-connection.
    if (phase === "signed-in") {
      disconnect();
      connect();
    }
  }, [phase, connect, disconnect]);

  if (phase === "probing") {
    return (
      <div className="auth-shell">
        <div className="auth-card" style={{ textAlign: "center" }}>
          <div className="auth-brand">Dobot</div>
          <p className="subtle">Connecting to the backend…</p>
        </div>
      </div>
    );
  }
  if (phase === "signed-out") return <AuthPage />;
  return <>{children}</>;
}

type Tab = "signin" | "signup";

export function AuthPage() {
  const { accounts, openRegistration, busy, error, login, register, clearError } = useAuth();
  // No accounts yet → this run IS the owner signup; otherwise signing in is the default.
  const [tab, setTab] = useState<Tab>(accounts === 0 ? "signup" : "signin");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");

  const needsInvite = (accounts ?? 0) > 0 && !openRegistration;
  const canSubmit =
    name.trim().length > 0 && password.length >= 8 && (!needsInvite || invite.trim().length > 0);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit || busy) return;
    if (tab === "signin") await login(name.trim(), password);
    else await register(name.trim(), password, invite.trim());
  }

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">Dobot</div>
        <p className="subtle" style={{ marginTop: 0 }}>
          {tab === "signin" ? "Sign in to your account" : "Create your account"}
        </p>

        <div className="auth-tabs">
          <button
            type="button"
            className={tab === "signin" ? "auth-tab active" : "auth-tab"}
            onClick={() => {
              setTab("signin");
              clearError();
            }}
          >
            Sign in
          </button>
          <button
            type="button"
            className={tab === "signup" ? "auth-tab active" : "auth-tab"}
            onClick={() => {
              setTab("signup");
              clearError();
            }}
          >
            Create account
          </button>
        </div>

        <label className="auth-field">
          <span>Name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoComplete="username"
            autoFocus
            placeholder="your name"
          />
        </label>

        <label className="auth-field">
          <span>Password</span>
          <input
            value={password}
            type="password"
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={tab === "signin" ? "current-password" : "new-password"}
            placeholder="at least 8 characters"
          />
        </label>

        {tab === "signup" && needsInvite && (
          <label className="auth-field">
            <span>Invite code</span>
            <input
              value={invite}
              onChange={(event) => setInvite(event.target.value)}
              autoComplete="off"
              placeholder="ask the owner for an invite"
            />
          </label>
        )}

        {tab === "signup" && !needsInvite && (
          <p className="subtle" style={{ margin: "4px 0 0" }}>
            {accounts === 0
              ? "No accounts exist yet — the first account becomes the owner of this Dobot."
              : "Anyone can create an account on this Dobot."}
          </p>
        )}

        {error && <p className="auth-error">{error}</p>}

        <button type="submit" className="auth-submit" disabled={!canSubmit || busy}>
          {busy
            ? "Working…"
            : tab === "signin"
              ? "Sign in"
              : accounts === 0
                ? "Create owner account"
                : "Create account"}
        </button>

        <p className="subtle auth-backend">Backend: {getBaseUrl()}</p>
      </form>
    </div>
  );
}
