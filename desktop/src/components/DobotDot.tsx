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
  // A click on the dot is two quick `mousedown`s, not a `mouseup` pair: Windows enters the OS drag
  // loop the moment `startDragging()` is called (which happens on the press, so the dot can be
  // dragged from anywhere) and swallows the matching `mouseup` — a single click therefore used to be
  // unreliable, and only the odd left+right chord landed. Successive presses always arrive, so the
  // dialog now opens on a double left click and nothing else. Dragging still works: a press that
  // moves past a few pixels asks the shell to drag the window instead.
  const press = useRef<{ x: number; y: number; at: number; dragging: boolean } | null>(null);
  /** Two left presses within this window (and a few pixels apart) open the dialog. */
  const DOUBLE_MS = 420;
  const DOUBLE_PX = 14;

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
    const previous = press.current;
    // The second press of a double click: open (or close) the chat dialog, and do not start a drag
    // from the confirming press.
    if (
      previous &&
      !previous.dragging &&
      now - previous.at <= DOUBLE_MS &&
      Math.abs(event.screenX - previous.x) + Math.abs(event.screenY - previous.y) < DOUBLE_PX
    ) {
      press.current = null;
      void native.togglePanel();
      return;
    }
    press.current = { x: event.screenX, y: event.screenY, at: now, dragging: false };
    if (native.isNative) {
      void native.startDragging();
    }
  }, []);

  // A press that moves marks the press as a drag (best-effort: on Windows these moves are eaten by
  // the OS loop, in which case the coordinates of the next press simply won't match).
  const onMouseMove = useCallback((event: React.MouseEvent) => {
    const current = press.current;
    if (current && !current.dragging && Math.abs(event.screenX - current.x) + Math.abs(event.screenY - current.y) >= 6) {
      current.dragging = true;
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
      onMouseMove={onMouseMove}
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
