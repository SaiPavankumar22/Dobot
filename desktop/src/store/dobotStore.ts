// The single store the whole UI reads from. Events from the WebSocket drive dot state, message
// updates, approvals and notifications, so the UI is a projection of what the agent actually did.

import { create } from "zustand";
import { ApiError, api } from "../services/api";
import { native } from "../services/native";
import { socket } from "../services/websocket";
import { captureSelection, type Selection } from "../services/screen";
import type {
  ApprovalRecord,
  ChatAttachment,
  ChatMessage,
  DashboardSummary,
  DobotEvent,
  DotStatus,
  ExecutionMode,
  Plan,
  TaskRecord,
} from "../types";

const MAX_EVENTS = 250;
const MODE_KEY = "dobot.mode";
const SPEAK_KEY = "dobot.speakAnswers";

/**
 * Read a finished answer aloud, if the user asked for that.
 *
 * Speech is a side effect, never a dependency: a machine with no engine, or a backend with voice off,
 * must not turn a successful task into a visible error. The failure is swallowed and the answer stands.
 */
async function maybeSpeak(answer: string, get: () => DobotStore): Promise<void> {
  const text = (answer || "").trim();
  if (!get().speakAnswers || !text) return;
  try {
    await api.speak(text);
  } catch {
    /* no engine, or voice disabled on the backend: the answer still arrived */
  }
}

/**
 * A reply that landed while the user was in another application is held up as a system
 * notification: an always-on assistant should speak up when it is done, not wait to be looked at.
 * The chat window speaks for every window (they all see the same event), and only when no Dobot
 * window has the user's attention — if they are already reading us, the thread is the feedback.
 */
