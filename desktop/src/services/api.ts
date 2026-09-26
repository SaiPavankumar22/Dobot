// Minimal typed client for the Dobot backend. No credentials live here: the frontend only ever asks
// about connection status, never for a key.

import type {
  ActivityRecord,
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
  Region,
  SkillFinding,
  SkillRecord,
  TaskRecord,
  UsageSummary,
  VoiceSummary,
} from "../types";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8756";

let baseUrl = localStorage.getItem("dobot.baseUrl") || DEFAULT_BASE_URL;

/**
 * The optional bearer token, when the backend's DOBOT_API_TOKEN gate is on.
 *
 * Held in localStorage rather than baked into the build: it is a per-machine secret, and the whole
 * point of the gate is that it is not shipped in the app.
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

function headers(extra?: Record<string, string>): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json", ...extra };
  if (apiToken) base.Authorization = `Bearer ${apiToken}`;
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

/** One manageable credential, as reported by GET /settings/keys (always masked, never raw). */
export type KeyRecord = {
  id: string;
  field: string;
  kind: "secret" | "uri";
  set: boolean;
  masked: string;
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
        "This backend requires an API token. Generate one with `uv run python -m app.selftest --new-token`, add it to .env, then paste it in Settings.";
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
  settings: () => request<Record<string, unknown>>("/settings"),
  updateSettings: (patch: Record<string, unknown>) =>
    request<{ applied: Record<string, unknown> }>("/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  keyList: () => request<{ keys: KeyRecord[] }>("/settings/keys"),
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
        },
      }),
    }),

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

  tasks: (status?: string) => request<TaskRecord[]>(`/tasks${status ? `?status=${status}` : ""}`),
  task: (id: string) => request<TaskRecord>(`/tasks/${id}`),
  createTask: (payload: { title: string; description?: string; priority?: string }) =>
    request<TaskRecord>("/tasks", { method: "POST", body: JSON.stringify(payload) }),
  cancelTask: (id: string) => request<TaskRecord>(`/tasks/${id}/cancel`, { method: "POST" }),
  runTask: (id: string) => request<TaskRecord>(`/tasks/${id}/run`, { method: "POST" }),
  deleteTask: (id: string) => request<{ deleted: boolean }>(`/tasks/${id}`, { method: "DELETE" }),

  approvals: (includeResolved = false) =>
    request<ApprovalRecord[]>(`/approvals${includeResolved ? "?include_resolved=true" : ""}`),
  decide: (id: string, decision: "approve" | "reject" | "edit", note = "", edits?: Record<string, unknown>) =>
    request<ChatResponse>(`/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ decision, note, edits }),
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

  authStatus: () => request<{ required: boolean; header: string }>("/auth/status"),

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
