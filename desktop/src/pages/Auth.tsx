// Sign-in / sign-up for a multi-user backend, plus the gate that asks for it.
//
// Renders nothing unusual on a single-user backend: `/auth/status` says `multi_user:false`
// and the children (Chat, Dashboard) appear untouched. On a multi-user backend the gate
// blocks both windows until a session exists — the token it receives from /auth/login is
// then carried by every request, the WebSocket handshake, and the event stream.
//
// The look follows the assistant-app convention (Grok, Meta AI): one centered column, a
// breathing dot mark, a sliding Sign in / Create account toggle, generous rounded fields,
// and a single full-width primary button. No social sign-in — this backend authenticates
// locally, and pretending otherwise would be a lie on the button.

import { useEffect, useState, type ReactNode } from "react";
import { IconEye } from "../components/Icons";
import { getBaseUrl } from "../services/api";
import { useAuth } from "../store/authStore";
import { clearThread, loadThread, useDobot } from "../store/dobotStore";

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
    if (phase === "signed-in") {
      // This account's thread — the conversation continues where it left off.
      void loadThread();
      // The event stream was opened with whatever credentials existed at window start (or
      // closed on sign-out): redial once a session exists so the handshake carries the
      // signed-in token — WS cannot pick up a token change mid-connection.
      disconnect();
      connect();
    }
    if (phase === "off") {
      // Single-user backend: one shared thread, restored the same way.
      void loadThread();
    }
    if (phase === "signed-out") {
      // Never leave one account's conversation on screen behind the sign-in card.
      clearThread();
    }
  }, [phase, connect, disconnect]);

  if (phase === "probing") {
    return (
      <div className="auth-shell">
        <div className="auth-card auth-card--splash">
          <span className="auth-dot" aria-hidden="true" />
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
  const [reveal, setReveal] = useState(false);

  const needsInvite = (accounts ?? 0) > 0 && !openRegistration;
  const canSubmit =
    name.trim().length > 0 && password.length >= 8 && (!needsInvite || invite.trim().length > 0);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit || busy) return;
    if (tab === "signin") await login(name.trim(), password);
    else await register(name.trim(), password, invite.trim());
  }

  function switchTab(next: Tab) {
    if (next === tab) return;
    setTab(next);
    setReveal(false);
    clearError();
  }

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-head">
          <span className="auth-dot" aria-hidden="true" />
          <div className="auth-brand">Dobot</div>
          <h1 className="auth-title">{tab === "signin" ? "Welcome back" : "Create your account"}</h1>
          <p className="auth-sub">
            {tab === "signin"
              ? "Sign in to pick up your chats where you left off."
              : "One account for your chats, memory and automations."}
          </p>
        </div>

        <div className="auth-seg" role="tablist" aria-label="Sign in or create account">
          <span
            className={`auth-seg__thumb${tab === "signup" ? " auth-seg__thumb--right" : ""}`}
            aria-hidden="true"
          />
          <button
            type="button"
            role="tab"
            aria-selected={tab === "signin"}
            className={`auth-seg__opt${tab === "signin" ? " is-active" : ""}`}
            onClick={() => switchTab("signin")}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "signup"}
            className={`auth-seg__opt${tab === "signup" ? " is-active" : ""}`}
            onClick={() => switchTab("signup")}
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
          <div className="auth-input-wrap">
            <input
              value={password}
              type={reveal ? "text" : "password"}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={tab === "signin" ? "current-password" : "new-password"}
              placeholder={tab === "signin" ? "your password" : "at least 8 characters"}
            />
            <button
              type="button"
              className="auth-reveal"
              onClick={() => setReveal((shown) => !shown)}
              aria-label={reveal ? "Hide password" : "Show password"}
              tabIndex={-1}
            >
              <IconEye size={16} />
            </button>
          </div>
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
          <p className="auth-hint">
            {accounts === 0
              ? "No accounts exist yet — the first account becomes the owner of this Dobot."
              : "Anyone can create an account on this Dobot."}
          </p>
        )}

        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="auth-submit" disabled={!canSubmit || busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          {busy
            ? "Working…"
            : tab === "signin"
              ? "Sign in"
              : accounts === 0
                ? "Create owner account"
                : "Create account"}
        </button>

        <p className="auth-foot">Backend: {getBaseUrl()}</p>
      </form>
    </div>
  );
}
