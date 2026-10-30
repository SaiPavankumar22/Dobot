// The chat window — the default face of Dobot.
//
// It is deliberately an ordinary chatbot: a sidebar of conversations, a message thread, a composer.
// The ambient part of Dobot (the always-on floating dot) is opt-in and lives behind one switch in the
// header. With the switch off, nothing of Dobot is on screen but this window.

import { Fragment, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Activity } from "../pages/Activity";
import { Approvals } from "../pages/Approvals";
import { Automations } from "../pages/Automations";
import { ApprovalCard } from "../components/ApprovalCard";
import {
  IconClose,
  IconEye,
  IconFile,
  IconGrid,
  IconImage,
  IconMic,
  IconMoon,
  IconPlus,
  IconScreen,
  IconSearch,
  IconSend,
  IconStopCircle,
} from "../components/Icons";
import { Doctor } from "../pages/Doctor";
import { Guide } from "../pages/Guide";
import { Identity } from "../pages/Identity";
import { Memory } from "../pages/Memory";
import { Overview } from "../pages/Overview";
import { PlanView } from "../components/PlanView";
import { Security } from "../pages/Security";
import { Settings } from "../pages/Settings";
import { Skills } from "../pages/Skills";
import { Tasks } from "../pages/Tasks";
import { Switch } from "../components/Switch";
import { statusLabel } from "../components/DobotDot";
import { api } from "../services/api";
import {
  DEFAULT_LIMITS,
  formatBytes,
  readAttachments,
  textExtensions,
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
  { id: "identity", label: "Identity", hint: "The documents that make Dobot yours" },
  { id: "skills", label: "Skills", hint: "Reusable workflows it can run again" },
  { id: "activity", label: "Activity", hint: "The honest log of every step" },
  { id: "security", label: "Security", hint: "Permissions, guards and the kill switch" },
  { id: "doctor", label: "Doctor", hint: "Which capabilities are live, degraded or missing" },
  { id: "settings", label: "Settings", hint: "Keys, connection, appearance" },
] as const;

/** ChatGPT-style buckets for the Recent list — newest groups first, oldest last. */
const THREAD_GROUPS = ["Today", "Yesterday", "Previous 7 days", "Older"] as const;

function threadGroup(iso: string): string {
  const stamp = new Date(iso).getTime();
  if (!Number.isFinite(stamp)) return "Older";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (stamp >= today) return "Today";
  if (stamp >= today - 86_400_000) return "Yesterday";
  if (stamp >= today - 7 * 86_400_000) return "Previous 7 days";
  return "Older";
}

/** Workspace sections render inside the chat window — the sidebar must not spawn a second window. */
function Section({ id, onBack, onWorkspace }: { id: string; onBack: () => void; onWorkspace: () => void }) {
  const body =
    id === "guide" ? <Guide /> :
    id === "overview" ? <Overview /> :
    id === "tasks" ? <Tasks /> :
    id === "automations" ? <Automations /> :
    id === "approvals" ? <Approvals /> :
    id === "memory" ? <Memory /> :
    id === "identity" ? <Identity /> :
    id === "skills" ? <Skills /> :
    id === "activity" ? <Activity /> :
    id === "security" ? <Security /> :
    id === "doctor" ? <Doctor /> :
    id === "settings" ? <Settings /> :
    <div className="notice">Unknown section.</div>;
  const label = NAV.find((item) => item.id === id)?.label ?? id;
  return (
    <div className="chat__section-view">
      <div className="chat__section-bar">
        <button className="ghost" onClick={onBack} title="Back to the conversation">← Chat</button>
        <button className="ghost" onClick={onWorkspace} title="Back to the workspace cards">← Workspace</button>
        <strong>{label}</strong>
        <span className="chat__composer-spacer" />
      </div>
      <div className="chat__section-body">{body}</div>
    </div>
  );
}

/**
 * The workspace picker: every sidebar section as a card, opened from the footer's Workspace
 * button. Cards navigate in-window (same as the old inline nav) so no second window ever spawns
 * from here.
 */
