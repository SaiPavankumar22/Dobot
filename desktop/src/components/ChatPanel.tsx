// The compact panel: ask, watch the plan, approve, done. Small on purpose — it sits next to whatever
// the user is already doing rather than replacing it.
//
// It is also the voice surface: the talk chord (Ctrl+Shift+Space) always comes here, because only
// one window can hold the microphone. Press and the panel is already up and listening, let go and
// what was said is sent; the mic button in the composer is the slower, editable version of the same
// thing.

import { useEffect, useRef, useState } from "react";
import { ApprovalCard } from "./ApprovalCard";
import { PlanView } from "./PlanView";
import { statusLabel } from "./DobotDot";
import { native } from "../services/native";
import { previewDataUrl } from "../services/screen";
import { useVoiceInput } from "../services/voice";
import { useDobot } from "../store/dobotStore";

export function ChatPanel() {
  const messages = useDobot((state) => state.messages);
  const approvals = useDobot((state) => state.approvals);
  const dotStatus = useDobot((state) => state.dotStatus);
  const connected = useDobot((state) => state.connected);
  const connectionDetail = useDobot((state) => state.connectionDetail);
  const shadowMode = useDobot((state) => state.shadowMode);
  const selection = useDobot((state) => state.selection);
  const lastError = useDobot((state) => state.lastError);
  const send = useDobot((state) => state.send);
  const captureScreen = useDobot((state) => state.captureScreen);
  const clearSelection = useDobot((state) => state.clearSelection);
  const setShadowMode = useDobot((state) => state.setShadowMode);
  const kill = useDobot((state) => state.kill);
  const capturing = useDobot((state) => state.capturing);
  const setCapturing = useDobot((state) => state.setCapturing);
  const captureActiveWindow = useDobot((state) => state.captureActiveWindow);
  const dotDetail = useDobot((state) => state.dotDetail);

  const [draft, setDraft] = useState("");
  const [useScreen, setUseScreen] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);
  /** Always the words in the box now: a recording's callbacks land later than this render. */
  const draftRef = useRef(draft);
  draftRef.current = draft;
  /** Whether the recording came from the talk chord, which sends rather than filling the box. */
  const hotkeyVoice = useRef(false);

  const voice = useVoiceInput(
    (text) => {
      if (hotkeyVoice.current) {
        hotkeyVoice.current = false;
        const base = draftRef.current.trim();
        const message = base ? `${base} ${text}` : text;
        if (message) {
          void send(message, { shadow: shadowMode, useSelection: useScreen && Boolean(selection) });
          return;
        }
      }
      setDraft((current) => (current ? `${current} ${text}` : text));
    },
    () => {
      // However it ended — transcribed, too short to be words, cancelled, mic refused — the
      // chord's session is over, and the shell must be told so the next press starts fresh.
      hotkeyVoice.current = false;
      void native.voiceDone();
    },
  );

  // The talk chord: the shell owns the key, this window owns the microphone.
  const { start: startListening, stop: stopListening, cancel: cancelListening } = voice;
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void native
      .onVoice((signal) => {
        if (signal === "start") {
          hotkeyVoice.current = true;
          void startListening();
        } else if (signal === "stop") {
          stopListening();
        }
      })
      .then((fn) => {
        unlisten = fn ?? undefined;
      });
    return () => unlisten?.();
  }, [startListening, stopListening]);

  // A press can land before this page has finished loading; the shell still holds the session open.
  useEffect(() => {
    void native.voiceListening().then((listening) => {
      if (listening) {
        hotkeyVoice.current = true;
        void startListening();
      }
    });
  }, [startListening]);

  // Esc lets go of the microphone, the way any recorder does.
  useEffect(() => {
    if (voice.phase !== "recording") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        cancelListening();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [voice.phase, cancelListening]);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, dotStatus]);

  async function submit() {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    await send(text, { shadow: shadowMode, useSelection: useScreen && Boolean(selection) });
  }

  return (
    <div className="panel">
      <header className="panel__header">
        <span className="dot__glyph" style={{ fontSize: 14, color: "var(--accent)" }}>
          ●
        </span>
        <span className="panel__title">Dobot</span>
        <span
          className={`chip ${connected ? "chip--ok" : "chip--danger"} chip--detail`}
          title={connected ? (dotDetail ? `${statusLabel(dotStatus)} — ${dotDetail}` : connectionDetail) : connectionDetail}
        >
          {connected
            ? dotDetail && (dotStatus === "EXECUTING" || dotStatus === "THINKING")
              ? dotDetail
              : statusLabel(dotStatus)
            : "reconnecting"}
        </span>
        <span className="panel__spacer" />
        <button className="ghost" title="Open the dashboard" onClick={() => void native.openDashboard()}>
          ⧉
        </button>
        <button className="ghost" title="Hide the panel" onClick={() => void native.hidePanel()}>
          —
        </button>
      </header>

      <div className="panel__body" ref={bodyRef}>
        {approvals.length > 0 && (
          <>
            <div className="row">
              <strong>Approval required</strong>
              <span className={`risk risk--${approvals[0].risk}`}>{approvals[0].risk}</span>
            </div>
            {approvals.map((approval) => (
              <ApprovalCard key={approval.id} approval={approval} />
            ))}
          </>
        )}

        {messages.length === 0 && (
          <div className="notice">
            Ask a question, hold <span className="mono">Ctrl+Shift+Space</span> anywhere and speak, or
            show Dobot a window (<span className="mono">Ctrl+Alt+L</span>). Try “clean my downloads
            folder” to see the action firewall.
          </div>
        )}

        {messages.map((message) => (
          <div key={message.id} className={`message ${message.role === "user" ? "message--user" : ""}`}>
            <div className="message__meta">
              <span className="message__role">{message.role === "user" ? "You" : "Dobot"}</span>
              {message.status && <span className={`status status--${message.status}`}>{message.status}</span>}
              <span>{new Date(message.createdAt).toLocaleTimeString()}</span>
            </div>
            <div>{message.text}</div>
            {message.plan && message.plan.steps.length > 0 && <PlanView plan={message.plan} />}
            {message.sources && message.sources.length > 0 && (
              <div className="sources">
                <strong>Sources</strong>
                {message.sources.slice(0, 6).map((source, index) => (
                  <span key={index}>
                    [{index + 1}] {source.title || source.url}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}

        {lastError && <div className="notice notice--danger">{lastError}</div>}
      </div>

      <footer className="panel__footer">
        <div className="chip-row">
          <button
            className={`chip ${shadowMode ? "chip--warn" : ""}`}
            onClick={() => void setShadowMode(!shadowMode)}
            title="Plan actions without executing them"
          >
            {shadowMode ? "Shadow mode: on" : "Shadow mode: off"}
          </button>
          <button
            className={`chip ${selection ? "chip--ok" : ""}`}
            onClick={() => {
              if (native.isNative) {
                void captureScreen();
              } else {
                setCapturing(true);
              }
            }}
            disabled={capturing}
          >
            {selection ? "Screen selected" : "Select screen"}
          </button>
          {native.isNative && (
            <button
              className="chip"
              onClick={() => void captureActiveWindow()}
              disabled={capturing}
              title="Show Dobot the window you are in (Ctrl+Alt+L) — you check it before it goes"
            >
              This window
            </button>
          )}
          {selection && (
            <button className="chip" onClick={clearSelection}>
              clear
            </button>
          )}
          <button className="chip chip--danger" onClick={() => void kill()} title="Stop the active task">
            STOP
          </button>
        </div>

        {selection?.image && (
          <img className="mini-thumb" src={previewDataUrl(selection) ?? undefined} alt="selected region" />
        )}

        <div className={`composer ${voice.phase !== "idle" ? "composer--listening" : ""}`}>
          {voice.phase !== "idle" ? (
            <>
              <button
                className="chip"
                onClick={() => voice.cancel()}
                title="Discard what was said (Esc)"
                aria-label="Discard recording"
              >
                ✕
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
                value={draft}
                placeholder={selection ? "Ask about the selected region…" : "Ask Dobot — or tap the mic and speak…"}
                onChange={(event) => setDraft(event.target.value)}
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
                  mic
                </span>
              )}
              <button
                className="chip chat__mic"
                onClick={() => void voice.start()}
                title="Speak — audio is transcribed locally by Whisper"
              >
                🎤
              </button>
            </>
          )}
          <button
            className="primary"
            onClick={() => (voice.phase === "recording" ? voice.stop() : void submit())}
            disabled={voice.phase === "processing" || (voice.phase === "idle" && !draft.trim())}
            title={
              voice.phase === "recording"
                ? hotkeyVoice.current
                  ? "Send what you said"
                  : "Write it into the box"
                : "Send (Enter)"
            }
          >
            {voice.phase === "recording" && !hotkeyVoice.current ? "Transcribe" : "Send"}
          </button>
        </div>
        {voice.phase === "recording" && (
          <div className="listening__hint">
            {hotkeyVoice.current
              ? "Let go of Ctrl+Shift+Space (or press it again) to send · Esc to cancel"
              : "Say it all, then Transcribe — it lands in the box to check · Esc to cancel"}
          </div>
        )}
        <div className="row" style={{ justifyContent: "space-between" }}>
          <label className="row" style={{ gap: 6, color: "var(--text-dim)", fontSize: 12 }}>
            <input
              type="checkbox"
              checked={useScreen}
              style={{ width: "auto" }}
              onChange={(event) => setUseScreen(event.target.checked)}
            />
            include selected screen
          </label>
          <span className="mono" style={{ color: "var(--text-dim)" }}>
            Enter to send · Shift+Enter for a new line
          </span>
        </div>
      </footer>
    </div>
  );
}
