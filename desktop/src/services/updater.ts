// In-app updates: check GitHub Releases (the endpoint tauri.conf.json pins for the Rust
// updater), report what is available, download + install with progress, relaunch.
// Native shell only — in a browser dev build there is no updater plugin, so every entry
// point degrades to null / no-op instead of throwing.
//
// Trust: Rust verifies the minisign signature against the pubkey in tauri.conf.json
// before installing; the private half only ever exists in CI (TAURI_SIGNING_PRIVATE_KEY).

import { relaunch } from "@tauri-apps/plugin-process";
import { check, type Update } from "@tauri-apps/plugin-updater";
import { isNative, native } from "./native";

/** The available update, or null (browser build, offline, or already current). */
export async function checkForUpdates(): Promise<Update | null> {
  if (!isNative) return null;
  return await check();
}

/**
 * Download and install in one pass, reporting a whole-number percent whenever the
 * server reported a content length. The promise resolves only once the install has
 * been handed to the OS — the caller then relaunches.
 */
export async function downloadAndInstall(
  update: Update,
  onProgress: (percent: number | undefined) => void,
  onPreparing?: () => void,
): Promise<void> {
  onPreparing?.();
  const preparation = await native.prepareUpdate();
  if (isNative && preparation && !preparation.released) {
    throw new Error(
      preparation.external_backend
        ? "An external backend is using the local port. Stop that backend and retry the update."
        : "Dobot could not stop its backend safely. Please retry the update.",
    );
  }
  let received = 0;
  let total: number | undefined;
  await update.downloadAndInstall((event) => {
    if (event.event === "Started") {
      total = event.data.contentLength ?? undefined;
      onProgress(total ? 0 : undefined);
    } else if (event.event === "Progress") {
      received += event.data.chunkLength;
      onProgress(
        total ? Math.min(100, Math.round((received / total) * 100)) : undefined,
      );
    } else if (event.event === "Finished") {
      onProgress(100);
    }
  });
}

/** Restart into the freshly installed version. No-op outside the native shell. */
export async function relaunchApp(): Promise<void> {
  if (isNative) await relaunch();
}