function WorkspaceGrid({ onClose, onPick }: { onClose: () => void; onPick: (id: string) => void }) {
  const approvals = useDobot((state) => state.approvals);
  return (
    <div className="chat__workspace">
      <div className="chat__section-bar">
        <button className="ghost" onClick={onClose} title="Back to the conversation">← Chat</button>
        <strong>Workspace</strong>
        <span className="chat__composer-spacer" />
      </div>
      <div className="chat__workspace-grid">
        {NAV.map((item) => (
          <button key={item.id} className="chat__workspace-card" onClick={() => onPick(item.id)}>
            <span className="chat__workspace-card-title">
              {item.label}
              {item.id === "approvals" && approvals.length > 0 && (
                <span className="chat__nav-badge">{approvals.length}</span>
              )}
            </span>
            <span className="chat__workspace-card-hint">{item.hint}</span>
          </button>
        ))}
      </div>
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
  const research = useDobot((state) => state.research);
  const [researchMode, setResearchMode] = useState(false);
  const [researchDepth, setResearchDepth] = useState<"quick" | "deep">("quick");
  const captureScreen = useDobot((state) => state.captureScreen);
  const captureActiveWindow = useDobot((state) => state.captureActiveWindow);
  const clearSelection = useDobot((state) => state.clearSelection);
  const setShadowMode = useDobot((state) => state.setShadowMode);
  const setDotEnabled = useDobot((state) => state.setDotEnabled);
  const newConversation = useDobot((state) => state.newConversation);
  const conversations = useDobot((state) => state.conversations);
  const activeConversationId = useDobot((state) => state.activeConversationId);
  const openConversation = useDobot((state) => state.openConversation);
  const setCapturing = useDobot((state) => state.setCapturing);
  const kill = useDobot((state) => state.kill);

  const [draft, setDraft] = useState("");
  const [includeSelection, setIncludeSelection] = useState(true);
  const [section, setSection] = useState<string | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [rejections, setRejections] = useState<Rejection[]>([]);
  const [limits, setLimits] = useState<AttachmentLimits>(DEFAULT_LIMITS);
  const [dragging, setDragging] = useState(false);
  const [composerMenuOpen, setComposerMenuOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const imageRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!composerMenuOpen) return;
    const close = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest(".composer__plus-wrap")) {
        setComposerMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [composerMenuOpen]);

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
    if (researchMode && sending.length === 0) {
      await research(text, researchDepth);
      return;
    }
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
    // Sections open inside this window — the dashboard window is not reachable from here anymore.
    setSection(id);
  }

  return (
    <div className="chat">
      <aside className="chat__sidebar">
        <div className="chat__brand">
          <img className="chat__brand-logo" src="/branding/dobot-logo.png" alt="Dobot" />
        </div>

        <button
          className="chat__new"
          onClick={() => {
            setWorkspaceOpen(false);
            setSection(null);
            newConversation();
          }}
          title="Start a fresh conversation"
        >
          ＋ New chat
        </button>

        <div className="chat__section-label">Recent</div>
        <div className="chat__threads">
          {conversations.length === 0 && <div className="chat__empty-hint">No conversations yet</div>}
          {THREAD_GROUPS.map((group) => {
            const items = conversations.filter((item) => threadGroup(item.updated_at) === group);
            if (items.length === 0) return null;
            return (
              <Fragment key={group}>
                <div className="chat__threads-label">{group}</div>
                {items.map((conversation) => (
                  <button
                    key={conversation.id}
                    className={`chat__thread${
                      conversation.id === activeConversationId ? " chat__thread--active" : ""
                    }`}
                    onClick={() => {
                      setWorkspaceOpen(false);
                      setSection(null);
                      void openConversation(conversation.id);
                    }}
                    title={conversation.summary || conversation.title}
                  >
                    <span className="chat__thread-title">{conversation.title}</span>
                    <span className="chat__thread-time">
                      {new Date(conversation.updated_at).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </button>
                ))}
              </Fragment>
            );
          })}
        </div>

        <div className="chat__sidebar-footer">
          <span className={`chip ${connected ? "chip--ok" : "chip--danger"}`} title={connectionDetail}>
            {connected ? "backend connected" : "backend offline"}
          </span>
          <button
            className="ghost chat__workspace-btn"
            onClick={() => {
              setSection(null);
              setWorkspaceOpen(true);
            }}
            title="Every workspace section, as cards"
          >
            <span className="chat__workspace-btn-label">
              <IconGrid size={15} />
              Workspace
            </span>
            {approvals.length > 0 && <span className="chat__nav-badge">{approvals.length}</span>}
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
          <Section
            id={section}
            onBack={() => setSection(null)}
            onWorkspace={() => {
              setSection(null);
              setWorkspaceOpen(true);
            }}
          />
        ) : workspaceOpen ? (
          <WorkspaceGrid
            onClose={() => setWorkspaceOpen(false)}
            onPick={(id) => {
              setWorkspaceOpen(false);
              openSection(id);
            }}
          />
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
          </div>
        </header>

        {dotEnabled && (
          <div className="chat__banner">
            The floating dot is on screen. Drag it anywhere, single-click it for the quick menu
            (chat · snip · voice), double-click it for a quick ask, or turn{" "}
            <strong>Always on</strong> off to go back to a plain chat window.
          </div>
        )}

        <div className="chat__scroll" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="chat__hero">
              <img className="chat__hero-mark" src="/branding/dobot-logo.png" alt="" />
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
            accept={textExtensions(limits).join(",")}
            onChange={(event) => {
              void addFiles(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />
          <input
            ref={imageRef}
            className="hidden-file"
            type="file"
            multiple
            accept="image/*"
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
              <>Dobot is starting the local backend. Please wait a moment…</>
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
              className={`tool-chip ${researchMode ? "tool-chip--on" : ""}`}
              onClick={() => {
                setResearchMode(!researchMode);
                setResearchDepth("quick");
              }}
              title={
                researchDepth === "deep"
                  ? "Use DeepAgents multi-search research with citations"
                  : "Use the fast Tavily research pipeline and answer with citations"
              }
            >
              <IconSearch size={14} />
              {researchMode ? (researchDepth === "deep" ? "Deep research on" : "Quick research on") : "Research off"}
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
            <div className="composer__plus-wrap">
              <button
                className={`icon-btn composer__plus ${composerMenuOpen ? "composer__plus--open" : ""}`}
                onClick={() => setComposerMenuOpen((open) => !open)}
                title="Add files or start deep research"
                aria-label="Add files or start deep research"
                aria-expanded={composerMenuOpen}
                aria-haspopup="menu"
              >
                <IconPlus />
              </button>
              {composerMenuOpen && (
                <div className="composer__plus-menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setComposerMenuOpen(false);
                      fileRef.current?.click();
                    }}
                  >
                    <IconFile size={16} />
                    <span><strong>Add files</strong><small>Text and code documents</small></span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setComposerMenuOpen(false);
                      imageRef.current?.click();
                    }}
                  >
                    <IconImage size={16} />
                    <span><strong>Add photos</strong><small>PNG, JPEG, WebP, or GIF</small></span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className={researchMode ? "composer__plus-menu-item--active" : ""}
                    onClick={() => {
                      setComposerMenuOpen(false);
                      setResearchMode(true);
                      setResearchDepth("deep");
                      composerRef.current?.focus();
                    }}
                  >
                    <IconSearch size={16} />
                    <span><strong>Deep research</strong><small>Multi-search cited report</small></span>
                  </button>
                </div>
              )}
            </div>
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

/**
 * Inline markdown: **bold**, *italic*, `code`, [links](url). Splits on a single pass so model
 * output like **What's missing:** renders as formatted text instead of literal asterisks.
 */
const INLINE_MD = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\))/g;

