// The in-app update notification: checks once when a window starts (native shell only)
// and offers "Update now" / "Later". Progress is shown live; once the install lands the
// banner says so and the app relaunches into the new version.
//
// A failed check never nags — the banner stays away and Settings offers a manual check.
// Mounted inside AuthGate, so it never appears over the sign-in screen.

import type { Update } from "@tauri-apps/plugin-updater";
import { useEffect, useRef, useState } from "react";
import { isNative } from "../services/native";
import { checkForUpdates, downloadAndInstall, relaunchApp } from "../services/updater";

type Phase = "checking" | "available" | "downloading" | "installed" | "hidden";

export function UpdateBanner() {
  const [phase, setPhase] = useState<Phase>("checking");
  const [update, setUpdate] = useState<Update | null>(null);
  const [percent, setPercent] = useState<number | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [relaunching, setRelaunching] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    if (!isNative || started.current) return;
    started.current = true;
    let cancelled = false;
    void (async () => {
      try {
        const found = await checkForUpdates();
        if (cancelled) return;
        if (found) {
          setUpdate(found);
          setPhase("available");
        } else {
          setPhase("hidden");
        }
      } catch {
        // Offline, endpoint unreachable, or a pre-updater build published first —
        // none of those deserve a banner. The next launch checks again.
        if (!cancelled) setPhase("hidden");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (phase === "checking" || phase === "hidden" || !update) return null;

  async function install() {
    if (!update) return;
    setPhase("downloading");
    setError(null);
    setPercent(undefined);
    try {
      await downloadAndInstall(update, (value) => setPercent(value));
      setPhase("installed");
      setRelaunching(true);
      try {
        await relaunchApp();
      } catch {
        // Relaunch refused (an installer may still be finishing): keep the app usable
        // and let the person restart it themselves — never strand them on a dead banner.
        setRelaunching(false);
        setError("Installed. Restart Dobot to finish switching to the new version.");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("available");
    }
  }

  return (
    <div className="update-banner" role="status">
      <div style={{ flex: 1, minWidth: 0 }}>
        {phase === "available" && (
          <>
            <strong>Dobot {update.version} is ready</strong>
            <span className="subtle">
              {"  ·  "}you have {update.currentVersion}
            </span>
          </>
        )}
        {phase === "downloading" && (
          <>
            <strong>
              {percent !== undefined && percent < 100
                ? `Downloading ${update.version}… ${percent}%`
                : `Installing ${update.version}…`}
            </strong>
            <div className="update-banner__track">
              <div
                className="update-banner__bar"
                style={{ width: `${percent !== undefined ? Math.max(percent, 4) : 100}%` }}
              />
            </div>
          </>
        )}
        {phase === "installed" && (
          <strong>{relaunching ? "Installed — restarting…" : `Installed ${update.version}`}</strong>
        )}
        {error && <div className="auth-error">{error}</div>}
      </div>
      {phase === "available" && (
        <>
          <button className="primary" onClick={() => void install()}>
            Update now
          </button>
          <button className="ghost" onClick={() => setPhase("hidden")}>
            Later
          </button>
        </>
      )}
      {phase === "downloading" && (
        <button className="ghost" disabled>
          {percent !== undefined ? `${percent}%` : "…"}
        </button>
      )}
    </div>
  );
}
