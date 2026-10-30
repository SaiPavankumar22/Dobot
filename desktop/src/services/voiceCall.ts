// The voice call client: duplex audio over one WebSocket, playback that can be cut off mid-word.
//
// Everything the panel needs to *be* a phone call with Dobot:
//
//   * the microphone streams PCM frames as they are captured (the backend's VAD decides what is
//     speech — the client never has to guess, so there is one answer to "when does a turn end");
//   * assistant audio arrives as base64 samples and is scheduled on a gapless playback queue, so
//     sentences spoken back-to-back never click or overlap;
//   * barge-in works from both ends: the backend cancels generation when it hears the user, and
//     the moment it says so (`call_interrupted`, or any phase leaving "speaking") every queued
//     sample is dropped — the ear must not keep hearing a reply that has been retracted.
//
// The microphone runs with echo cancellation on for this surface alone (the composer's recorder
// deliberately runs raw): during a call the speaker is saying words into the same room the mic is
// listening to, and without AEC Dobot would hear itself and interrupt itself forever.

import { getBaseUrl, getBearerToken } from "./api";

export type CallPhase = "connecting" | "loading" | "listening" | "thinking" | "speaking" | "ended";

export interface CallStatus {
  vad: { detector: string; sample_rate: number };
  stt: { state: string | null; model: string | null };
  tts: { provider: string; available: boolean; model: string; voice: string; rate: number };
  llm: { model: string; configured: boolean };
  hints?: string[];
}

export interface CallCallbacks {
  onPhase: (phase: CallPhase) => void;
  onStatus: (status: CallStatus) => void;
  onUserText: (text: string) => void;
  onAssistantDelta: (delta: string) => void;
  onError: (message: string) => void;
  onEnd: (reason: string) => void;
}

function encodePcm(samples: Int16Array): string {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = "";
  const SLICE = 0x8000;
  for (let index = 0; index < bytes.length; index += SLICE) {
    const piece = bytes.subarray(index, index + SLICE);
    binary += String.fromCharCode(...piece);
  }
  return btoa(binary);
}

function decodePcm(base64: string): DataView {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new DataView(bytes.buffer);
}

export class VoiceCall {
  private ws: WebSocket | null = null;
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private processor: ScriptProcessorNode | null = null;
  /** Playback volume — the only thing between "speaking" and the speakers. */
  private voice: GainNode | null = null;
  private sources: AudioBufferSourceNode[] = [];
  /** When the next buffered sample may start: the queue that makes sentences gapless. */
  private nextStart = 0;
  private started = false;
  private stopped = false;
  private muted = false;
  private phase: CallPhase = "connecting";

  constructor(private callbacks: CallCallbacks) {}

  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;

    let url = getBaseUrl().replace(/^http/, "ws") + "/voice/call";
    const token = getBearerToken();
    if (token) url += `?access_token=${encodeURIComponent(token)}`;
    try {
      this.ws = new WebSocket(url);
    } catch (error) {
      this.callbacks.onError(`The call could not connect: ${String(error)}`);
      this.finish("Connection failed");
      return;
    }
    this.ws.onmessage = (raw) => {
      try {
        this.dispatch(JSON.parse(raw.data));
      } catch {
        /* one malformed frame must not end the call */
      }
    };
    this.ws.onerror = () => {
      if (!this.stopped) this.callbacks.onError("The call connection dropped.");
    };
    this.ws.onclose = () => {
      if (!this.stopped) this.finish("Connection lost");
    };

