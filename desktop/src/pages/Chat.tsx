// The chat window — the default face of Dobot.
//
// It is deliberately an ordinary chatbot: a sidebar of conversations, a message thread, a composer.
// The ambient part of Dobot (the always-on floating dot) is opt-in and lives behind one switch in the
// header. With the switch off, nothing of Dobot is on screen but this window.

import { useEffect, useMemo, useRef, useState } from "react";
import { Activity } from "../pages/Activity";
import { Approvals } from "../pages/Approvals";
import { Automations } from "../pages/Automations";
import { ApprovalCard } from "../components/ApprovalCard";
import {
  IconClose,
  IconEye,
  IconFile,
  IconImage,
  IconMic,
  IconMoon,
  IconPaperclip,
  IconScreen,
  IconSend,
  IconStopCircle,
} from "../components/Icons";
import { Guide } from "../pages/Guide";
import { Memory } from "../pages/Memory";
import { Overview } from "../pages/Overview";
import { PlanView } from "../components/PlanView";
import { Security } from "../pages/Security";
import { Settings } from "../pages/Settings";
import { Skills } from "../pages/Skills";
import { Tasks } from "../pages/Tasks";
import { Switch } from "../components/Switch";
import { statusLabel } from "../components/DobotDot";
import { api, getBaseUrl } from "../services/api";
import {
  DEFAULT_LIMITS,
  acceptAttribute,
  formatBytes,
  readAttachments,
  type Rejection,
} from "../services/attachments";
import { native } from "../services/native";
import { previewDataUrl } from "../services/screen";
import { useVoiceInput } from "../services/voice";
import { useDobot } from "../store/dobotStore";
import type { AttachmentLimits, ChatAttachment, ChatMessage, ExecutionMode } from "../types";

/** The execution ladder. Each mode is a real approval threshold, not a label — see MODES below. */
const MODES: { id: ExecutionMode; label: string; hint: string }[] = [
  { id: "ask", label: "Ask", hint: "Answer and show the plan. Nothing is executed, ever." },
  { id: "assist", label: "Assist", hint: "Confirm anything beyond a read before doing it." },
  {
    id: "agent",
    label: "Agent",
    hint: "Safe, reversible work proceeds; anything risky stops for your approval.",
  },
];

const NAV = [
  { id: "guide", label: "Guide", hint: "What every feature does, and where it lives" },
  { id: "overview", label: "Overview", hint: "Tasks, approvals and providers at a glance" },
  { id: "tasks", label: "Tasks", hint: "Everything Dobot has been asked to do" },
  { id: "automations", label: "Automations", hint: "Saved tasks on a schedule" },
  { id: "approvals", label: "Approvals", hint: "Risky steps waiting for your decision" },
  { id: "memory", label: "Memory", hint: "What Dobot remembers about you and your work" },
  { id: "skills", label: "Skills", hint: "Reusable workflows it can run again" },
  { id: "activity", label: "Activity", hint: "The honest log of every step" },
  { id: "security", label: "Security", hint: "Permissions, guards and the kill switch" },
  { id: "settings", label: "Settings", hint: "Keys, connection, appearance" },
] as const;

/** Workspace sections render inside the chat window — the sidebar must not spawn a second window. */
function Section({ id, onBack }: { id: string; onBack: () => void }) {
  const body =
    id === "guide" ? <Guide /> :
    id === "overview" ? <Overview /> :
    id === "tasks" ? <Tasks /> :
    id === "automations" ? <Automations /> :
    id === "approvals" ? <Approvals /> :
    id === "memory" ? <Memory /> :
    id === "skills" ? <Skills /> :
    id === "activity" ? <Activity /> :
    id === "security" ? <Security /> :
    id === "settings" ? <Settings /> :
    <div className="notice">Unknown section.</div>;
  const label = NAV.find((item) => item.id === id)?.label ?? id;
  return (
    <div className="chat__section-view">
      <div className="chat__section-bar">
        <button className="ghost" onClick={onBack} title="Back to the conversation">← Chat</button>
        <strong>{label}</strong>
        <span className="chat__composer-spacer" />
        {native.isNative && (
          <button className="ghost" onClick={() => void native.openDashboard(id)} title="Open this section in the full dashboard window">Open in dashboard ⧉</button>
        )}
      </div>
      <div className="chat__section-body">{body}</div>
    </div>
  );
}

