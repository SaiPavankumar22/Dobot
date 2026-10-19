// Who is signed in — the multi-user account layer of the desktop shell.
//
// Probing `/auth/status` first means a single-user backend behaves exactly as before: the
// gate never renders and the pasted-token path in Settings stays untouched. On a
// multi-user backend the session token lives in `dobot.userToken` (services/api) so a
// login can never overwrite the operator/HF token, and signing out restores it.
//
// A failed probe (backend down at startup) resolves to "off" rather than a login wall:
// locking someone out of their own app because the backend was slow to boot would be
// worse than letting the app render and show its usual connection errors.

import { create } from "zustand";
import { ApiError, api, getUserToken, setUserToken } from "../services/api";

export type AuthUser = { id: string; name: string; role: string };

export type AuthPhase = "probing" | "off" | "signed-out" | "signed-in";

type AuthState = {
  phase: AuthPhase;
  user: AuthUser | null;
  /** Accounts on this deployment (null while unknown / accounts off). */
  accounts: number | null;
  openRegistration: boolean;
  busy: boolean;
  error: string | null;
  probe: () => Promise<void>;
  login: (name: string, password: string) => Promise<boolean>;
  register: (name: string, password: string, inviteCode: string) => Promise<boolean>;
  signOut: () => void;
  clearError: () => void;
};

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export const useAuth = create<AuthState>((set, get) => ({
  phase: "probing",
  user: null,
  accounts: null,
  openRegistration: false,
  busy: false,
  error: null,

  probe: async () => {
    try {
      const status = await api.authStatus();
      if (!status.multi_user) {
        set({ phase: "off", user: null, accounts: null, error: null });
        return;
      }
      const base = {
        accounts: status.accounts ?? null,
        openRegistration: Boolean(status.open_registration),
      };
      if (!getUserToken()) {
        set({ phase: "signed-out", user: null, ...base, error: null });
        return;
      }
      try {
        const me = await api.me();
        if (me.user) {
          set({ phase: "signed-in", user: me.user, ...base, error: null });
          return;
        }
      } catch {
        /* stale or rejected token — fall through to sign-in */
      }
      setUserToken("");
      set({ phase: "signed-out", user: null, ...base, error: null });
    } catch (error) {
      // Backend unreachable during startup: render the app; its own connection errors
      // are the honest signal. Never turn a transient failure into a login wall.
      set({ phase: "off", user: null, accounts: null, error: null });
    }
  },

  login: async (name, password) => {
    set({ busy: true, error: null });
    try {
      const result = await api.login(name, password);
      setUserToken(result.token);
      set({ phase: "signed-in", user: result.user, busy: false });
      return true;
    } catch (error) {
      set({ busy: false, error: messageOf(error) });
      return false;
    }
  },

  register: async (name, password, inviteCode) => {
    set({ busy: true, error: null });
    try {
      const result = await api.register(name, password, inviteCode);
      setUserToken(result.token);
      set({
        phase: "signed-in",
        user: result.user,
        busy: false,
        accounts: (get().accounts ?? 0) + 1,
      });
      return true;
    } catch (error) {
      set({ busy: false, error: messageOf(error) });
      return false;
    }
  },

  signOut: () => {
    setUserToken("");
    set({ phase: "signed-out", user: null, error: null });
  },

  clearError: () => set({ error: null }),
}));
