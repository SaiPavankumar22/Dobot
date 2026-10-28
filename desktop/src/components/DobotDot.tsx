// The dot is the product's whole first impression: it must communicate state at a glance.
// The app icon is its face; the status is the coloured ring around it and its animation:
// Idle blue · Listening green (pulse) · Thinking purple (spinner) · Executing cyan (pulse)
// Approval amber (pulse) · Done green · Error/Stopped red

import { useCallback, useEffect, useRef, useState } from "react";
import { native } from "../services/native";
import { useDobot } from "../store/dobotStore";
import type { DotStatus } from "../types";

const CLASSES: Record<DotStatus, string> = {
  IDLE: "",
  LISTENING: "dot--listening",
  THINKING: "dot--thinking",
  EXECUTING: "dot--executing",
  APPROVAL_REQUIRED: "dot--approval",
  COMPLETED: "dot--completed",
  ERROR: "dot--error",
  KILLED: "dot--killed",
};

const LABELS: Record<DotStatus, string> = {
  IDLE: "Dobot is ready",
  LISTENING: "Dobot is listening",
  THINKING: "Dobot is thinking",
  EXECUTING: "Dobot is working on your computer",
  APPROVAL_REQUIRED: "Dobot needs your approval",
  COMPLETED: "Done",
  ERROR: "Dobot hit an error",
  KILLED: "Stopped",
};

export function DobotDot() {
  const dotStatus = useDobot((state) => state.dotStatus);
  const dotDetail = useDobot((state) => state.dotDetail);
  const approvals = useDobot((state) => state.approvals);
  const connected = useDobot((state) => state.connected);
  const [visibleStatus, setVisibleStatus] = useState<DotStatus>("IDLE");
  // Three gestures, one window:
  //
  //   single left click   open the radial menu (chat · snip · voice)
  //   double left click   open or close the compact chat dialog straight away
  //   press and move      drag the dot (an open dialog comes with it)
  //
  // A drag is recognised from the shell's `dobot://dot-moved` event, not from the mouse: Windows
  // enters its OS drag loop the moment `startDragging()` runs — on the press, so the dot can be
  // moved from anywhere — and swallows the matching `mouseup`. So what is left of a press is either
  // that shell event (it really moved: a drag) or silence (it didn't: a click, confirmed by the
  // mouse-up when Windows does deliver it, or by CLICK_MS of nothing happening at all).
  const press = useRef<{ x: number; y: number; timer: number; dragging: boolean; acted: boolean } | null>(
    null,
  );
  /** The last press, outliving the menu delay, so even a slow double click still wins over it. */
  const lastPress = useRef<{ x: number; y: number; at: number; dragged: boolean } | null>(null);
  /** Two presses this close together (and this close on screen) mean "open the dialog". */
  const DOUBLE_MS = 460;
  const DOUBLE_PX = 14;
  /** How long a press may sit still before it counts as the menu's single click. */
  const CLICK_MS = 380;

  // Completed/error are transient: show the glyph for a moment, then fall back to idle.
  useEffect(() => {
    setVisibleStatus(dotStatus);
    if (dotStatus === "COMPLETED" || dotStatus === "ERROR" || dotStatus === "KILLED") {
      const timer = window.setTimeout(() => setVisibleStatus("IDLE"), 2600);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [dotStatus]);

  useEffect(() => {
    document.body.classList.add("transparent");
    void native.loadDotPosition().then((position) => {
      if (position && native.isNative) {
        // The shell restores the window itself; this keeps the browser dev view consistent.
        console.debug("restored dot position", position);
      }
    });
  }, []);

  const onMouseDown = useCallback((event: React.MouseEvent) => {
    if (event.button !== 0) return;
    const now = Date.now();
    const last = lastPress.current;
    // The second press of a double click: the dialog — and any menu the first press raised goes
    // down with it (toggle_panel retires the menu in the shell).
    if (
      last &&
      !last.dragged &&
      now - last.at <= DOUBLE_MS &&
      Math.abs(event.screenX - last.x) + Math.abs(event.screenY - last.y) < DOUBLE_PX
    ) {
      if (press.current) window.clearTimeout(press.current.timer);
      press.current = null;
      lastPress.current = null;
      void native.togglePanel();
      return;
    }
    if (press.current) window.clearTimeout(press.current.timer);
    const current = { x: event.screenX, y: event.screenY, timer: 0, dragging: false, acted: false };
    lastPress.current = { x: event.screenX, y: event.screenY, at: now, dragged: false };
    current.timer = window.setTimeout(() => {
      if (press.current !== current || current.dragging || current.acted) return;
      current.acted = true;
      // A press that never moved and never released is still a click — Windows ate the mouse-up.
      void native.toggleMenu();
    }, CLICK_MS);
    press.current = current;
    if (native.isNative) {
      void native.startDragging();
    }
  }, []);

  const onMouseUp = useCallback((event: React.MouseEvent) => {
    if (event.button !== 0) return;
    const current = press.current;
    if (!current) return;
    window.clearTimeout(current.timer);
    // A press-and-release that never moved opens the menu at once — the timer is only the fallback
    // for the mouse-up Windows swallows inside the drag loop. A press that dragged simply ends.
    if (!current.dragging && !current.acted) {
      current.acted = true;
      void native.toggleMenu();
    }
    press.current = null;
  }, []);

  // The shell's word that the dot really moved: this is where a drag becomes known. Mark the
  // press (so it can never also count as a click), drop its pending timer, and retire any menu the
  // icon is being carried away from.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void native
      .onDotMoved(() => {
        const current = press.current;
        if (!current || current.dragging) return;
        current.dragging = true;
        window.clearTimeout(current.timer);
        if (lastPress.current) lastPress.current.dragged = true;
        void native.closeMenu();
      })
      .then((fn) => {
        unlisten = fn ?? undefined;
      });
    return () => unlisten?.();
  }, []);

  // Best-effort supplement: on Windows these moves are usually eaten by the OS loop, in which case
  // the shell event above does the work. Either way, once it moves this press is a drag, not a click.
  const onMouseMove = useCallback((event: React.MouseEvent) => {
    const current = press.current;
    if (current && !current.dragging) {
      if (Math.abs(event.screenX - current.x) + Math.abs(event.screenY - current.y) >= 6) {
        current.dragging = true;
        window.clearTimeout(current.timer);
        if (lastPress.current) lastPress.current.dragged = true;
        void native.closeMenu();
      }
    }
  }, []);

  // The glyph says *that* something is happening; the detail says *what* — and a hovering or
  // screen-reading user gets both, not just a colour.
  const label =
    dotDetail && (visibleStatus === "EXECUTING" || visibleStatus === "THINKING")
      ? `${LABELS[visibleStatus]} — ${dotDetail}`
      : LABELS[visibleStatus];

  return (
    <div
      className="dot-shell"
      onMouseDown={onMouseDown}
      onMouseUp={onMouseUp}
      onMouseMove={onMouseMove}
      onContextMenu={(event) => event.preventDefault()}
      title={label}
    >
      <div className={`dot ${CLASSES[visibleStatus]}`} role="button" aria-label={label}>
        <img className="dot__icon" src="/branding/dobot-icon.png" alt="" draggable={false} />
        {approvals.length > 0 && <span className="dot__badge">{approvals.length}</span>}
        {!connected && <span className="dot__badge" style={{ background: "#6b7280" }}>·</span>}
      </div>
    </div>
  );
}

export function statusLabel(status: DotStatus): string {
  return LABELS[status];
}
