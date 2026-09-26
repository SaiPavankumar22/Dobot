// A quiet toast for things worth noticing without interrupting: an error, or a background task that
// finished while the panel was closed.
//
// Placement matters: the chat window has a composer pinned to the bottom, so a bottom-right toast
// would sit on top of Send. They dock top-right there and bottom-right everywhere else.

import { useEffect, useState } from "react";
import { useDobot } from "../store/dobotStore";

interface ToastState {
  id: number;
  text: string;
  tone: "info" | "warn" | "danger";
}

interface Props {
  placement?: "bottom-right" | "top-right";
}

export function Toast({ placement = "bottom-right" }: Props) {
  const lastError = useDobot((state) => state.lastError);
  const events = useDobot((state) => state.events);
  const [toast, setToast] = useState<ToastState | null>(null);

  useEffect(() => {
    if (lastError) setToast({ id: Date.now(), text: lastError, tone: "danger" });
  }, [lastError]);

  useEffect(() => {
    const latest = events[events.length - 1];
    if (!latest) return;
    if (latest.type === "notification" && latest.data.background) {
      setToast({ id: Date.now(), text: String(latest.data.body ?? latest.message), tone: "info" });
    }
    if (latest.type === "approval_required") {
      setToast({ id: Date.now(), text: latest.message, tone: "warn" });
    }
  }, [events]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 7000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  if (!toast) return null;
  return (
    <div
      className={`notice toast toast--${placement} ${
        toast.tone === "danger" ? "notice--danger" : toast.tone === "warn" ? "notice--warn" : ""
      }`}
      onClick={() => setToast(null)}
      role="status"
    >
      {toast.text}
    </div>
  );
}