async function notifyIfAway(task: TaskRecord): Promise<void> {
  if (native.label() !== "chat") return;
  if (await native.dobotFocused()) return;
  const answer = (task.answer || task.error || "")
    .replace(/[*_`#>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  const body = answer
    ? answer.length > 180
      ? `${answer.slice(0, 180)}…`
      : answer
    : "Open Dobot to see what it did.";
  await native.notify(task.status === "FAILED" ? "Dobot hit a snag" : "Dobot finished", body);
}

/** The mode chosen last time, defaulting to the specification's `agent`. */
function storedMode(): ExecutionMode {
  const raw = localStorage.getItem(MODE_KEY);
  return raw === "ask" || raw === "assist" || raw === "agent" ? raw : "agent";
}

/** Whether this module has already attached its socket listeners. */
let subscribed = false;

function makeId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

interface DobotStore {
  dotStatus: DotStatus;
  /** What the backend is actually doing, in its own words — the dot's tooltip and the panel's chip. */
  dotDetail: string;
  connected: boolean;
  connectionDetail: string;
  providers: Record<string, string>;
  messages: ChatMessage[];
  events: DobotEvent[];
  approvals: ApprovalRecord[];
  dashboard: DashboardSummary | null;
  security: Record<string, unknown> | null;
  shadowMode: boolean;
  /** Per-message execution mode: how much Dobot may do without asking. */
  mode: ExecutionMode;
  /** Always-on floating dot. Off means Dobot is a plain chat window. */
  dotEnabled: boolean;
  /** Read finished answers aloud using the machine's own speech engine. Off by default. */
  speakAnswers: boolean;
  selection: Selection | null;
  capturing: boolean;
  busy: boolean;
  lastError: string | null;
  activeTaskId: string | null;

  connect: () => void;
  disconnect: () => void;
  handleEvent: (event: DobotEvent) => void;
  send: (
    text: string,
    options?: { shadow?: boolean; useSelection?: boolean; attachments?: ChatAttachment[] },
  ) => Promise<void>;
  captureScreen: () => Promise<void>;
  /** One chord, one picture: the window the user is in, as ready-to-send selection. */
  captureActiveWindow: () => Promise<void>;
  setCapturing: (capturing: boolean) => void;
  finishBrowserSelection: (selection: Selection) => void;
  clearSelection: () => void;
  refreshApprovals: () => Promise<void>;
  refreshDashboard: () => Promise<void>;
  refreshProviders: () => Promise<void>;
  refreshSecurity: () => Promise<void>;
  decide: (id: string, decision: "approve" | "reject", note?: string) => Promise<void>;
  kill: (taskId?: string) => Promise<void>;
  setShadowMode: (on: boolean) => Promise<void>;
  setMode: (mode: ExecutionMode) => void;
  setDotEnabled: (enabled: boolean) => Promise<void>;
  refreshDotEnabled: () => Promise<void>;
  setSpeakAnswers: (enabled: boolean) => Promise<void>;
  refreshVoice: () => Promise<void>;
  /** Start a fresh conversation. Pending approvals survive — they belong to running tasks. */
  newConversation: () => void;
  reset: () => void;
}

function patchMessage(
  messages: ChatMessage[],
  taskId: string,
  patch: Partial<ChatMessage>,
): ChatMessage[] {
  return messages.map((message) => (message.taskId === taskId ? { ...message, ...patch } : message));
}

/**
 * Fold the task's persisted step statuses back into the plan we first showed.
 *
 * The `plan_created` event carries every step as PENDING; only the task record knows how each step
 * actually ended, so without this the thread would show a completed answer above an all-pending plan.
 */
function mergeStepStatuses(plan: Plan | undefined, task: TaskRecord): Plan | undefined {
  if (!plan || task.steps.length === 0) return plan;
  const bySequence = new Map(task.steps.map((step) => [step.sequence, step]));
  return {
    ...plan,
    steps: plan.steps.map((step) => {
      const record = bySequence.get(step.index);
      if (!record) return step;
      return {
        ...step,
        status: record.status,
        error: record.error ?? step.error,
        verification: record.verification ?? step.verification,
      };
    }),
  };
}

export const useDobot = create<DobotStore>((set, get) => ({
  dotStatus: "IDLE",
  dotDetail: "",
  connected: false,
  connectionDetail: "not connected",
  providers: {},
  messages: [],
  events: [],
  approvals: [],
  dashboard: null,
  security: null,
  shadowMode: false,
  mode: storedMode(),
  dotEnabled: false,
  speakAnswers: localStorage.getItem(SPEAK_KEY) === "true",
  selection: null,
  capturing: false,
  busy: false,
  lastError: null,
  activeTaskId: null,

  connect: () => {
    // Subscribing twice would deliver every event twice (two notifications, two answer updates), so
    // subscription is idempotent. React StrictMode mounts effects twice in development.
    if (!subscribed) {
      subscribed = true;
      socket.onStatus((connected, detail) => {
        set({ connected, connectionDetail: detail ?? (connected ? "connected" : "disconnected") });
        if (connected) {
          void get().refreshApprovals();
          void get().refreshProviders();
        }
      });
      socket.onEvent((event) => get().handleEvent(event));
      // The tray menu can flip the dot too; keep every window's toggle honest.
      void native.onDotEnabled((enabled) => set({ dotEnabled: enabled }));
    }
    socket.connect();
    void get().refreshProviders();
    void get().refreshApprovals();
    void get().refreshDotEnabled();
  },

  disconnect: () => socket.close(),

  handleEvent: (event) => {
    set((state) => ({ events: [...state.events.slice(-MAX_EVENTS + 1), event] }));

    switch (event.type) {
      case "dot_status": {
        const status = (event.data.status as DotStatus) ?? "IDLE";
        // The detail is what makes the dot readable at a glance: "understanding your request",
        // "read file …", not just a spinning glyph.
        set({ dotStatus: status, dotDetail: event.message || "" });
        break;
      }
      case "approval_required": {
        void get().refreshApprovals();
        const description = (event.data.approval as ApprovalRecord | undefined)?.description ?? event.message;
        void native.notify("Dobot needs your approval", description.slice(0, 160));
        // A task paused at a gate needs its bubble refreshed: the background response only reported
        // "in progress", so the gate's explanation lives in the task record.
        if (event.task_id) void finaliseTask(event.task_id, set, get);
        break;
      }
      case "shadow_plan": {
        // Shadow mode ends without a `completed` event, so refresh here too.
        if (event.task_id) void finaliseTask(event.task_id, set, get);
        break;
      }
      case "notification": {
        void native.notify(
          String(event.data.title ?? "Dobot"),
          String(event.data.body ?? event.message),
        );
        break;
      }
      case "plan_created": {
        const plan = event.data.plan as Plan | undefined;
        if (plan && event.task_id) {
          set((state) => ({ messages: patchMessage(state.messages, event.task_id, { plan }) }));
        }
        break;
      }
      case "completed":
      case "failed":
      case "killed": {
        if (event.task_id) void finaliseTask(event.task_id, set, get);
        break;
      }
      default:
        break;
    }
  },

  send: async (text, options) => {
    const trimmed = text.trim();
    const attachments = options?.attachments ?? [];
    // An attachment on its own is a legitimate request ("what does this say?"); the backend fills in
    // the implied words. Nothing at all is not.
    if (!trimmed && attachments.length === 0) return;
    const selection = options?.useSelection ? get().selection : null;
    const userMessage: ChatMessage = {
      id: makeId(),
      role: "user",
      text: selection
        ? `${trimmed}\n\n[selected ${selection.region?.width}×${selection.region?.height} region from ${selection.windowTitle || selection.application || "screen"}]`
        : trimmed,
      createdAt: Date.now(),
      attachments: attachments.length ? attachments : undefined,
    };
    const placeholder: ChatMessage = {
      id: makeId(),
      role: "dobot",
      text: "Working on it…",
      status: "PLANNING",
      createdAt: Date.now(),
    };
    set((state) => ({ messages: [...state.messages, userMessage, placeholder], busy: true, lastError: null }));

    try {
      const response = await api.chat({
        message: trimmed,
        background: true,
        shadow: options?.shadow ?? get().shadowMode,
        mode: get().mode,
        region: selection?.region ?? null,
        image: selection?.image ?? null,
        attachments: attachments.map(({ name, mime, data }) => ({ name, mime, data })),
      });
      // The immediate response carries the answer for anything that finished synchronously (a direct
      // answer, a shadow plan, an approval gate). Background completions arrive later as events.
      set((state) => ({
        activeTaskId: response.task_id,
        selection: null,
        messages: state.messages.map((item) =>
          item.id === placeholder.id
            ? {
                ...item,
                taskId: response.task_id,
                text: response.answer || "Started in the background — I'll notify you when it's done.",
                status: response.status,
                plan: response.plan ?? item.plan,
                sources: response.sources,
                approvals: response.approvals,
              }
            : item,
        ),
      }));
    } catch (error) {
      const message = error instanceof ApiError ? error.message : String(error);
      set((state) => ({
        busy: false,
        lastError: message,
        messages: state.messages.map((item) =>
          item.id === placeholder.id ? { ...item, text: message, status: "FAILED" as const } : item,
        ),
      }));
    }
  },

  captureScreen: async () => {
    set({ lastError: null });
    const selection = await captureSelection();
    if (selection.source === "none") {
      set({ lastError: selection.note ?? "selection cancelled" });
      return;
    }
    set({ selection });
  },

  setCapturing: (capturing) => set({ capturing, lastError: capturing ? null : get().lastError }),

  captureActiveWindow: async () => {
    if (!native.isNative) return;
    set({ lastError: null });
    const payload = await native.captureActiveWindow();
    if (!payload || payload.error || !payload.image) {
      set({ lastError: payload?.error ?? "Dobot couldn't take a picture of that window." });
      return;
    }
    set({
      selection: {
        image: payload.image,
        region: payload.region,
        application: payload.application,
        windowTitle: payload.window_title,
        source: "native",
      },
    });
  },

  finishBrowserSelection: (selection) => set({ selection, capturing: false }),

  clearSelection: () => set({ selection: null }),

  refreshApprovals: async () => {
    try {
      set({ approvals: await api.approvals() });
    } catch {
      /* backend may be starting */
    }
  },

  refreshDashboard: async () => {
    try {
      const dashboard = await api.dashboard();
      set({ dashboard, providers: dashboard.providers });
    } catch {
      /* backend may be starting */
    }
  },

  refreshProviders: async () => {
    try {
      set({ providers: await api.providers() });
    } catch {
      /* backend may be starting */
    }
  },

  refreshSecurity: async () => {
    try {
      set({ security: await api.security() });
    } catch {
      /* backend may be starting */
    }
  },

  decide: async (id, decision, note = "") => {
    try {
      const response = await api.decide(id, decision, note);
      set((state) => ({
        messages: patchMessage(state.messages, response.task_id, {
          text: response.answer,
          status: response.status,
          plan: response.plan ?? undefined,
          approvals: response.approvals,
        }),
        approvals: state.approvals.filter((approval) => approval.id !== id),
      }));
      void get().refreshDashboard();
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  kill: async (taskId) => {
    try {
      await api.kill(taskId ?? get().activeTaskId ?? undefined);
      set({ busy: false, dotStatus: "KILLED" });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  setShadowMode: async (on) => {
    set({ shadowMode: on });
    try {
      await api.updateSettings({ shadow_mode: on });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  setMode: (mode) => {
    localStorage.setItem(MODE_KEY, mode);
    set({ mode });
  },

  setDotEnabled: async (enabled) => {
    // Optimistic: the switch should feel instant; the shell echoes back the real state.
    set({ dotEnabled: enabled });
    const result = await native.setDotEnabled(enabled);
    set({ dotEnabled: result });
  },

  refreshDotEnabled: async () => {
    set({ dotEnabled: await native.dotEnabled() });
  },

  setSpeakAnswers: async (enabled) => {
    // Two switches must never disagree: the local one decides whether the UI asks, the backend one
    // decides whether speech is allowed at all. Setting both keeps the answer unambiguous.
    localStorage.setItem(SPEAK_KEY, String(enabled));
    set({ speakAnswers: enabled });
    try {
      await api.updateSettings({ voice_enabled: enabled });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  refreshVoice: async () => {
    try {
      const voice = await api.voice();
      set({ speakAnswers: voice.enabled });
    } catch {
      /* the toggle falls back to its stored value */
    }
  },

  newConversation: () => set({ messages: [], lastError: null, activeTaskId: null }),

  reset: () => set({ messages: [], events: [], approvals: [], lastError: null }),
}));

async function finaliseTask(
  taskId: string,
  set: (partial: Partial<DobotStore> | ((state: DobotStore) => Partial<DobotStore>)) => void,
  get: () => DobotStore,
): Promise<void> {
  try {
    const task: TaskRecord = await api.task(taskId);
    set((state) => ({
      messages: patchMessage(state.messages, taskId, {
        text: task.answer || task.error || "Finished.",
        status: task.status,
        plan: mergeStepStatuses(
          state.messages.find((message) => message.taskId === taskId)?.plan ?? undefined,
          task,
        ),
        sources: task.sources,
      }),
      busy: state.activeTaskId === taskId ? false : state.busy,
    }));
    await maybeSpeak(task.answer, get);
    void get().refreshApprovals();
    void get().refreshDashboard();
    // After the state is written: whoever comes back from the notification finds the answer there.
    await notifyIfAway(task);
  } catch {
    set({ busy: false });
  }
}
