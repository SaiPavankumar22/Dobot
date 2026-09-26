// Screen selection.
//
// Native path: the Rust overlay window covers the screen, the user drags a box, and the pixels are
// captured by the shell (xcap) — the webview never sees the screen.
// Browser path: getDisplayMedia + canvas crop, so the same flow is demonstrable in `npm run dev`.
//
// Nothing here captures continuously: a capture only happens inside an explicit user gesture.

import { native, type SelectionPayload } from "./native";
import type { Region } from "../types";

export interface Selection {
  image?: string;
  region?: Region;
  application: string;
  windowTitle: string;
  source: "native" | "browser" | "none";
  note?: string;
}

let pendingResolver: ((payload: SelectionPayload | null) => void) | null = null;

export function resolvePendingSelection(payload: SelectionPayload | null): void {
  pendingResolver?.(payload);
  pendingResolver = null;
}

/** Ask the user to select a screen region; resolves with the captured PNG. */
export async function captureSelection(timeoutMs = 90_000): Promise<Selection> {
  if (native.isNative) {
    const result = await new Promise<SelectionPayload | null>((resolve) => {
      pendingResolver = resolve;
      window.setTimeout(() => {
        if (pendingResolver) {
          pendingResolver(null);
          pendingResolver = null;
        }
      }, timeoutMs);
      void native.openOverlay();
    });
    await native.closeOverlay();
    if (!result) {
      return { application: "", windowTitle: "", source: "none", note: "selection cancelled" };
    }
    return {
      image: result.image,
      region: result.region,
      application: result.application,
      windowTitle: result.window_title,
      source: "native",
    };
  }

  const browser = await browserCapture();
  if (!browser) {
    return {
      application: "",
      windowTitle: "",
      source: "none",
      note: "screen capture in the browser needs a display-share grant, or run the Tauri shell",
    };
  }
  return browser;
}

/** Capture the whole shared display and crop down to the drag rectangle drawn in the overlay. */
export async function browserCapture(region?: Region): Promise<Selection | null> {
  const mediaDevices = navigator.mediaDevices as MediaDevices | undefined;
  if (!mediaDevices?.getDisplayMedia) return null;
  let stream: MediaStream;
  try {
    stream = await mediaDevices.getDisplayMedia({ video: true, audio: false });
  } catch {
    return null;
  }
  try {
    const track = stream.getVideoTracks()[0];
    const video = document.createElement("video");
    video.srcObject = stream;
    await video.play();
    const width = video.videoWidth;
    const height = video.videoHeight;
    const canvas = document.createElement("canvas");
    const cropping = region && region.width > 1 && region.height > 1;
    canvas.width = cropping ? region!.width : width;
    canvas.height = cropping ? region!.height : height;
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.drawImage(
      video,
      cropping ? region!.x : 0,
      cropping ? region!.y : 0,
      canvas.width,
      canvas.height,
      0,
      0,
      canvas.width,
      canvas.height,
    );
    const dataUrl = canvas.toDataURL("image/png");
    return {
      image: dataUrl.split(",")[1] ?? "",
      region: cropping ? region : { x: 0, y: 0, width, height },
      application: track.label || "browser share",
      windowTitle: document.title,
      source: "browser",
    };
  } finally {
    stream.getTracks().forEach((track) => track.stop());
  }
}

/** A small visual preview of what was captured, shown next to the message in the panel. */
export function previewDataUrl(selection: Selection): string | null {
  if (!selection.image) return null;
  return `data:image/png;base64,${selection.image}`;
}