function renderInline(text: string, keyBase: string): React.ReactNode[] {
  const parts = text.split(INLINE_MD);
  return parts.filter(Boolean).map((part, index) => {
    const key = `${keyBase}-${index}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={key}>{part.slice(1, -1)}</em>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
    if (link) {
      return (
        <a key={key} href={link[2]} target="_blank" rel="noreferrer">
          {link[1]}
        </a>
      );
    }
    return <Fragment key={key}>{part}</Fragment>;
  });
}

/** Block markdown: fenced code, headings, ordered/unordered lists, rules, paragraphs. */
function renderMarkdown(body: string): React.ReactNode[] {
  const lines = body.replace(/\r\n/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  const nextKey = () => `md-${key++}`;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (line.trimStart().startsWith("```")) {
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trimStart().startsWith("```")) buf.push(lines[i++]);
      i += 1;
      blocks.push(
        <pre key={nextKey()}>
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push(
        <p key={nextKey()} className={`md-heading md-heading--${heading[1].length}`}>
          {renderInline(heading[2], nextKey())}
        </p>,
      );
      i += 1;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={nextKey()} />);
      i += 1;
      continue;
    }

    const bullet = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (bullet) {
      const ordered = /^\s*\d/.test(line);
      const items: React.ReactNode[] = [];
      while (i < lines.length) {
        const next = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!next || /^\s*([-*+])/.test(lines[i]) === ordered) break;
        items.push(<li key={`${nextKey()}-${items.length}`}>{renderInline(next[2], `li-${items.length}`)}</li>);
        i += 1;
      }
      blocks.push(ordered ? <ol key={nextKey()}>{items}</ol> : <ul key={nextKey()}>{items}</ul>);
      continue;
    }

    // Paragraph: every consecutive non-blank, non-block line joined, with soft breaks kept.
    const buf: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !lines[i].trimStart().startsWith("```") &&
      !/^\s*(-{3,}|\*{3,})\s*$/.test(lines[i])
    ) {
      buf.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <p key={nextKey()}>{renderInline(buf.join("\n"), nextKey())}</p>,
    );
  }
  return blocks;
}

