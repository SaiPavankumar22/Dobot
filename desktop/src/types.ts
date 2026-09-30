// Types mirror the backend contract (backend/app/schemas.py). Keep them in sync by hand — there are
// few enough that a code generator would be more machinery than the project needs.

export type RiskLevel = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type Verdict = "ALLOW" | "APPROVAL" | "BLOCK";
export type TaskStatus =
  | "PENDING"
  | "PLANNING"
  | "IN_PROGRESS"
  | "WAITING_APPROVAL"
  | "WAITING_USER"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";
export type StepStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "SKIPPED"
  | "BLOCKED"
  | "WAITING_APPROVAL";
export type DotStatus =
  | "IDLE"
  | "LISTENING"
  | "THINKING"
  | "EXECUTING"
  | "APPROVAL_REQUIRED"
  | "COMPLETED"
  | "ERROR"
  | "KILLED";
export type MemoryType = "preference" | "project" | "person" | "fact" | "workflow" | "episode" | "task";

/**
 * How much Dobot may do without asking, per message.
 * ask    — answer and plan, execute nothing.
 * assist — confirm anything beyond a read.
 * agent  — the default: safe and reversible work proceeds, the risky stops for you.
 */
export type ExecutionMode = "ask" | "assist" | "agent";

export interface Region {
  x: number;
  y: number;
  width: number;
  height: number;
  monitor?: number;
}

/**
 * A file or image attached to a message. `data` is the base64 payload the backend accepts; images
 * also keep their data URL so the composer and the thread can render a thumbnail without a round
 * trip. Limits are the backend's (GET /chat/attachments).
 */
export interface ChatAttachment {
  id: string;
  name: string;
  mime: string;
  kind: "image" | "text";
  size: number;
  data: string;
  previewUrl?: string;
}

/** The published attachment contract the composer pre-checks against. */
export interface AttachmentLimits {
  images: { max_count: number; max_bytes: number; types: string[] };
  files: { max_count: number; max_bytes: number; extensions: string[] };
  max_chars_in_prompt: number;
  accepted: { image_types: string[]; text_extensions: string[] };
}

export interface PlanStep {
  id: string;
  index: number;
  reason: string;
  action: {
    tool: string;
    params: Record<string, unknown>;
    description: string;
    expected: string;
    reversible?: boolean | null;
  };
  status: StepStatus;
  risk?: RiskLevel | null;
  verdict?: Verdict | null;
  decision_reason: string;
  result?: Record<string, unknown> | null;
  verification?: { verified: boolean; checks: { name: string; passed: boolean; detail: string }[]; skipped_reason?: string } | null;
  error?: string | null;
  duration_ms?: number | null;
  approval_id?: string | null;
}

export interface Plan {
  intent: string;
  summary: string;
  reasoning: string;
  steps: PlanStep[];
  answer: string;
  needs_research: boolean;
  memory_writes: string[];
  confidence: number;
  model_tier: "light" | "super" | "ultra";
  used_model: string;
  degraded: boolean;
}

export interface Decision {
  verdict: Verdict;
  risk: RiskLevel;
  step_id?: string | null;
  reasons: string[];
  policies: string[];
  jev_notes: string[];
  requires_approval: boolean;
  blocked_reason: string;
}

export interface ApprovalRecord {
  id: string;
  task_id: string;
  step_id: string;
  action: string;
  risk: RiskLevel;
  description: string;
  payload: Record<string, unknown>;
  preview: string[];
  status: "PENDING" | "APPROVED" | "REJECTED" | "EDITED" | "EXPIRED";
  created_at: string;
  /** Note the user attached when they decided, if any. */
  decision_note?: string;
}

/** A restricted scope the user granted: payload.grant on an approval, or a stored permission. */
export interface GrantInfo {
  policy: string;
  roots: string[];
  reason?: string;
}

/** A remembered permission ("always allow"), listed and revocable on the Security page. */
export interface PermissionGrant {
  id: string;
  policy: string;
  roots: string[];
  description: string;
  kind: "once" | "lifetime";
  created_at: string;
  expires_at: string | null;
  source: string;
}

export interface TaskStepRecord {
  id: string;
  sequence: number;
  description: string;
  status: StepStatus;
  tool: string;
  error?: string | null;
  verification?: PlanStep["verification"] | null;
}

export interface TaskRecord {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: string;
  source: string;
  created_at: string;
  updated_at?: string | null;
  progress: number;
  steps: TaskStepRecord[];
  answer: string;
  sources: { title?: string; url?: string; snippet?: string }[];
  error?: string | null;
  background: boolean;
}

export interface ActivityRecord {
  id: string;
  task_id: string;
  event_type: string;
  message: string;
  metadata: Record<string, unknown>;
  timestamp: string;
}

export interface AutomationRecord {
  id: string;
  name: string;
  schedule: string;
  prompt: string;
  status: string;
  kind: "cron" | "reminder";
  created_at: string;
  last_run_at?: string | null;
  next_run_at?: string | null;
  run_count: number;
}

export interface MemoryRecord {
  id: string;
  type: MemoryType;
  content: string;
  importance: number;
  tags: string[];
  created_at: string;
  source: string;
}

export interface MemoryHit {
  memory: MemoryRecord;
  score: number;
  components: Record<string, number>;
}

export interface SkillFinding {
  rule: string;
  severity: "critical" | "high" | "medium";
  detail: string;
  location: string;
}

