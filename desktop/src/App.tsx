// One bundle, five windows. The Tauri shell labels each window:
//
//   chat       the default surface — an ordinary chatbot window (sidebar · thread · composer)
//   dot        the always-on floating dot, only shown when the user enables it
//   panel      the compact quick-ask surface anchored to the dot
//   dashboard  tasks · automations · approvals · memory · skills · activity · security · settings
//   overlay    the temporary region-selection layer
//
// In a plain browser (`npm run dev`) the chat window renders by default so the UI is fully usable
// without the native shell.

import { useEffect, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { Confetti } from "./components/Confetti";
import { DobotDot } from "./components/DobotDot";
import { DotMenu } from "./components/DotMenu";
import { ScreenSelector } from "./components/ScreenSelector";
import { Toast } from "./components/Toast";
import { Chat } from "./pages/Chat";
import { Dashboard } from "./pages/Dashboard";
import { UpdateBanner } from "./components/UpdateBanner";
import { native } from "./services/native";
import { resolvePendingSelection } from "./services/screen";
import { loadThread, useDobot } from "./store/dobotStore";
import { applyTheme, loadThemeId } from "./theme";

const DASHBOARD_HASHES = [
  "dashboard",
  "guide",
  "overview",
  "tasks",
  "automations",
  "approvals",
  "memory",
  "identity",
  "skills",
  "activity",
  "security",
  "doctor",
  "settings",
];

function resolveWindow(): string {
  if (!native.isNative) {
    const hash = window.location.hash.replace(/^#\/?/, "");
    if (["dot", "panel", "overlay", "menu"].includes(hash)) return hash;
    if (DASHBOARD_HASHES.includes(hash)) return "dashboard";
    return "chat";
  }
  return native.label();
}

export default function App() {
  const [windowKind, setWindowKind] = useState(resolveWindow);
  const connect = useDobot((state) => state.connect);
  const disconnect = useDobot((state) => state.disconnect);
  const captureScreen = useDobot((state) => state.captureScreen);
  const finishBrowserSelection = useDobot((state) => state.finishBrowserSelection);
  const capturing = useDobot((state) => state.capturing);
  const kill = useDobot((state) => state.kill);

  useEffect(() => {
    // Paint the stored theme before the first frame settles, so a light theme never flashes dark.
    applyTheme(loadThemeId());
    void loadThread();
  }, []);

  useEffect(() => {
    setWindowKind(resolveWindow());
    // In the browser (`npm run dev`) this is how the sidebar switches between chat and dashboard.
    // In the native shell the window label wins, so this is a harmless no-op.
    const onHash = () => setWindowKind(resolveWindow());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    // The dot, its radial menu and the overlay windows are transparent; the panel is a translucent
    // card. Chat paints its own background.
    if (
      windowKind === "dot" ||
      windowKind === "overlay" ||
      windowKind === "panel" ||
      windowKind === "menu"
    ) {
      document.body.classList.add("transparent");
    }
    connect();
    return () => disconnect();
  }, [connect, disconnect, windowKind]);

  useEffect(() => {
    // Native selection events resolve whichever window is waiting for one.
    let unlisten: (() => void) | undefined;
    void native.onSelection((payload) => {
      resolvePendingSelection(payload);
      if (payload.image && !payload.error) {
        // The picture lands in every window's store, so whichever surface opens next — after a
        // snip from the dot's menu, say — already has it attached and ready to ask about.
        useDobot.setState({
          lastError: null,
          selection: {
            image: payload.image,
            region: payload.region,
            application: payload.application,
            windowTitle: payload.window_title,
            source: "native",
          },
        });
        return;
      }
      useDobot.setState({ selection: null });
    }).then((fn) => {
      unlisten = fn ?? undefined;
    });
    // A Look chord: the shell already took the picture, so it lands as a ready selection in this
    // window's store — every window gets it, and the user may be in any of them.
    let unlistenLook: (() => void) | undefined;
    void native.onLook((payload) => {
      if (payload.error || !payload.image) {
        useDobot.setState({
          lastError: payload.error || "Dobot couldn't take a picture of that window.",
        });
        return;
      }
      useDobot.setState({
        lastError: null,
        selection: {
          image: payload.image,
          region: payload.region,
          application: payload.application,
          windowTitle: payload.window_title,
          source: "native",
        },
      });
    }).then((fn) => {
      unlistenLook = fn ?? undefined;
    });
    let unlistenHotkey: (() => void) | undefined;
    void native.onHotkey((action) => {
      // Global shortcuts are emitted to every WebView. The chat surface is the single UI
      // coordinator for actions that mutate shared state; without this guard a capture or kill
      // request is sent once per mounted WebView.
      if (windowKind !== "chat") return;
      if (action === "panel") void native.openPanel();
      if (action === "capture") void captureScreen();
      if (action === "kill") void kill();
      if (action === "dashboard") void native.openDashboard();
    }).then((fn) => {
      unlistenHotkey = fn ?? undefined;
    });
    return () => {
      unlisten?.();
      unlistenLook?.();
      unlistenHotkey?.();
    };
  }, [captureScreen, kill, windowKind]);

  return (
    <>
      {windowKind === "dot" && <DobotDot />}
      {windowKind === "dot" && capturing && (
        <ScreenSelector
          onDone={(selection) => finishBrowserSelection(selection as never)}
          onCancel={() => useDobot.setState({ capturing: false })}
        />
      )}
      {windowKind === "menu" && <DotMenu />}
      {windowKind === "panel" && <ChatPanel />}
      {windowKind === "overlay" && (
        <ScreenSelector
          onDone={(selection) => {
            const payload = selection as { image: string; region: { x: number; y: number; width: number; height: number } };
            resolvePendingSelection(null);
            void native.finishSelection({
              image: payload.image,
              region: payload.region,
              application: "",
              window_title: "",
            });
          }}
          onCancel={() => {
            resolvePendingSelection(null);
            void native.cancelSelection();
          }}
        />
      )}
      {windowKind === "chat" && (
        <>
          <UpdateBanner />
          <Chat />
          <Toast placement="top-right" />
          <Confetti />
          {capturing && (
            <ScreenSelector
              onDone={(selection) => finishBrowserSelection(selection as never)}
              onCancel={() => useDobot.setState({ capturing: false })}
            />
          )}
        </>
      )}
      {windowKind === "dashboard" && (
        <>
          <UpdateBanner />
          <Dashboard />
          <Toast />
          <Confetti />
          {capturing && (
            <ScreenSelector
              onDone={(selection) => finishBrowserSelection(selection as never)}
              onCancel={() => useDobot.setState({ capturing: false })}
            />
          )}
        </>
      )}
    </>
  );
}