const SUGGESTIONS: { title: string; detail: string; prompt: string; screen?: boolean }[] = [
  {
    title: "Tidy my Downloads folder",
    detail: "See the action firewall preview every file before anything moves",
    prompt: "Clean up my Downloads folder",
  },
  {
    title: "Explain what's on my screen",
    detail: "Select a region and Dobot reads it",
    prompt: "Explain what is on my screen",
    screen: true,
  },
  {
    title: "Research something properly",
    detail: "Source-aware web research with citations",
    prompt: "Research the best local AI models I can run on a laptop GPU, and cite your sources",
  },
  {
    title: "Plan my day",
    detail: "Turn a messy list into an ordered plan",
    prompt: "Help me plan my day and tell me the three things I should do first",
  },
];

interface Thread {
  id: string;
  title: string;
  at: number;
}

/** The sidebar list is derived from the message log: every user turn starts a thread. */
function deriveThreads(messages: ChatMessage[]): Thread[] {
  return messages
    .filter((message) => message.role === "user")
    .map((message) => ({
      id: message.id,
      title: message.text.split("\n")[0]?.slice(0, 52) || "New conversation",
      at: message.createdAt,
    }))
    .reverse();
}

export function Chat() {
  const messages = useDobot((state) => state.messages);
  const approvals = useDobot((state) => state.approvals);
  const dotStatus = useDobot((state) => state.dotStatus);
  const connected = useDobot((state) => state.connected);
  const connectionDetail = useDobot((state) => state.connectionDetail);
  const shadowMode = useDobot((state) => state.shadowMode);
  const mode = useDobot((state) => state.mode);
  const setMode = useDobot((state) => state.setMode);
  const dotEnabled = useDobot((state) => state.dotEnabled);
  const selection = useDobot((state) => state.selection);
  const lastError = useDobot((state) => state.lastError);
  const capturing = useDobot((state) => state.capturing);
  const send = useDobot((state) => state.send);
  const captureScreen = useDobot((state) => state.captureScreen);
  const captureActiveWindow = useDobot((state) => state.captureActiveWindow);
  const clearSelection = useDobot((state) => state.clearSelection);
  const setShadowMode = useDobot((state) => state.setShadowMode);
  const setDotEnabled = useDobot((state) => state.setDotEnabled);
  const newConversation = useDobot((state) => state.newConversation);
  const setCapturing = useDobot((state) => state.setCapturing);
  const kill = useDobot((state) => state.kill);

  const [draft, setDraft] = useState("");
  const [includeSelection, setIncludeSelection] = useState(true);
  const [section, setSection] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [rejections, setRejections] = useState<Rejection[]>([]);
  const [limits, setLimits] = useState<AttachmentLimits>(DEFAULT_LIMITS);
  const [dragging, setDragging] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const voice = useVoiceInput((text) => {
    setDraft((current) => (current ? `${current} ${text}` : text));
    composerRef.current?.focus();
  });

  // Esc lets go of the microphone, the way any recorder does.
  useEffect(() => {
    if (voice.phase !== "recording") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        voice.cancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [voice.phase, voice.cancel]);

  // The attachment limits come from the backend, so the two can never drift apart. A backend that
  // predates the endpoint simply leaves the defaults in place.
  useEffect(() => {
    let cancelled = false;
    api
      .attachmentLimits()
      .then((published) => {
        if (!cancelled && published?.images && published?.files) setLimits(published);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  async function addFiles(files: File[]): Promise<void> {
    if (!files.length) return;
    const { attachments: accepted, rejected } = await readAttachments(files, limits, attachments);
    setRejections(rejected);
    if (accepted.length) setAttachments((current) => [...current, ...accepted]);
  }

  const threads = useMemo(() => deriveThreads(messages), [messages]);
  const pending = approvals.length > 0;

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, dotStatus, lastError]);

  async function submit() {
    const text = draft.trim();
    if (!text && attachments.length === 0) return;
    const sending = attachments;
    setDraft("");
    setAttachments([]);
    setRejections([]);
    await send(text, {
      shadow: shadowMode,
      useSelection: includeSelection && Boolean(selection),
      attachments: sending,
    });
  }

  async function startScreenCapture() {
    if (native.isNative) {
      await captureScreen();
    } else {
      setCapturing(true);
    }
  }

  function openSection(id: string) {
    // Sections open inside this window. The dashboard window stays reachable via the header button.
    setSection(id);
  }

  return (
    <div className="chat">
      <aside className="chat__sidebar">
        <div className="chat__brand">
          <span className="chat__brand-mark">●</span>
          <span className="chat__brand-name">Dobot</span>
        </div>

        <button className="chat__new" onClick={newConversation} title="Start a fresh conversation">
          ＋ New chat
        </button>

        <div className="chat__section-label">Recent</div>
        <div className="chat__threads">
          {threads.length === 0 && <div className="chat__empty-hint">No conversations yet</div>}
          {threads.map((thread) => (
            <button
              key={thread.id}
              className="chat__thread"
              onClick={() => {
                document
                  .querySelector(`[data-message-id="${thread.id}"]`)
                  ?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
              title={thread.title}
            >
              <span className="chat__thread-title">{thread.title}</span>
              <span className="chat__thread-time">
                {new Date(thread.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </span>
            </button>
          ))}
        </div>

        <div className="chat__section-label">Workspace</div>
        <nav className="chat__nav">
          {NAV.map((item) => (
            <button key={item.id} className="chat__nav-item" onClick={() => openSection(item.id)}>
              {item.label}
              {item.id === "approvals" && approvals.length > 0 && (
                <span className="chat__nav-badge">{approvals.length}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="chat__sidebar-footer">
          <span className={`chip ${connected ? "chip--ok" : "chip--danger"}`} title={connectionDetail}>
            {connected ? "backend connected" : "backend offline"}
          </span>
          <button className="ghost" onClick={() => openSection("settings")}>
            Connection settings
          </button>
          {native.isNative && (
            <button className="ghost" onClick={() => void native.quit()}>
              Quit Dobot
            </button>
          )}
        </div>
      </aside>

      <main className="chat__main">
        {section ? (
          <Section id={section} onBack={() => setSection(null)} />
        ) : (
          <>
        <header className="chat__header">
          <div className="chat__heading">
            <div className="chat__title">Dobot</div>
            <div className="chat__subtitle">
              <span className={`chat__status-dot chat__status-dot--${dotStatus}`} />
              {connected ? statusLabel(dotStatus) : "waiting for the backend"}
            </div>
          </div>
          <div className="chat__header-actions">
            <div className="chat__always-on">
              <Switch
                checked={dotEnabled}
                onChange={(value) => void setDotEnabled(value)}
                label="Always on"
                hint="Keep Dobot's floating dot on screen above every window"
              />
            </div>
            <button onClick={() => openSection("dashboard")}>Dashboard</button>
          </div>
        </header>

        {dotEnabled && (
          <div className="chat__banner">
            The floating dot is on screen. Drag it anywhere, click it for a quick ask, or turn{" "}
            <strong>Always on</strong> off to go back to a plain chat window.
          </div>
        )}

        <div className="chat__scroll" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="chat__hero">
              <div className="chat__hero-mark">●</div>
              <h1>An AI that doesn&apos;t just answer you.</h1>
              <p>
                Ask a question, point Dobot at your screen, or hand it a job. It plans what it will do,
                shows you the plan, asks before anything risky, and remembers what matters.
              </p>
              <div className="chat__suggestions">
                {SUGGESTIONS.map((item) => (
                  <button
                    key={item.title}
                    className="chat__suggestion"
                    onClick={() => {
                      setDraft(item.prompt);
                      composerRef.current?.focus();
                      if (item.screen) void startScreenCapture();
                    }}
                  >
                    <span className="chat__suggestion-title">{item.title}</span>
                    <span className="chat__suggestion-detail">{item.detail}</span>
                  </button>
                ))}
              </div>
              <div className="chat__hero-foot">
                <span className="mono">Ctrl+Shift+Space</span> talk · <span className="mono">Ctrl+Alt+L</span>{" "}
                show a window · <span className="mono">Ctrl+Shift+S</span> select screen ·{" "}
                <span className="mono">Ctrl+Alt+D</span> dashboard · <span className="mono">Ctrl+Shift+Esc</span>{" "}
                stop
              </div>
            </div>
          ) : (
            <div className="chat__thread-view">
              {messages.map((message) => (
                <Message
                  key={message.id}
                  message={message}
                  onOpenDashboard={() => openSection("tasks")}
                />
              ))}
              {lastError && <div className="notice notice--danger">{lastError}</div>}
            </div>
          )}
        </div>

        <footer
          className={`chat__composer ${dragging ? "chat__composer--drop" : ""}`}
          onDragOver={(event) => {
            if (Array.from(event.dataTransfer?.types ?? []).includes("Files")) {
              event.preventDefault();
              setDragging(true);
            }
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            const files = Array.from(event.dataTransfer?.files ?? []);
            if (!files.length) return;
            event.preventDefault();
            setDragging(false);
            void addFiles(files);
          }}
        >
          <input
            ref={fileRef}
            className="hidden-file"
            type="file"
            multiple
            accept={acceptAttribute(limits)}
            onChange={(event) => {
              void addFiles(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />

          {attachments.length > 0 && (
            <div className="attach-strip">
              {attachments.map((item) => (
                <div
                  className={`attach-chip attach-chip--${item.kind}`}
                  key={item.id}
                  title={`${item.name} · ${formatBytes(item.size)}`}
                >
                  {item.kind === "image" && item.previewUrl ? (
                    <img className="attach-chip__thumb" src={item.previewUrl} alt={item.name} />
                  ) : (
                    <span className="attach-chip__icon">
                      {item.kind === "image" ? <IconImage size={14} /> : <IconFile size={14} />}
                    </span>
                  )}
                  <span className="attach-chip__name">{item.name}</span>
                  <span className="attach-chip__size">{formatBytes(item.size)}</span>
                  <button
                    className="attach-chip__remove"
                    onClick={() => setAttachments((current) => current.filter((entry) => entry.id !== item.id))}
                    title="Remove"
                    aria-label={`Remove ${item.name}`}
                  >
                    <IconClose size={12} />
                  </button>
                </div>
              ))}
              <span className="attach-strip__limit">
                images ≤ {formatBytes(limits.images.max_bytes)} each · files ≤ {formatBytes(limits.files.max_bytes)} each
              </span>
            </div>
          )}

          {rejections.length > 0 && (
            <div className="attach-rejects" role="alert">
              <strong>Not attached</strong>
              {rejections.map((item) => (
                <span key={item.name}>
                  {item.name} — {item.reason}
                </span>
              ))}
              <button className="ghost" onClick={() => setRejections([])}>
                dismiss
              </button>
            </div>
          )}

          {dragging && <div className="chat__drop-hint">Drop to attach — images and text files</div>}

          {!connected && (
            <div className="notice notice--warn">
              The Dobot backend is not reachable at <span className="mono">{getBaseUrl()}</span>.
              {getBaseUrl().includes("hf.space") ? (
                <>
                  {" "}
                  The Space may be asleep, still building (first wake takes ~30 s), or missing its
                  token — check its logs on huggingface.co, then re-paste your HF read token in
                  Settings.
                </>
              ) : (
                <>
                  {" "}
                  Start it with{" "}
                  <span className="mono">cd backend &amp;&amp; uv run uvicorn app.main:app --port 8756</span>
                  , or change the address in Settings.
                </>
              )}
            </div>
          )}

          {pending && (
            <div className="chat__approvals">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <strong>Dobot is waiting on you</strong>
                <span className={`risk risk--${approvals[0].risk}`}>{approvals[0].risk}</span>
              </div>
              {approvals.map((approval) => (
                <ApprovalCard key={approval.id} approval={approval} />
              ))}
            </div>
          )}

          <div className="composer__toolbar">
            <div className="segmented" role="group" aria-label="Execution mode">
              {MODES.map((item) => (
                <button
                  key={item.id}
                  className={`segmented__item ${mode === item.id ? "segmented__item--active" : ""}`}
                  aria-pressed={mode === item.id}
                  title={item.hint}
                  onClick={() => setMode(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <span className="composer__mode-hint">{MODES.find((item) => item.id === mode)?.hint}</span>
            <span className="chat__composer-spacer" />
            <button
              className={`tool-chip ${shadowMode ? "tool-chip--on" : ""}`}
              onClick={() => void setShadowMode(!shadowMode)}
              title="Plan and preview actions without executing anything"
            >
              <IconMoon size={14} />
              Shadow {shadowMode ? "on" : "off"}
            </button>
            <button
              className={`tool-chip ${selection ? "tool-chip--on" : ""}`}
              onClick={() => void startScreenCapture()}
              disabled={capturing}
              title="Capture a region of your screen for the next message"
            >
              <IconScreen size={14} />
              {selection ? "Screen attached" : "Select screen"}
            </button>
            {native.isNative && (
              <button
                className="tool-chip"
                onClick={() => void captureActiveWindow()}
                disabled={capturing}
                title="Show Dobot the window you are in — Ctrl+Alt+L — and check it before it goes"
              >
                <IconEye size={14} />
                This window
              </button>
            )}
            {selection && (
              <button className="tool-chip" onClick={clearSelection} title="Remove the selected region">
                <IconClose size={12} />
                clear
              </button>
            )}
            <button
              className="tool-chip tool-chip--danger"
              onClick={() => void kill()}
              title="Stop the active task"
            >
              <IconStopCircle size={13} />
              Stop
            </button>
          </div>

          {selection?.image && (
            <img className="mini-thumb" src={previewDataUrl(selection) ?? undefined} alt="selected region" />
          )}

          <div className="composer">
            <button
              className="icon-btn"
              onClick={() => fileRef.current?.click()}
              title="Attach images or text files — or drag them onto the chat, or paste an image"
              aria-label="Attach files"
            >
              <IconPaperclip />
            </button>
            {voice.phase !== "idle" ? (
              <>
                <button
                  className="icon-btn"
                  onClick={() => voice.cancel()}
                  title="Discard what was said (Esc)"
                  aria-label="Discard recording"
                >
                  <IconClose />
                </button>
                {voice.phase === "recording" ? (
                  <span className="wave" aria-hidden="true">
                    {voice.levels.map((level, index) => (
                      <span key={index} className="wave__bar" style={{ height: `${Math.max(8, level * 100)}%` }} />
                    ))}
                  </span>
                ) : (
                  <span className="listening__label" role="status">
                    Writing it down…
                  </span>
                )}
              </>
            ) : (
              <>
                <textarea
                  ref={composerRef}
                  value={draft}
                  placeholder={
                    selection
                      ? "Ask about the selected region…"
                      : mode === "ask"
                        ? "Ask a question — Ask mode runs nothing…"
                        : attachments.length
                          ? "Tell Dobot what to do with what you attached…"
                          : "Message Dobot…"
                  }
                  onChange={(event) => setDraft(event.target.value)}
                  onPaste={(event) => {
                    const files = Array.from(event.clipboardData?.files ?? []);
                    if (files.length) {
                      event.preventDefault();
                      void addFiles(files);
                    }
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void submit();
                    }
                  }}
                  rows={1}
                />
                {voice.error && (
                  <span className="chat__voice-error" title={voice.error} role="alert">
                    mic: {voice.error}
                  </span>
                )}
                <button
                  className="icon-btn composer__mic"
                  onClick={() => void voice.start()}
                  title="Speak to Dobot — the audio is transcribed by your own backend"
                  aria-label="Voice input"
                >
                  <IconMic />
                </button>
              </>
            )}
            <button
              className="composer__send"
              onClick={() => (voice.phase === "recording" ? voice.stop() : void submit())}
              disabled={
                voice.phase === "processing" ||
                (voice.phase === "idle" && !draft.trim() && attachments.length === 0)
              }
              title={voice.phase === "recording" ? "Send what you said (Enter)" : "Send (Enter)"}
              aria-label="Send"
            >
              <IconSend />
            </button>
          </div>
          <div className="chat__composer-note">
            <label className="row" style={{ gap: 6, color: "var(--text-dim)", fontSize: 12 }}>
              <input
                type="checkbox"
                checked={includeSelection}
                style={{ width: "auto" }}
                onChange={(event) => setIncludeSelection(event.target.checked)}
              />
              attach the selected screen region
            </label>
            {voice.phase === "recording" ? (
              <span className="listening__hint">Listening — say it all, then Send · Esc to cancel</span>
            ) : (
              <span className="mono" style={{ color: "var(--text-dim)" }}>
                Enter to send · Shift+Enter for a new line
              </span>
            )}
          </div>
        </footer>
          </>
        )}
      </main>
    </div>
  );
}

function Message({ message, onOpenDashboard }: { message: ChatMessage; onOpenDashboard: () => void }) {
  const isUser = message.role === "user";
  return (
    <div
      className={`chat__message ${isUser ? "chat__message--user" : ""}`}
      data-message-id={message.id}
    >
      <div className={`chat__avatar ${isUser ? "chat__avatar--user" : "chat__avatar--dobot"}`}>
        {isUser ? "You" : "●"}
      </div>
      <div className="chat__bubble-wrap">
        <div className="chat__bubble">
          {(message.status || message.plan) && (
            <div className="message__meta">
              {message.status && <span className={`status status--${message.status}`}>{message.status}</span>}
              {message.plan && (
                <span className="mono" style={{ color: "var(--text-dim)" }}>
                  {message.plan.used_model} · {message.plan.model_tier}
                </span>
              )}
              <span>{new Date(message.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            </div>
          )}
          {message.attachments && message.attachments.length > 0 && (
            <div className="chat__bubble-attachments">
              {message.attachments.map((item) =>
                item.kind === "image" && item.previewUrl ? (
                  <img key={item.id} className="chat__bubble-image" src={item.previewUrl} alt={item.name} />
                ) : (
                  <span key={item.id} className="chat__bubble-file" title={`${item.name} · ${formatBytes(item.size)}`}>
                    <IconFile size={13} />
                    {item.name}
                  </span>
                ),
              )}
            </div>
          )}
          <div className="chat__bubble-text">{message.text}</div>
          {message.plan && message.plan.steps.length > 0 && <PlanView plan={message.plan} />}
          {message.sources && message.sources.length > 0 && (
            <div className="sources">
              <strong>Sources</strong>
              {message.sources.slice(0, 6).map((source, index) => (
                <a key={index} href={source.url ?? "#"} target="_blank" rel="noreferrer">
                  [{index + 1}] {source.title || source.url}
                </a>
              ))}
            </div>
          )}
          {message.approvals && message.approvals.length > 0 && (
            <button className="ghost" onClick={onOpenDashboard}>
              {message.approvals.length} approval(s) — open Tasks
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