export interface SkillRecord {
  name: string;
  description: string;
  trigger: string;
  required_tools: string[];
  safety: string;
  workflow: Record<string, unknown>[];
  path: string;
  /** Result of the pre-activation scan: clean | warn | blocked | allowlisted | off. */
  scan_status?: "clean" | "warn" | "blocked" | "allowlisted" | "off";
  scan_findings?: SkillFinding[];
}

export interface ToolPermission {
  tool: string;
  risk_floor: RiskLevel;
  automatic: boolean;
  can_ask: boolean;
  can_deny: boolean;
  guards: string[];
}

export interface PermissionMatrix {
  mode: ExecutionMode;
  approval_threshold: RiskLevel;
  require_write_approval: boolean;
  modes: ExecutionMode[];
  protected_paths: string[];
  skill_scan_mode: string;
  flagged_skills: string[];
  blocked_skills: string[];
  tools: ToolPermission[];
}

export interface ChatResponse {
  task_id: string;
  status: TaskStatus;
  answer: string;
  plan?: Plan | null;
  decisions: Decision[];
  sources: { title?: string; url?: string }[];
  approvals: ApprovalRecord[];
  error?: string | null;
  providers: Record<string, string>;
}

export interface DobotEvent {
  type: string;
  task_id: string;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
  sequence: number;
}

export interface DashboardSummary {
  dot: string;
  tasks: {
    total: number;
    by_status: Record<string, number>;
    in_progress: { id: string; title: string; progress: number }[];
  };
  automations: { total: number; active: number };
  approvals: { pending: number; items: ApprovalRecord[] };
  memory: { total: number; by_type: Record<string, number>; store: string; vectors: string; embedder: string };
  providers: Record<string, string>;
  skills: string[];
}

export interface ScreenContext {
  application: string;
  window_title: string;
  selected_region: Region | null;
  ocr_text: string;
  image_available: boolean;
  ocr_engine: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "dobot" | "system";
  text: string;
  taskId?: string;
  status?: TaskStatus;
  plan?: Plan | null;
  sources?: { title?: string; url?: string }[];
  approvals?: ApprovalRecord[];
  createdAt: number;
  /** Which model answered, and whether it degraded to the offline path. */
  model?: string;
  tier?: string;
  degraded?: boolean;
  /** Tokens and estimated cost for the task, when the backend reported them. */
  usage?: { total_tokens: number; cost_usd: number } | null;
  /** Files and images this message carried, so the thread can show what was sent. */
  attachments?: ChatAttachment[];
}

// ---------------------------------------------------------------------- system

/**
 * One capability, as the Doctor actually probed it. `state` is the whole point: `declined` and
 * `not_configured` are honest outcomes, not failures, and `stale` means configured-but-not-applied —
 * the state that looks identical to a working one from the outside.
 */
export interface Capability {
  id: string;
  label: string;
  state: "live" | "broken" | "declined" | "stale" | "not_configured";
  detail: string;
  fix: string;
  env: string;
  optional: boolean;
  meta: Record<string, unknown>;
}

export interface DoctorReport {
  generated_at: string;
  overall: "healthy" | "degraded";
  counts: Record<string, number>;
  capabilities: Capability[];
  fixes: { id: string; label: string; state: string; fix: string; env: string }[];
  environment: { platform: string; python: string; state_dir: string };
}

export interface UsageSummary {
  model: {
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_cost_usd: number;
    priced: boolean;
    degraded_calls: number;
    avg_latency_ms: number;
    by_model: Record<string, { calls: number; total_tokens?: number; cost_usd: number }>;
    by_tier: Record<string, number>;
    by_purpose: Record<string, { calls: number; total_tokens: number }>;
  };
  recent_calls: {
    model: string;
    tier: string;
    purpose: string;
    total_tokens: number;
    latency_ms: number;
    cost_usd: number;
    degraded: boolean;
    at: string;
  }[];
  tokenjuice: {
    enabled: boolean;
    calls: number;
    compressed_calls: number;
    original_chars: number;
    sent_chars: number;
    saved_chars: number;
    saved_tokens: number;
    saved_pct: number;
    by_label: Record<string, { calls: number; saved_chars: number }>;
  };
  pricing_configured: boolean;
  recent_compressions: { label: string; strategy: string; saved_pct: number }[];
}

export interface IdentitySummary {
  directory: string;
  documents: Record<string, { chars: number; owned_by: "user" | "system" }>;
  axioms: string[];
  telos_fields: string[];
  revision: string;
  rendered: string;
  documents_available: string[];
}

export interface InterceptorRule {
  id: string;
  action: string;
  reason: string;
  source: string;
  tools: string[];
  conditions: Record<string, unknown>;
  priority: number;
}

export interface InterceptorReport {
  enabled: boolean;
  count: number;
  by_source: Record<string, number>;
  errors: string[];
  rules: InterceptorRule[];
}

export interface CanonicalFact {
  id: string;
  key: string;
  content: string;
  confidence: number;
  reinforcements: number;
  pinned: boolean;
  status: string;
  source: string;
  history: { text: string; at: string; reason: string }[];
  candidates: { text: string; count: number }[];
  updated_at: string;
}

export interface VoiceSummary {
  enabled: boolean;
  engine: string;
  available: boolean;
  detail: string;
  spoken: number;
  failures: number;
  probe: { engine: string; detail: string; fix: string; available: boolean; platform: string };
}

export type ThemeId =
  | "midnight"
  | "graphite"
  | "aurora"
  | "ember"
  | "daylight"
  | "rosewood";