function Message({ message, onOpenDashboard }: { message: ChatMessage; onOpenDashboard: () => void }) {
  const isUser = message.role === "user";
  const [lightbox, setLightbox] = useState<string | null>(null);

  useEffect(() => {
    if (!lightbox) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setLightbox(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox]);

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
                  <img
                    key={item.id}
                    className="chat__bubble-image"
                    src={item.previewUrl}
                    alt={item.name}
                    title="Click to view full size"
                    onClick={() => setLightbox(item.previewUrl!)}
                  />
                ) : (
                  <span key={item.id} className="chat__bubble-file" title={`${item.name} · ${formatBytes(item.size)}`}>
                    <IconFile size={13} />
                    {item.name}
                  </span>
                ),
              )}
            </div>
          )}
          <div className={`chat__bubble-text${isUser ? "" : " chat__bubble-text--md"}`}>
            {isUser ? message.text : renderMarkdown(message.text)}
          </div>
          {message.research && (
            <div className={`research-meta ${message.research.degraded ? "research-meta--degraded" : ""}`}>
              <strong>
                {message.research.engine === "deepagents"
                  ? "DeepAgents report"
                  : message.research.engine === "tavily_fallback"
                    ? "Quick research fallback"
                    : "Quick research"}
              </strong>
              {message.research.notice && <span>{message.research.notice}</span>}
            </div>
          )}
          {message.plan && message.plan.steps.length > 0 && <PlanView plan={message.plan} />}
          {message.sources && message.sources.length > 0 && (
            <div className="sources">
              <strong>Sources</strong>
              {message.sources.slice(0, message.research?.engine === "deepagents" ? 12 : 6).map((source, index) => (
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
      {lightbox &&
        createPortal(
          <div className="chat__lightbox" onClick={() => setLightbox(null)} role="dialog" aria-label="Image preview">
            <img src={lightbox} alt="Attachment preview" />
            <span className="chat__lightbox-hint">Click anywhere or press Esc to close</span>
          </div>,
          document.body,
        )}
    </div>
  );
}
