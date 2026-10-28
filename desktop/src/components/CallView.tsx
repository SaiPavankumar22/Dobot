// The call surface: what the panel becomes when the dot menu's Voice option picks up.
//
// Deliberately shaped like a phone call rather than a chat window — one status ring, one live
// caption, one row of call buttons — because that is the contract the whole pipeline is built to:
// Dobot is listening, thinking, or speaking, and hanging up is always one red button away.

import { useCallback, useEffect, useRef, useState } from "react";
import { VoiceCall, type CallPhase, type CallStatus } from "../services/voiceCall";

interface Turn {
  role: "you" | "dobot";
  text: string;
}

const PHASE_LABEL: Record<CallPhase, string> = {
  connecting: "Connecting…",
  listening: "Listening…",
  thinking: "Dobot is thinking…",
  speaking: "Dobot is speaking…",
  ended: "Call ended",
};

function elapsedLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function CallView({ onEnd }: { onEnd: () => void }) {
  const [phase, setPhase] = useState<CallPhase>("connecting");
  const [status, setStatus] = useState<CallStatus | null>(null);
  const [hints, setHints] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState("");
  const [muted, setMuted] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [ended, setEnded] = useState("");

  const liveRef = useRef("");
  const feedRef = useRef<HTMLDivElement>(null);
  const callRef = useRef<VoiceCall | null>(null);

  const commitLive = useCallback(() => {
    const text = liveRef.current.trim();
    liveRef.current = "";
    setLive("");
    if (text) setTurns((previous) => [...previous, { role: "dobot", text }]);
  }, []);

  useEffect(() => {
    const call = new VoiceCall({
      onPhase: (next) => {
        setPhase(next);
        // A reply that finished — or was cut off — becomes a line in the feed: the transcript
        // shows what was actually said, not what the model went on to generate after silence.
        if (next === "listening" || next === "thinking") commitLive();
      },
      onStatus: (received) => {
        setStatus(received);
        if (received.hints) setHints(received.hints);
      },
      onUserText: (text) => {
        commitLive();
        setTurns((previous) => [...previous, { role: "you", text }]);
      },
      onAssistantDelta: (delta) => {
        liveRef.current += delta;
        setLive(liveRef.current);
      },
      onError: (message) => setError(message),
      onEnd: (reason) => {
        commitLive();
        setEnded(reason);
      },
    });
    callRef.current = call;
    void call.start();
    return () => {
      call.end("Call ended");
      callRef.current = null;
    };
  }, [commitLive]);

  useEffect(() => {
    if (ended) return undefined;
    const timer = window.setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [ended]);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
  }, [turns.length, live]);

  if (ended) {
    return (
      <div className="call call--ended">
        <div className="call__ended-mark" aria-hidden="true">
          ✕
        </div>
        <div className="call__phase">{ended}</div>
        <div className="call__meta">
          {turns.length} exchange{turns.length === 1 ? "" : "s"} · {elapsedLabel(seconds)}
        </div>
        <button className="call__btn" onClick={onEnd}>
          Back to chat
        </button>
      </div>
    );
  }

  return (
    <div className="call">
      <header className="call__head">
        <span className={`call__ring call__ring--${phase}`} aria-hidden="true">
          <span />
        </span>
        <div className="call__ident">
          <div className="call__phase">{PHASE_LABEL[phase]}</div>
          <div className="call__meta">
            {elapsedLabel(seconds)}
            {status ? ` · ${status.llm.model}` : ""}
            {status ? ` · ${status.vad.detector} vad` : ""}
          </div>
        </div>
        <span className={`chip ${status ? "chip--ok" : ""}`}>{status ? "on air" : "…"}</span>
      </header>

      {hints.length > 0 && (
        <ul className="call__hints">
          {hints.map((hint) => (
            <li key={hint}>{hint}</li>
          ))}
        </ul>
      )}
      {error && (
        <div className="call__error" role="alert">
          {error}
        </div>
      )}

      <div className="call__feed" ref={feedRef}>
        {turns.length === 0 && !live && (
          <div className="call__empty">
            Dobot is on the line. Say something like “what are you working on for me today?” — talk
            over an answer any time and it stops.
          </div>
        )}
        {turns.map((turn, index) => (
          <div key={`${turn.role}-${index}`} className={`call__line call__line--${turn.role}`}>
            <span className="call__speaker">{turn.role === "you" ? "You" : "Dobot"}</span>
            <span>{turn.text}</span>
          </div>
        ))}
        {live && (
          <div className="call__line call__line--dobot call__line--live">
            <span className="call__speaker">Dobot</span>
            <span>{live}</span>
          </div>
        )}
      </div>

      <footer className="call__bar">
        <button
          className={`call__btn ${muted ? "call__btn--active" : ""}`}
          onClick={() => {
            const next = !muted;
            setMuted(next);
            callRef.current?.setMuted(next);
          }}
          title={muted ? "Unmute the microphone" : "Mute the microphone"}
        >
          {muted ? "Unmute" : "Mute"}
        </button>
        <button
          className="call__btn"
          onClick={() => callRef.current?.interrupt()}
          title="Cut Dobot off and listen instead"
        >
          Stop
        </button>
        <button className="call__btn call__btn--danger" onClick={() => callRef.current?.end("You ended the call")}>
          End call
        </button>
      </footer>
    </div>
  );
}
