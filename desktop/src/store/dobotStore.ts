// The single store the whole UI reads from. Events from the WebSocket drive dot state, message
// updates, approvals and notifications, so the UI is a projection of what the agent actually did.

import { create } from "zustand";
import { ApiError, api } from "../services/api";
import { native } from "../services/native";
import { socket } from "../services/websocket";
import { useProgress } from "../services/gamification";
import { captureSelection, type Selection } from "../services/screen";
import type {
  ApprovalRecord,
  ChatAttachment,
  ChatMessage,
  Conversation,
  DashboardSummary,
  DobotEvent,
  DotStatus,
  ExecutionMode,
  Plan,
  SessionMessage,
  TaskRecord,
} from "../types";

const MAX_EVENTS = 250;
const MODE_KEY = "dobot.mode";
const SPEAK_KEY = "dobot.speakAnswers";
const THREAD_PREFIX = "dobot.thread.";
/** The conversation that was open last time, so reopening the app lands you where you were. */
const ACTIVE_CONV_KEY = "dobot.activeConversation";
/** Enough history to feel continuous; small enough to stay inside the localStorage quota. */
const THREAD_LIMIT = 60;

/** The local cache slot for the conversation currently open. */
let activeThreadKey: string | null = null;

function newConversationId(): string {
  return `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function threadKeyFor(conversationId: string | null): string {
  // `local` is the pre-transcript slot every older install used; it is still readable so nothing
  // already saved disappears.
  return `${THREAD_PREFIX}${conversationId || "local"}`;
}

/**
 * The id this turn will be filed under, minted on first use.
 *
 * Created lazily rather than when the app opens: a conversation the user never said anything in
 * should not exist on the server.
 */
function ensureConversation(): string {
  const current = useDobot.getState().activeConversationId;
  if (current) return current;
  const id = newConversationId();
  activeThreadKey = threadKeyFor(id);
  localStorage.setItem(ACTIVE_CONV_KEY, id);
  useDobot.setState({ activeConversationId: id });
  return id;
}

/** A stored turn becomes a thread message again. */
function fromSessionMessage(message: SessionMessage): ChatMessage {
  const status = (message.status || "") as ChatMessage["status"];
  return {
    id: message.id,
    role: message.role === "dobot" ? "dobot" : "user",
    text: message.content,
    createdAt: message.created_at ? Date.parse(message.created_at) || Date.now() : Date.now(),
    taskId: message.task_id || undefined,
    // Only a status the backend actually recorded is shown; an unknown one renders no chip rather
    // than pretending a turn succeeded.
    status: status || undefined,
  };
}

/**
 * Anything still marked in-flight is a turn from a run that ended when the app closed.
 * Showing it as an eternal spinner would be a lie; the backend task is gone.
 */
function markInterrupted(messages: ChatMessage[]): ChatMessage[] {
  const interrupted = new Set(["PENDING", "PLANNING", "IN_PROGRESS", "WAITING_APPROVAL", "WAITING_USER"]);
  return messages.map((message) =>
    message.status && interrupted.has(message.status)
      ? {
          ...message,
          status: "CANCELLED" as const,
          text: message.text || "Stopped — Dobot was closed while this was running.",
          plan: message.plan
            ? {
                ...message.plan,
                steps: message.plan.steps.map((step) =>
                  step.status === "PENDING" ? { ...step, status: "SKIPPED" as const } : step,
                ),
              }
            : message.plan,
        }
      : message,
  );
}

/** Persist the thread, minus anything too heavy for localStorage (base64 attachments). */
function persistThread(messages: ChatMessage[]): void {
  if (!activeThreadKey) return;
  try {
    const slim = messages
      .slice(-THREAD_LIMIT)
      // Attachments carry base64 payloads that would blow the quota; names stay if present
      // through the message text itself.
      .map(({ attachments: _dropped, ...rest }) => rest);
    localStorage.setItem(activeThreadKey, JSON.stringify(slim));
  } catch {
    // Quota exceeded or private mode: the chat simply isn't saved this time — the live
    // thread is unaffected, which is the honest trade.
  }
}

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
  // Only a task that actually finished is worth a notification. A task paused at an approval
  // gate (or still running) announces itself through its own event, and "Dobot finished" for it
  // would be a lie — finaliseTask calls this for those states too.
  if (task.status !== "COMPLETED" && task.status !== "FAILED" && task.status !== "CANCELLED") return;
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
  /** Every chat the backend can reopen, newest first — this, not the message log, is the sidebar. */
  conversations: Conversation[];
  /** The chat currently open. Every outgoing turn is filed under this id. */
  activeConversationId: string | null;
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
  /** Web research outside the chat loop: Tavily search + synthesis, answered with sources. */
  research: (text: string) => Promise<void>;
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
  decide: (
    id: string,
    decision: "approve" | "reject" | "edit",
    note?: string,
    edits?: Record<string, unknown>,
    scope?: "once" | "lifetime",
  ) => Promise<void>;
  kill: (taskId?: string) => Promise<void>;
  setShadowMode: (on: boolean) => Promise<void>;
  setMode: (mode: ExecutionMode) => void;
  setDotEnabled: (enabled: boolean) => Promise<void>;
  refreshDotEnabled: () => Promise<void>;
  setSpeakAnswers: (enabled: boolean) => Promise<void>;
  refreshVoice: () => Promise<void>;
  /** Start a fresh conversation. Pending approvals survive — they belong to running tasks. */
  newConversation: () => void;
  /** Refresh the sidebar from the backend. */
  loadConversations: () => Promise<void>;
  /** Reopen a past chat: replay its transcript and keep sending under the same id. */
  openConversation: (id: string) => Promise<boolean>;
  /** Remove a chat and its turns from the database. */
  deleteConversation: (id: string) => Promise<void>;
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
  conversations: [],
  activeConversationId: null,
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
          void get().refreshVoice();
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
    void get().refreshVoice();
  },

  disconnect: () => socket.close(),

  handleEvent: (event) => {
    set((state) => {
      // Every reconnect replays the server's history, which is largely already in this buffer —
      // appending it twice duplicated rows in the live timeline and made React warn about the
      // duplicate keys. Only the display buffer is deduplicated: the switch below still runs, so
      // approvals, task finalisation and the dot status are never skipped. MAX_SAFE_INTEGER marks
      // frames synthesised in this client, which are deliberately repeatable.
      const replayed =
        event.sequence !== Number.MAX_SAFE_INTEGER &&
        state.events.some((known) => known.sequence === event.sequence);
      return {
        events: replayed ? state.events : [...state.events.slice(-MAX_EVENTS + 1), event],
      };
    });

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
        // A finished task is already announced by finaliseTask → notifyIfAway, which carries the
        // answer and also covers failures; the backend still emits this event so the in-app toast
        // can show it. Notifying here as well popped twice for every completed response — only
        // non-task notifications (e.g. "Permission remembered") go straight to the system here.
        if (!("background" in event.data)) {
          void native.notify(
            String(event.data.title ?? "Dobot"),
            String(event.data.body ?? event.message),
          );
        }
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
        conversation_id: ensureConversation(),
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

  research: async (text) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const userMessage: ChatMessage = {
      id: makeId(),
      role: "user",
      text: trimmed,
      createdAt: Date.now(),
    };
    const placeholder: ChatMessage = {
      id: makeId(),
      role: "dobot",
      text: "Researching the web…",
      status: "PLANNING",
      createdAt: Date.now(),
    };
    set((state) => ({ messages: [...state.messages, userMessage, placeholder], busy: true, lastError: null }));

    try {
      const result = await api.research(trimmed, "quick");
      useProgress.getState().award("researchRun");
      set((state) => ({
        busy: false,
        messages: state.messages.map((item) =>
          item.id === placeholder.id
            ? {
                ...item,
                text: result.summary || "Research finished — no summary came back.",
                status: "COMPLETED" as const,
                sources: result.sources,
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

  decide: async (id, decision, note = "", edits, scope = "once") => {
    try {
      const response = await api.decide(id, decision, note, edits, scope);
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

  newConversation: () => {
    const id = newConversationId();
    activeThreadKey = threadKeyFor(id);
    localStorage.setItem(ACTIVE_CONV_KEY, id);
    set({ messages: [], lastError: null, activeTaskId: null, activeConversationId: id });
  },

  loadConversations: async () => {
    try {
      // 200 is the backend's cap: the sidebar promises every previous chat, like ChatGPT.
      set({ conversations: await api.conversations(200) });
    } catch {
      /* backend may be starting; the sidebar keeps whatever it already had */
    }
  },

  openConversation: async (id) => {
    if (!id) return false;
    try {
      const payload = await api.conversation(id, 500);
      const restored = (payload.messages ?? []).map(fromSessionMessage);
      activeThreadKey = threadKeyFor(id);
      localStorage.setItem(ACTIVE_CONV_KEY, id);
      set({
        activeConversationId: id,
        messages: restored,
        lastError: null,
        activeTaskId: null,
        busy: false,
      });
      persistThread(restored);
      return true;
    } catch {
      // Still starting, or deleted elsewhere: leave what is on screen alone rather than blank it.
      return false;
    }
  },

  deleteConversation: async (id) => {
    try {
      await api.deleteConversation(id);
      set((state) => ({ conversations: state.conversations.filter((item) => item.id !== id) }));
      if (get().activeConversationId === id) get().newConversation();
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  reset: () => set({ messages: [], events: [], approvals: [], lastError: null }),
}));

/**
 * Restore the conversation that was open last time, then reconcile it with the backend.
 *
 * The cached copy is shown first so reopening is instant, and so a chat still renders when the
 * backend is not up yet. The database is the source of truth: whatever it has replaces the cache.
 * Called by the auth gate when a session exists (sign-in or app start).
 */
export function loadThread(): Promise<void> {
  // App mount and the auth gate both ask for this. Run one restore at a time so two of them cannot
  // interleave and leave the sidebar showing whatever the slower one happened to fetch.
  if (restoreInFlight) return restoreInFlight;
  restoreInFlight = restoreThread().finally(() => {
    restoreInFlight = null;
  });
  return restoreInFlight;
}

let restoreInFlight: Promise<void> | null = null;

async function restoreThread(): Promise<void> {
  const lastId = localStorage.getItem(ACTIVE_CONV_KEY) || "";
  const cacheKey = threadKeyFor(lastId || null);
  activeThreadKey = cacheKey;

  let cached: ChatMessage[] = [];
  try {
    const raw = lastId ? localStorage.getItem(cacheKey) : null;
    if (raw) cached = JSON.parse(raw) as ChatMessage[];
  } catch {
    cached = [];
  }
  useDobot.setState({
    activeConversationId: lastId || null,
    messages: markInterrupted(cached),
    busy: false,
    activeTaskId: null,
    lastError: null,
  });

  const store = useDobot.getState();
  await store.loadConversations();
  const conversations = useDobot.getState().conversations;
  const known = Boolean(lastId && conversations.some((item) => item.id === lastId));

  if (known) {
    await useDobot.getState().openConversation(lastId);
    return;
  }
  if (conversations[0]) {
    // Nothing remembered, but there is history: land on the most recent chat, like any chat app.
    await useDobot.getState().openConversation(conversations[0].id);
    return;
  }
  if (cached.length && lastId) {
    // The backend has never seen this thread. Push it up so the transcript lives in the database
    // from now on instead of only in this browser's storage.
    try {
      await api.seedConversation(
        lastId,
        cached.map((message) => ({
          role: message.role === "dobot" ? "dobot" : "user",
          content: message.text,
          status: message.status ?? "",
          created_at: new Date(message.createdAt).toISOString(),
        })),
      );
      await useDobot.getState().openConversation(lastId);
    } catch {
      useDobot.setState({ activeConversationId: lastId });
    }
    return;
  }
  if (!useDobot.getState().messages.length) useDobot.getState().newConversation();
}

/**
 * Detach persistence, then blank the thread — in that order. Signing out hides the
 * conversation behind the login card without erasing the saved copy on the server, so the next
 * sign-in (by anyone) starts clean and the owner finds theirs intact.
 */
export function clearThread(): void {
  activeThreadKey = null;
  localStorage.removeItem(ACTIVE_CONV_KEY);
  useDobot.setState({
    messages: [],
    busy: false,
    activeTaskId: null,
    activeConversationId: null,
    lastError: null,
  });
}

// The thread saves itself as it grows. Only the chat window writes: every Tauri window has
// its own store instance, and the dashboard/dot windows hold an empty `messages` array that
// would silently overwrite the real conversation if they persisted too.
if (!native.isNative || native.label() === "chat") {
  useDobot.subscribe((state, previous) => {
    if (state.messages !== previous.messages) persistThread(state.messages);
  });
}

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
    // The turn count and title just changed server-side; keep the sidebar honest.
    void get().loadConversations();
    // After the state is written: whoever comes back from the notification finds the answer there.
    await notifyIfAway(task);
  } catch {
    set({ busy: false });
  }
}
