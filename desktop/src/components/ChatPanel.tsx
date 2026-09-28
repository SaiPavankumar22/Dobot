// The compact panel: ask, watch the plan, approve, done. Small on purpose — it sits next to whatever
// the user is already doing rather than replacing it.

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

  const [draft, setDraft] = useState("");
  const [useScreen, setUseScreen] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);

  const voice = useVoiceInput((text) => {
    setDraft((current) => (current ? `${current} ${text}` : text));
  });

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
        <span className={`chip ${connected ? "chip--ok" : "chip--danger"}`} title={connectionDetail}>
          {connected ? statusLabel(dotStatus) : "reconnecting"}
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
            Ask a question, or select something on screen and ask Dobot about it. Try “Explain this”
            after a selection, or “clean my downloads folder” to see the action firewall.
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

        <div className="composer">
          <textarea
            value={draft}
            placeholder={
              voice.phase === "recording"
                ? "Listening… click the mic to stop"
                : voice.phase === "processing"
                  ? "Transcribing…"
                  : selection
                    ? "Ask about the selected region…"
                    : "Ask Dobot — or tap the mic and speak…"
            }
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
            className={`chip chat__mic ${voice.phase === "recording" ? "chat__mic--live" : ""}`}
            onClick={() => (voice.phase === "recording" ? voice.stop() : void voice.start())}
            disabled={voice.phase === "processing"}
            title={
              voice.phase === "recording"
                ? "Stop recording and transcribe"
                : "Speak — audio is transcribed locally by Whisper"
            }
            aria-pressed={voice.phase === "recording"}
          >
            {voice.phase === "recording" ? "■" : voice.phase === "processing" ? "…" : "🎤"}
          </button>
          <button className="primary" onClick={() => void submit()} disabled={!draft.trim()}>
            Send
          </button>
        </div>
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
