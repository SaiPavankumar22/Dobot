// The dot is the product's whole first impression: it must communicate state at a glance.
// Idle ● available · Listening ◉ · Thinking ◌ · Executing ↻ · Approval ! · Halted ×

import { useCallback, useEffect, useRef, useState } from "react";
import { native } from "../services/native";
import { useDobot } from "../store/dobotStore";
import type { DotStatus } from "../types";

const GLYPHS: Record<DotStatus, string> = {
  IDLE: "●",
  LISTENING: "◉",
  THINKING: "◌",
  EXECUTING: "↻",
  APPROVAL_REQUIRED: "!",
  COMPLETED: "✓",
  ERROR: "×",
  KILLED: "×",
};

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
  const approvals = useDobot((state) => state.approvals);
  const connected = useDobot((state) => state.connected);
  const [visibleStatus, setVisibleStatus] = useState<DotStatus>("IDLE");
  // Windows enters the OS drag loop the moment `startDragging()` is called, which swallows the
  // matching `mouseup` — so a click can never be detected from the mouse events alone. The pointer
  // position is tracked instead, and a press that barely moves is treated as a click.
  const pressPoint = useRef<{ x: number; y: number; dragging: boolean } | null>(null);

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
    pressPoint.current = { x: event.screenX, y: event.screenY, dragging: false };
    if (native.isNative) {
      void native.startDragging();
    }
  }, []);

  // A press that ends within a few pixels is a click, not a drag: toggle the chat dialog. The OS
  // drag loop eats the mouseup on Windows, so this fires from both paths harmlessly.
  const onMouseUp = useCallback(
    (event: React.MouseEvent) => {
      const press = pressPoint.current;
      pressPoint.current = null;
      if (!press || press.dragging) return;
      const moved = Math.abs(event.screenX - press.x) + Math.abs(event.screenY - press.y);
      if (moved < 6) {
        void native.togglePanel();
      }
    },
    [],
  );

  const onMouseMove = useCallback((event: React.MouseEvent) => {
    const press = pressPoint.current;
    if (press && Math.abs(event.screenX - press.x) + Math.abs(event.screenY - press.y) >= 6) {
      press.dragging = true;
    }
  }, []);

  return (
    <div
      className="dot-shell"
      onMouseDown={onMouseDown}
      onMouseUp={onMouseUp}
      onMouseMove={onMouseMove}
      title={LABELS[visibleStatus]}
    >
      <div className={`dot ${CLASSES[visibleStatus]}`} role="button" aria-label={LABELS[visibleStatus]}>
        <span className="dot__glyph">{GLYPHS[visibleStatus]}</span>
        {approvals.length > 0 && <span className="dot__badge">{approvals.length}</span>}
        {!connected && <span className="dot__badge" style={{ background: "#6b7280" }}>·</span>}
      </div>
    </div>
  );
}

export function statusLabel(status: DotStatus): string {
  return LABELS[status];
}
