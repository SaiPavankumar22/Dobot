// Minimal typed client for the Dobot backend. No credentials live here: the frontend only ever asks
// about connection status, never for a key.

import type {
  ActivityRecord,
  AttachmentLimits,
  ApprovalRecord,
  AutomationRecord,
  CanonicalFact,
  ChatResponse,
  DashboardSummary,
  DoctorReport,
  ExecutionMode,
  IdentitySummary,
  InterceptorReport,
  MemoryHit,
  MemoryRecord,
  PermissionMatrix,
  PermissionGrant,
  Region,
  SkillFinding,
  SkillRecord,
  TaskRecord,
  UsageSummary,
  VoiceSummary,
} from "../types";

/** A backend you run yourself (`cd backend && uv run uvicorn app.main:app`). */
export const LOCAL_BACKEND_URL = "http://127.0.0.1:8756";

/**
 * Where the app points before Settings overrides it: this machine's own backend, in every build —
 * so a fresh install is useful the moment it opens, and no repo or binary names a deployment.
 * A backend hosted anywhere else (your own server, container, or Space) is a URL pasted into
 * Settings → Backend, stored per machine in localStorage (`dobot.baseUrl`) along with the token
 * every call carries (`dobot.apiToken`).
 */
export const DEFAULT_BASE_URL = LOCAL_BACKEND_URL;

let baseUrl = localStorage.getItem("dobot.baseUrl") || DEFAULT_BASE_URL;

/**
 * The bearer token sent with every call: the connected backend's `DOBOT_API_TOKEN`, or — for a
 * private Hugging Face Space — your HF read token (its proxy demands the token on every request,
 * WS handshake included).
 *
 * Held in localStorage rather than baked into the build: it is a per-machine secret, and the whole
 * point of both gates is that it is not shipped in the app.
 */
let apiToken = localStorage.getItem("dobot.apiToken") || "";

export function getBaseUrl(): string {
  return baseUrl;
}

export function setBaseUrl(url: string): void {
  baseUrl = url.replace(/\/$/, "");
  localStorage.setItem("dobot.baseUrl", baseUrl);
}

export function getApiToken(): string {
  return apiToken;
}

export function setApiToken(token: string): void {
  apiToken = token.trim();
  if (apiToken) localStorage.setItem("dobot.apiToken", apiToken);
  else localStorage.removeItem("dobot.apiToken");
}

/**
 * The signed-in session on a multi-user backend (`dobot_u_...` from /auth/login or
 * /auth/register), kept separate from `dobot.apiToken` so signing in never clobbers the
 * operator/HF token pasted in Settings — and signing out restores exactly that setup.
 */
export function getUserToken(): string {
  return "";
}

export function setUserToken(token: string): void {
  void token;
}

/** The optional local backend token. Local mode has no account session. */
export function getBearerToken(): string {
  return apiToken;
}

/**
 * The bearer header alone, for raw `fetch`es that do not go through `request()` (voice, onboarding).
 * A private Hugging Face Space rejects every call without it.
 */
export function authHeader(): Record<string, string> {
  const token = getBearerToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function headers(extra?: Record<string, string>): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json", ...extra };
  const token = getBearerToken();
  if (token) base.Authorization = `Bearer ${token}`;
  return base;
}

export class ApiError extends Error {
  code: string;
  recoverable: boolean;
  constructor(code: string, message: string, recoverable = false) {
    super(message);
    this.code = code;
    this.recoverable = recoverable;
  }
}

/** One user-manageable credential, as reported by GET /settings/keys (masked, never raw). */
export type KeyRecord = {
  id: string;
  field: string;
  kind: "secret" | "uri";
  set: boolean;
  masked: string;
};

/**
 * A value that lives on the backend itself (environment / HF Space secret), not in the app: the UI
 * shows where it comes from instead of an empty field nobody can legitimately fill.
 */
export type OperatorKey = {
  id: string;
  set: boolean;
  why: string;
};

export type BackendCapabilities = {
  contract_version: string;
  backend_location: "local" | "hosted" | "remote";
  browser: {
    available: boolean;
    logged_in: boolean | null;
    checked: boolean;
    detail: string;
  };
  hermes: { available: boolean; mode: string; name: string; detail: string };
  local_screen: { available: boolean; mode: string; detail: string };
  local_files: { available: boolean; roots: string[]; detail: string };
  voice: { available: boolean; enabled: boolean; detail: string };
  sandbox: { provider: string; isolation: string; degraded: boolean; detail: string };
};

