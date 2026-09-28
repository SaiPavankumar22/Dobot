// Bridge to the Tauri shell. Every call degrades to a safe no-op (or a localStorage stand-in) when the
// UI runs in a plain browser, so `npm run dev` is a fully usable development loop.

import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { Region } from "../types";

export const isNative = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export interface DotPosition {
  x: number;
  y: number;
  monitor: number;
  scale?: number;
}

export interface MonitorInfo {
  index: number;
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
  scale: number;
}

export interface SelectionPayload {
  image: string;
  region: Region;
  application: string;
  window_title: string;
  /** Why there is no picture, when the Look hotkey could not take one. Empty when it could. */
  error?: string;
}

/** What the shell tells the panel to do with the microphone: start listening, stop and send. */
export type VoiceSignal = "start" | "stop";

function windowLabel(): string {
  if (!isNative) return "dashboard";
  try {
    return getCurrentWindow().label;
  } catch {
    return "dashboard";
  }
}

export const native = {
  isNative,
  label: windowLabel,

  async invokeSafe<T>(command: string, args?: Record<string, unknown>): Promise<T | null> {
    if (!isNative) return null;
    try {
      return await invoke<T>(command, args);
    } catch (error) {
      console.warn(`native command ${command} failed`, error);
      return null;
    }
  },

  async openChat(): Promise<void> {
    await this.invokeSafe("open_chat");
  },
  async openPanel(): Promise<void> {
    await this.invokeSafe("open_panel");
  },
  /** Show the compact chat dialog if hidden, hide it if shown — the dot click behaviour. */
  async togglePanel(): Promise<void> {
    await this.invokeSafe("toggle_panel");
  },
  async hidePanel(): Promise<void> {
    await this.invokeSafe("hide_panel");
  },
  async openDashboard(route = "overview"): Promise<void> {
    await this.invokeSafe("open_dashboard", { route });
  },

  /** Is the always-on floating dot enabled? Falls back to localStorage in the browser. */
  async dotEnabled(): Promise<boolean> {
    if (!isNative) return localStorage.getItem("dobot.alwaysOn") === "true";
    return (await this.invokeSafe<boolean>("dot_enabled")) ?? false;
  },

  /** Turn the always-on floating dot on or off; resolves with the resulting state. */
  async setDotEnabled(enabled: boolean): Promise<boolean> {
    if (!isNative) {
      localStorage.setItem("dobot.alwaysOn", String(enabled));
      return enabled;
    }
    return (await this.invokeSafe<boolean>("set_dot_enabled", { enabled })) ?? enabled;
  },

  async onDotEnabled(callback: (enabled: boolean) => void): Promise<UnlistenFn | null> {
    if (!isNative) return null;
    try {
      return await listen<boolean>("dobot://dot-enabled", (event) => callback(event.payload));
    } catch (error) {
      console.warn("could not subscribe to dot toggle events", error);
      return null;
    }
  },

  /** Does any Dobot window have the user's attention right now? */
  async dobotFocused(): Promise<boolean> {
    if (!isNative) return false;
    return (await this.invokeSafe<boolean>("dobot_focused")) ?? false;
  },

  async quit(): Promise<void> {
    await this.invokeSafe("quit_dobot");
  },

  async startDragging(): Promise<void> {
    if (!isNative) return;
    try {
      await getCurrentWindow().startDragging();
    } catch (error) {
      console.warn("startDragging unavailable", error);
    }
  },

  async notify(title: string, body: string): Promise<void> {
    if (!isNative) {
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification(title, { body });
      }
      return;
    }
    await this.invokeSafe("notify_user", { title, body });
  },

  async loadDotPosition(): Promise<DotPosition | null> {
    if (!isNative) {
      const raw = localStorage.getItem("dobot.dotPosition");
      return raw ? (JSON.parse(raw) as DotPosition) : null;
    }
    return (await this.invokeSafe<DotPosition>("load_dot_position")) ?? null;
  },

  async saveDotPosition(position: DotPosition): Promise<void> {
    if (!isNative) {
      localStorage.setItem("dobot.dotPosition", JSON.stringify(position));
      return;
    }
    await this.invokeSafe("save_dot_position", { position });
  },

  async monitors(): Promise<MonitorInfo[]> {
    if (!isNative) return [];
    return (await this.invokeSafe<MonitorInfo[]>("list_monitors")) ?? [];
  },

  async captureRegion(region: Region): Promise<string | null> {
    if (!isNative) return null;
    const result = await this.invokeSafe<{ image: string }>("capture_region", { region });
    return result?.image ?? null;
  },

  async openOverlay(): Promise<void> {
    await this.invokeSafe("open_overlay");
  },
  async closeOverlay(): Promise<void> {
    await this.invokeSafe("close_overlay");
  },
  async finishSelection(payload: SelectionPayload): Promise<void> {
    await this.invokeSafe("finish_selection", { payload });
  },
  async cancelSelection(): Promise<void> {
    await this.invokeSafe("cancel_selection");
  },

  async onSelection(callback: (payload: SelectionPayload) => void): Promise<UnlistenFn | null> {
    if (!isNative) return null;
    try {
      return await listen<SelectionPayload>("dobot://selection", (event) => callback(event.payload));
    } catch (error) {
      console.warn("could not subscribe to selection events", error);
      return null;
    }
  },

  /**
   * The Look chord: a picture of the window the user is in, taken by the shell (which knows what
   * is in front) and broadcast to every window — each keeps its own store, and the user may be in
   * any of them.
   */
  async onLook(callback: (payload: SelectionPayload) => void): Promise<UnlistenFn | null> {
    if (!isNative) return null;
    try {
      return await listen<SelectionPayload>("dobot://look", (event) => callback(event.payload));
    } catch (error) {
      console.warn("could not subscribe to look events", error);
      return null;
    }
  },

  /** Take a picture of the window in front right now — the composer button's job. */
  async captureActiveWindow(): Promise<SelectionPayload | null> {
    return await this.invokeSafe<SelectionPayload>("capture_active_window");
  },

  /**
   * Push-to-talk: the shell owns the chord, so it says when to listen and when to stop. The panel
   * is the only window that owns the microphone.
   */
  async onVoice(callback: (signal: VoiceSignal) => void): Promise<UnlistenFn | null> {
    if (!isNative) return null;
    try {
      return await listen<VoiceSignal>("dobot://voice", (event) => callback(event.payload));
    } catch (error) {
      console.warn("could not subscribe to voice events", error);
      return null;
    }
  },

  /** Is the talk chord currently holding a listening session (asked when the panel mounts). */
  async voiceListening(): Promise<boolean> {
    if (!isNative) return false;
    return (await this.invokeSafe<boolean>("voice_state")) ?? false;
  },

  /** The panel has stopped listening — transcribed, cancelled, or the microphone never answered. */
  async voiceDone(): Promise<void> {
    await this.invokeSafe("voice_done");
  },

  async onHotkey(callback: (action: string) => void): Promise<UnlistenFn | null> {
    if (!isNative) return null;
    try {
      return await listen<string>("dobot://hotkey", (event) => callback(event.payload));
    } catch (error) {
      console.warn("could not subscribe to hotkeys", error);
      return null;
    }
  },

  async autostartEnabled(): Promise<boolean> {
    if (!isNative) return false;
    return (await this.invokeSafe<boolean>("autostart_enabled")) ?? false;
  },

  async setAutostart(enabled: boolean): Promise<void> {
    await this.invokeSafe("set_autostart", { enabled });
  },
};
