// Voice input: capture the microphone, transcribe locally via the backend.
//
// The audio goes to Dobot's own /voice/transcribe endpoint (Whisper small via faster-whisper,
// running in the backend process) — nothing leaves the machine. The hook is shared by the chat
// composer and the dot panel so both surfaces behave identically.
//
// Two decisions worth their comments: the microphone is asked for raw (no echo cancellation, no
// noise suppression, no auto gain — Whisper hears a room better than the browser's filters, which
// blur words), and a recording shorter than MIN_RECORDING_MS is dropped as a slip of the key
// rather than sent to the model to be turned into nonsense.

import { useCallback, useEffect, useRef, useState } from "react";
import { authHeader, getBaseUrl } from "./api";

export type TranscribeStatus = {
  ready: boolean;
  state: string;
  model: string;
  ffmpeg: boolean;
  detail?: string;
  fix?: string;
};

/** Shorter than this was a tap, not something said. */
const MIN_RECORDING_MS = 400;
/** Bars in the waveform. */
const BARS = 24;
/** How often the waveform samples the level, in milliseconds. */
const LEVEL_MS = 80;

export async function transcribeStatus(): Promise<TranscribeStatus> {
  try {
    const response = await fetch(`${getBaseUrl()}/voice/transcribe/status`, { headers: authHeader() });
    if (!response.ok) throw new Error("status unavailable");
    return (await response.json()) as TranscribeStatus;
  } catch {
    return {
      ready: false,
      state: "unreachable",
      model: "Systran/faster-whisper-small",
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
    response = await fetch(`${getBaseUrl()}/voice/transcribe`, {
      method: "POST",
      body: form,
      headers: authHeader(),
    });
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
  /** How loud it is now, 0 to 1, and the last few — for the waveform in the composer. */
  levels: number[];
  start: () => Promise<void>;
  stop: () => void;
  /** Abandon the recording without transcribing — Esc, or the shell saying stop after a cancel. */
  cancel: () => void;
};

/**
 * Mic capture → local transcription → onText(text).
 *
 * `onEnd` fires once per recording session however it ended (transcribed, too short to be words,
 * cancelled, or failed): the push-to-talk chord needs to know the microphone is free again.
 *
 * Errors are delivered through the state's `error` field (throwing inside MediaRecorder's onstop
 * handler would vanish — nobody awaits that callback), and through a rejected promise from start()
 * for permission problems, which happen synchronously enough to matter.
 */
export function useVoiceInput(onText: (text: string) => void, onEnd?: () => void): VoiceInputState {
  const [phase, setPhase] = useState<"idle" | "recording" | "processing">("idle");
  const [error, setError] = useState("");
  const [levels, setLevels] = useState<number[]>(() => Array(BARS).fill(0));
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const levelTimerRef = useRef<number | null>(null);
  const startedAtRef = useRef(0);
  const discardRef = useRef(false);
  // The recorder's callbacks fire long after start() returned; they must see the latest handlers,
  // not the ones that happened to be mounted when the recording began.
  const onTextRef = useRef(onText);
  const onEndRef = useRef(onEnd);
  onTextRef.current = onText;
  onEndRef.current = onEnd;

  const cleanup = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    recorderRef.current = null;
    if (levelTimerRef.current !== null) {
      window.clearInterval(levelTimerRef.current);
      levelTimerRef.current = null;
    }
    const context = contextRef.current;
    contextRef.current = null;
    if (context) void context.close().catch(() => {});
    setLevels(Array(BARS).fill(0));
  }, []);

  const finish = useCallback(() => {
    onEndRef.current?.();
  }, []);

  const start = useCallback(async () => {
    setError("");
    if (recorderRef.current) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("This environment has no microphone access. Use the desktop app or a secure browser context.");
      return;
    }
    try {
      // Raw audio: Whisper was trained on what microphones actually hear, not on what a browser
      // thinks a clean voice looks like. (Perry's pet and OpenWhispr record this way too.)
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      streamRef.current = stream;
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      const chunks: BlobPart[] = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      };
      recorder.onstop = async () => {
        const mimeType = recorder.mimeType || "audio/webm";
        const elapsed = Date.now() - startedAtRef.current;
        const discarded = discardRef.current || elapsed < MIN_RECORDING_MS;
        cleanup();
        if (discarded) {
          setPhase("idle");
          finish();
          return;
        }
        const blob = new Blob(chunks, { type: mimeType });
        if (blob.size === 0) {
          setPhase("idle");
          setError("The recording was empty — check that the right microphone is selected.");
          finish();
          return;
        }
        setPhase("processing");
        const { text, error: transcribeError } = await transcribeAudio(blob);
        setPhase("idle");
        if (transcribeError) {
          setError(transcribeError);
          finish();
          return;
        }
        if (text) onTextRef.current(text);
        finish();
      };
      recorder.onerror = () => {
        cleanup();
        setPhase("idle");
        setError("Recording failed unexpectedly.");
        finish();
      };
      // The waveform: how loud, every LEVEL_MS.
      const context = new AudioContext();
      contextRef.current = context;
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      context.createMediaStreamSource(stream).connect(analyser);
      const wave = new Uint8Array(analyser.fftSize);
      levelTimerRef.current = window.setInterval(() => {
        analyser.getByteTimeDomainData(wave);
        let sum = 0;
        for (const value of wave) sum += ((value - 128) / 128) ** 2;
        const level = Math.min(1, Math.sqrt(sum / wave.length) * 4);
        setLevels((previous) => [...previous.slice(1), level]);
      }, LEVEL_MS);

      discardRef.current = false;
      startedAtRef.current = Date.now();
      recorder.start(250);
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
      finish();
    }
  }, [cleanup, finish]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop(); // onstop handles cleanup + transcription
    } else {
      cleanup();
      setPhase("idle");
      finish();
    }
  }, [cleanup, finish]);

  const cancel = useCallback(() => {
    discardRef.current = true;
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    } else {
      cleanup();
      setPhase("idle");
      finish();
    }
  }, [cleanup, finish]);

  useEffect(() => cleanup, [cleanup]);

  return { phase, error, levels, start, stop, cancel };
}