export type RuntimeStatus = {
  contract_version: string;
  state: "not_started" | "starting" | "ready" | "degraded" | "crashed" | "stopping" | "external" | "unavailable";
  backend_location: "local" | "hosted" | "remote";
  pid: number;
  version: string;
  host: string;
  port: number;
  uptime_seconds: number;
  state_dir: string;
  data_dir: string;
  logs_dir: string;
};

export type HardwareProfile = {
  platform: string;
  architecture: string;
  cpu_threads: number;
  memory_bytes: number;
  free_disk_bytes: number;
  recommended_mode: "cloud" | "hybrid" | "local-or-hybrid";
  local_model_class: string;
  detail: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: headers(init?.headers as Record<string, string> | undefined),
    });
  } catch (error) {
    throw new ApiError(
      "BACKEND_UNREACHABLE",
      `Dobot's backend is not reachable at ${baseUrl}. Is it running?`,
      true,
    );
  }
  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = response.statusText;
    let recoverable = false;
    try {
      const body = await response.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        recoverable = Boolean(body.error.recoverable);
      }
    } catch {
      /* keep the status text */
    }
    if (response.status === 401) {
      message =
        "This backend requires an API token. For a Hugging Face Space, paste your HF read token in Settings; for a local backend, generate one with `uv run python -m app.selftest --new-token`, add it to .env, then paste it in Settings.";
    }
    throw new ApiError(code, message, recoverable);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string; providers: Record<string, string>; degraded: string[] }>("/health"),
  dashboard: () => request<DashboardSummary>("/dashboard"),
  providers: () => request<Record<string, string>>("/settings/providers"),
  capabilities: () => request<BackendCapabilities>("/capabilities"),
  runtime: () => request<RuntimeStatus>("/runtime"),
  runtimeProfile: () => request<HardwareProfile>("/runtime/profile"),
  settings: () => request<Record<string, unknown>>("/settings"),
  updateSettings: (patch: Record<string, unknown>) =>
    request<{ applied: Record<string, unknown> }>("/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  keyList: () => request<{ keys: KeyRecord[]; operator?: OperatorKey[] }>("/settings/keys"),
  keySet: (id: string, value: string) =>
    request<{ ok: boolean; field: string }>(`/settings/keys/${id}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  keyClear: (id: string) =>
    request<{ ok: boolean }>(`/settings/keys/${id}`, { method: "DELETE" }),

  chat: (payload: {
    message: string;
    background?: boolean;
    shadow?: boolean;
    mode?: ExecutionMode;
    region?: Region | null;
    image?: string | null;
    attachments?: { name: string; mime: string; data: string }[];
    source?: string;
  }) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({
        message: payload.message,
        background: payload.background ?? false,
        shadow: payload.shadow,
        mode: payload.mode,
        source: payload.source ?? "desktop",
        context: {
          screen: Boolean(payload.image || payload.region),
          region: payload.region ?? null,
          image: payload.image ?? null,
          attachments: payload.attachments ?? [],
        },
      }),
    }),

  /** The attachment contract (counts, sizes, accepted types) the composer enforces up front. */
  attachmentLimits: () => request<AttachmentLimits>("/chat/attachments"),

  screenAnalyze: (payload: { image?: string | null; region?: Region | null; question: string }) =>
    request<{ answer: string; context_id: string; screen: unknown }>("/screen/analyze", {
      method: "POST",
      body: JSON.stringify({ ...payload, with_reasoning: true }),
    }),

  research: (query: string, depth: "quick" | "deep" = "quick") =>
    request<{ summary: string; sources: { title: string; url: string; snippet: string }[]; degraded: boolean }>(
      "/research",
      { method: "POST", body: JSON.stringify({ query, depth }) },
    ),
  researchSources: () =>
    request<{
      events: { type: string; message: string; task_id: string; timestamp: string; data: Record<string, unknown> }[];
    }>("/research/sources"),
  chatTools: () =>
    request<{ count: number; tools: { name: string; description: string; risk_floor: string }[] }>("/chat/tools"),
  screenPrivacy: () =>
    request<{ mode: string; continuous_monitoring: boolean; captures_on: string[]; retention: string }>(
      "/screen/privacy",
    ),

  tasks: (status?: string) => request<TaskRecord[]>(`/tasks${status ? `?status=${status}` : ""}`),
  task: (id: string) => request<TaskRecord>(`/tasks/${id}`),
  createTask: (payload: { title: string; description?: string; priority?: string }) =>
    request<TaskRecord>("/tasks", { method: "POST", body: JSON.stringify(payload) }),
  cancelTask: (id: string) => request<TaskRecord>(`/tasks/${id}/cancel`, { method: "POST" }),
  runTask: (id: string) => request<TaskRecord>(`/tasks/${id}/run`, { method: "POST" }),
  deleteTask: (id: string) => request<{ deleted: boolean }>(`/tasks/${id}`, { method: "DELETE" }),

  approvals: (includeResolved = false) =>
    request<ApprovalRecord[]>(`/approvals${includeResolved ? "?include_resolved=true" : ""}`),
  decide: (
    id: string,
    decision: "approve" | "reject" | "edit",
    note = "",
    edits?: Record<string, unknown>,
    scope: "once" | "lifetime" = "once",
  ) =>
    request<ChatResponse>(`/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ decision, note, edits, scope }),
    }),

  automations: () => request<AutomationRecord[]>("/automations"),
  createAutomation: (payload: { name: string; schedule: string; task: string }) =>
    request<AutomationRecord>("/automations", { method: "POST", body: JSON.stringify(payload) }),
  setAutomationStatus: (id: string, status: "active" | "paused") =>
    request<AutomationRecord>(`/automations/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  runAutomation: (id: string) => request<{ started: boolean }>(`/automations/${id}/run`, { method: "POST" }),
  deleteAutomation: (id: string) => request<{ deleted: boolean }>(`/automations/${id}`, { method: "DELETE" }),

  memories: (params?: { type?: string; search?: string }) => {
    const query = new URLSearchParams();
    if (params?.type) query.set("type", params.type);
    if (params?.search) query.set("search", params.search);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<MemoryRecord[]>(`/memory${suffix}`);
  },
  memoryStats: () =>
    request<{ total: number; by_type: Record<string, number>; store: string; vectors: string; embedder: string }>(
      "/memory/stats",
    ),
  recall: (q: string) => request<MemoryHit[]>(`/memory/recall?q=${encodeURIComponent(q)}`),
  remember: (payload: { content: string; type?: string; importance?: number; tags?: string[] }) =>
    request<MemoryRecord>("/memory", { method: "POST", body: JSON.stringify(payload) }),
  forget: (id: string) => request<{ deleted: boolean }>(`/memory/${id}`, { method: "DELETE" }),

  skills: () => request<SkillRecord[]>("/skills"),
  runSkill: (name: string) => request<ChatResponse>(`/skills/${name}/run`, { method: "POST" }),
  deleteSkill: (name: string) => request<{ deleted: boolean }>(`/skills/${name}`, { method: "DELETE" }),

  activity: (limit = 100) => request<ActivityRecord[]>(`/activity?limit=${limit}`),
  taskActivity: (taskId: string) => request<ActivityRecord[]>(`/activity/${taskId}`),
  trace: () => request<{ lines: string[]; subscribers: number }>("/debug/trace?limit=80"),

  security: () => request<Record<string, unknown>>("/security/status"),
  permissions: () => request<PermissionMatrix>("/security/permissions"),
  skillScan: () =>
    request<{
      mode: string;
      allowlist: string[];
      blocked: string[];
      flagged: string[];
      reports: { name: string; status: string; findings: SkillFinding[] }[];
    }>("/security/skills"),
  kill: (taskId?: string) =>
    request<{ killed: string[] }>(`/security/kill${taskId ? `?task_id=${taskId}` : ""}`, { method: "POST" }),
  grants: () => request<{ grants: PermissionGrant[] }>("/security/grants"),
  revokeGrant: (id: string) =>
    request<{ revoked: string }>(`/security/grants/${id}`, { method: "DELETE" }),

  // ------------------------------------------------------------------ system

  doctor: () => request<DoctorReport>("/doctor"),
  usage: () => request<UsageSummary>("/system/usage"),
  resetUsage: () => request<{ reset: boolean }>("/system/reset-usage", { method: "POST" }),
  consolidate: (dryRun = false) =>
    request<Record<string, unknown>>(`/system/consolidate?dry_run=${dryRun}`, { method: "POST" }),
  journal: (taskId: string) =>
    request<{ task_id: string; entries: { seq: number; kind: string; detail: string; at: string }[] }>(
      `/system/journal/${taskId}`,
    ),
  traces: (taskId: string) =>
    request<{ task_id: string; count: number; entries: Record<string, unknown>[] }>(
      `/system/traces/${taskId}`,
    ),
  conversation: (conversationId: string, limit = 100) =>
    request<TaskRecord[]>(
      `/conversations/${encodeURIComponent(conversationId)}?limit=${limit}`,
    ),
  storage: () =>
    request<Record<string, unknown>>("/system/storage"),
  pruneStorage: (olderThanDays = 30, includeActivity = false) =>
    request<Record<string, unknown>>(
      `/system/storage/prune?older_than_days=${olderThanDays}&include_activity=${includeActivity}`,
      { method: "POST" },
    ),
  checkpointStorage: () =>
    request<Record<string, unknown>>("/system/storage/checkpoint", { method: "POST" }),
  vacuumStorage: () =>
    request<{ vacuumed: boolean }>("/system/storage/vacuum", { method: "POST" }),
  backupStorage: () =>
    request<{ backup: string; bytes: number }>("/system/storage/backup", { method: "POST" }),
  exportStorage: () =>
    request<{ format: string; created_at: string; collections: Record<string, unknown[]> }>(
      "/system/storage/export",
    ),

  authStatus: () =>
    request<{
      required: boolean;
      header: string;
      multi_user?: boolean;
      open_registration?: boolean;
      accounts?: number | null;
    }>("/auth/status"),

  // ------------------------------------------------------------------ accounts
  login: (name: string, password: string) =>
    request<{ user: { id: string; name: string; role: string }; token: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ name, password }),
    }),
  register: (name: string, password: string, inviteCode = "") =>
    request<{ user: { id: string; name: string; role: string }; token: string }>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ name, password, invite_code: inviteCode }),
    }),
  me: () => request<{ user: { id: string; name: string; role: string } | null; multi_user: boolean }>("/auth/me"),
  createInvite: () => request<{ invite_code: string }>("/auth/invites", { method: "POST" }),

  // ------------------------------------------------------------------ identity

  identity: () => request<IdentitySummary>("/identity"),
  identityDocument: (name: string) =>
    request<{ name: string; content: string }>(`/identity/${encodeURIComponent(name)}`),
  saveIdentityDocument: (name: string, content: string) =>
    request<{ name: string; chars: number; path: string }>(`/identity/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),

  interceptors: () => request<InterceptorReport>("/interceptors"),
  reloadInterceptors: () =>
    request<{ rules: number; errors: string[]; ids: string[] }>("/interceptors/reload", { method: "POST" }),
  testInterceptors: (tool: string, params: Record<string, unknown>) =>
    request<{ outcome: { verdict: string; fired: string[]; reasons: string[] } }>(
      "/interceptors/evaluate",
      { method: "POST", body: JSON.stringify({ tool, params }) },
    ),

  canonical: () =>
    request<{ facts: CanonicalFact[]; stats: Record<string, number>; rendered: string }>("/canonical"),
  stateFact: (payload: { content: string; key?: string; pinned?: boolean }) =>
    request<CanonicalFact>("/canonical", { method: "POST", body: JSON.stringify(payload) }),
  pinFact: (id: string, pinned: boolean) =>
    request<{ id: string; pinned: boolean }>(`/canonical/${id}/pin?pinned=${pinned}`, { method: "POST" }),
  forgetFact: (id: string) => request<{ deleted: boolean }>(`/canonical/${id}`, { method: "DELETE" }),

  // ------------------------------------------------------------------ voice

  voice: () => request<VoiceSummary>("/voice"),
  speak: (text: string, force = false) =>
    request<{ ok: boolean; engine: string; spoken_chars: number }>("/voice/speak", {
      method: "POST",
      body: JSON.stringify({ text, force }),
    }),
};
