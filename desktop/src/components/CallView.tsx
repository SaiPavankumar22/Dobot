// The compact voice surface: the panel temporarily becomes an always-on voice orb rather than a
// second phone application. The state is deliberately glanceable; captions remain available for
// accessibility without turning the call into another rectangular chat window.

import { useCallback, useEffect, useRef, useState } from "react";
import { VoiceCall, type CallPhase, type CallStatus } from "../services/voiceCall";

interface Turn {
  role: "you" | "dobot";
  text: string;
}

const PHASE_LABEL: Record<CallPhase, string> = {
  connecting: "Connecting…",
  loading: "Preparing voice…",
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
      <div className="call call--ended call--orb">
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
    <div className={`call call--orb call--${phase}`}>
      <div className="voice-orb" role="status" aria-live="polite" aria-label={PHASE_LABEL[phase]}>
        <div className="voice-orb__bars" aria-hidden="true">
          {Array.from({ length: 7 }, (_, index) => (
            <span key={index} style={{ "--bar": index } as React.CSSProperties} />
          ))}
        </div>
        <div className="voice-orb__core">D</div>
      </div>
      <div className="voice-orb__phase">{PHASE_LABEL[phase]}</div>
      <div className="voice-orb__meta">
        {elapsedLabel(seconds)}
        {status ? ` · ${status.vad.detector} · ${turns.length} exchanges` : ""}
      </div>
      {error && (
        <div className="call__error" role="alert">
          {error}
        </div>
      )}
      <div className="voice-orb__caption" ref={feedRef} aria-live="polite">
        {live || turns[turns.length - 1]?.text || "Say something to Dobot"}
      </div>
      {hints.length > 0 && (
        <details className="voice-orb__details">
          <summary>Voice setup</summary>
          {hints.map((hint) => <div key={hint}>{hint}</div>)}
        </details>
      )}
      <footer className="call__bar call__bar--orb">
        <button
          className={`call__btn call__btn--round ${muted ? "call__btn--active" : ""}`}
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
          className="call__btn call__btn--round"
          onClick={() => callRef.current?.interrupt()}
          title="Cut Dobot off and listen instead"
        >
          Interrupt
        </button>
        <button className="call__btn call__btn--round call__btn--danger" onClick={() => callRef.current?.end("You ended the call")}>
          End
        </button>
      </footer>
    </div>
  );
}