    try {
      this.context = new AudioContext();
      // WebView2 may create a suspended context until the originating click is acknowledged.
      await this.context.resume();
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      const source = this.context.createMediaStreamSource(this.stream);
      this.processor = this.context.createScriptProcessor(4096, 1, 1);
      this.processor.onaudioprocess = (event) => this.onCapture(event.inputBuffer.getChannelData(0));
      // A ScriptProcessor only runs while it is connected; routing it through a muted gain keeps
      // the graph alive without the microphone being played straight back into the speakers.
      const sink = this.context.createGain();
      sink.gain.value = 0;
      source.connect(this.processor);
      this.processor.connect(sink);
      sink.connect(this.context.destination);
      this.voice = this.context.createGain();
      this.voice.connect(this.context.destination);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.callbacks.onError(
        message.toLowerCase().includes("permission")
          ? "Microphone permission was denied — allow it in your system settings to take the call."
          : `The microphone is unavailable: ${message}`,
      );
      this.finish("Microphone unavailable");
    }
  }

  private onCapture(samples: Float32Array): void {
    const context = this.context;
    // Do not feed speaker output back into VAD. Audio is scheduled ahead of real time, so the
    // queue tail is a more reliable guard than the server's state transition.
    if (
      this.stopped ||
      this.muted ||
      this.phase === "speaking" ||
      (context && this.nextStart > context.currentTime + 0.03) ||
      !this.ws ||
      this.ws.readyState !== WebSocket.OPEN
    ) return;
    const pcm = new Int16Array(samples.length);
    for (let index = 0; index < samples.length; index += 1) {
      const value = Math.max(-1, Math.min(1, samples[index]));
      pcm[index] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    // The rate rides with every frame: the backend resamples to its own 16 kHz whatever the
    // device's clock happened to be, so nothing here depends on a particular sample rate.
    this.ws.send(
      JSON.stringify({
        type: "audio",
        rate: this.context?.sampleRate ?? 48000,
        data: encodePcm(pcm),
      }),
    );
  }

  private dispatch(message: Record<string, unknown>): void {
    switch (message.type) {
      case "call_status":
        this.callbacks.onStatus(message as unknown as CallStatus);
        break;
      case "call_state": {
        const state = String(message.state);
        this.phase = state as CallPhase;
        // A normal listening transition does not retract already scheduled audio. Only the
        // explicit interruption event is allowed to flush the playback queue.
        this.callbacks.onPhase(this.phase);
        break;
      }
      case "call_interrupted":
        this.stopPlayback();
        this.callbacks.onPhase("listening");
        break;
      case "call_transcript":
        if (message.role === "user") this.callbacks.onUserText(String(message.text ?? ""));
        break;
      case "call_text":
        if (message.delta) this.callbacks.onAssistantDelta(String(message.delta));
        break;
      case "call_audio":
        this.play(Number(message.rate) || 24000, decodePcm(String(message.data ?? "")));
        break;
      case "call_error":
        this.callbacks.onError(String(message.message ?? "The call hit an error."));
        break;
      default:
        break;
    }
  }

  /** Append one PCM16 stretch to the gapless playback queue. */
  private play(rate: number, view: DataView): void {
    const context = this.context;
    if (!context || this.stopped || this.phase === "ended" || view.byteLength < 2) return;
    const frames = Math.floor(view.byteLength / 2);
    const buffer = context.createBuffer(1, frames, rate);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < frames; index += 1) {
      channel[index] = view.getInt16(index * 2, true) / 0x8000;
    }
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.voice ?? context.destination);
    // Schedule against the queue's tail (with a small lead) so consecutive sentences butt up
    // against each other instead of each paying a fresh scheduling gap.
    const start = Math.max(context.currentTime + 0.02, this.nextStart);
    source.start(start);
    this.nextStart = start + buffer.duration;
    source.onended = () => {
      this.sources = this.sources.filter((item) => item !== source);
    };
    this.sources.push(source);
  }

  /** Drop every queued and playing sample — the playback half of barge-in. */
  stopPlayback(): void {
    for (const source of this.sources.splice(0)) {
      try {
        source.onended = null;
        source.stop();
      } catch {
        /* already stopped */
      }
    }
    this.nextStart = 0;
  }

  /** The panel's own stop button: cut the reply short without saying a word. */
  interrupt(): void {
    if (this.stopped || this.ws?.readyState !== WebSocket.OPEN) return;
    this.stopPlayback();
    this.ws.send(JSON.stringify({ type: "cancel" }));
    this.phase = "listening";
    this.callbacks.onPhase("listening");
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
  }

  setVolume(volume: number): void {
    if (this.voice) this.voice.gain.value = Math.max(0, Math.min(1, volume));
  }

  end(reason = "Call ended"): void {
    if (this.stopped) return;
    if (this.ws?.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify({ type: "end" }));
      } catch {
        /* closing anyway */
      }
    }
    this.finish(reason);
  }

  private finish(reason: string): void {
    if (this.stopped) return;
    this.stopped = true;
    this.stopPlayback();
    try {
      this.ws?.close();
    } catch {
      /* already closed */
    }
    this.ws = null;
    this.processor?.disconnect();
    this.processor = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    void this.context?.close().catch(() => undefined);
    this.context = null;
    this.callbacks.onPhase("ended");
    this.callbacks.onEnd(reason);
  }
}
