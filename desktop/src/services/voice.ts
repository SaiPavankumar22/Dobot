// Voice input: capture the microphone, transcribe locally via the backend.
//
// The audio goes to Dobot's own /voice/transcribe endpoint (CrisperWhisper 2.0 small running in the
// backend process) — nothing leaves the machine. The hook is shared by the chat composer and the dot
// panel so both surfaces behave identically.

import { useCallback, useRef, useState } from "react";
import { getBaseUrl } from "./api";

export type TranscribeStatus = {
  ready: boolean;
  state: string;
  model: string;
  ffmpeg: boolean;
  detail?: string;
  fix?: string;
};

export async function transcribeStatus(): Promise<TranscribeStatus> {
  try {
    const response = await fetch(`${getBaseUrl()}/voice/transcribe/status`);
    if (!response.ok) throw new Error("status unavailable");
    return (await response.json()) as TranscribeStatus;
  } catch {
    return {
      ready: false,
      state: "unreachable",
      model: "nyralabs/CrisperWhisper2.0_small",
      ffmpeg: false,
      detail: "Backend unreachable",
      fix: "Start the Dobot backend to enable voice input.",
    };
  }
}

export async function transcribeAudio(blob: Blob): Promise<{ text: string; error?: string }> {
  const form = new FormData();
  const extension = blob.type.includes("webm")
    ? "webm"
    : blob.type.includes("ogg")
      ? "ogg"
      : blob.type.includes("mp4")
        ? "m4a"
        : "wav";
  form.append("file", blob, `recording.${extension}`);
  let response: Response;
  try {
    response = await fetch(`${getBaseUrl()}/voice/transcribe`, { method: "POST", body: form });
  } catch {
    return { text: "", error: "The Dobot backend is not reachable, so the recording could not be transcribed." };
  }
  const body = (await response.json().catch(() => ({}))) as {
    text?: string;
    error?: string;
    detail?: { message?: string } | string;
  };
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
    return { text: "", error: detail || body.error || `Transcription failed (HTTP ${response.status})` };
  }
  return { text: String(body.text ?? ""), error: body.error || undefined };
}

export type VoiceInputState = {
  /** "recording" while the mic is live, "processing" while the backend transcribes. */
  phase: "idle" | "recording" | "processing";
  error: string;
  start: () => Promise<void>;
  stop: () => void;
};

/**
 * Mic capture → local transcription → onText(text).
 *
 * Errors are delivered through the state's `error` field (throwing inside MediaRecorder's onstop
 * handler would vanish — nobody awaits that callback), and through a rejected promise from start()
 * for permission problems, which happen synchronously enough to matter.
 */
export function useVoiceInput(onText: (text: string) => void): VoiceInputState {
  const [phase, setPhase] = useState<"idle" | "recording" | "processing">("idle");
  const [error, setError] = useState("");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  const cleanup = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  const start = useCallback(async () => {
    setError("");
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("This environment has no microphone access. Use the desktop app or a secure browser context.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      const chunks: BlobPart[] = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      };
      recorder.onstop = async () => {
        const mimeType = recorder.mimeType || "audio/webm";
        cleanup();
        const blob = new Blob(chunks, { type: mimeType });
        if (blob.size === 0) {
          setPhase("idle");
          setError("The recording was empty — check that the right microphone is selected.");
          return;
        }
        setPhase("processing");
        const { text, error: transcribeError } = await transcribeAudio(blob);
        setPhase("idle");
        if (transcribeError) {
          setError(transcribeError);
          return;
        }
        if (text) onText(text);
      };
      recorder.onerror = () => {
        cleanup();
        setPhase("idle");
        setError("Recording failed unexpectedly.");
      };
      recorder.start();
      setPhase("recording");
    } catch (exc) {
      cleanup();
      setPhase("idle");
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(
        message.includes("Permission") || message.includes("denied")
          ? "Microphone permission was denied. Allow it in your system or browser settings."
          : message,
      );
    }
  }, [cleanup, onText]);

  const stop = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop(); // onstop handles cleanup + transcription
    } else {
      cleanup();
      setPhase("idle");
    }
  }, [cleanup]);

  return { phase, error, start, stop };
}
