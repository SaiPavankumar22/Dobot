// Event stream client. The dot's state, approvals, notifications and activity all come from here, so
// reconnection has to be automatic and non-destructive: the backend replays recent events on connect.

import { getBaseUrl } from "./api";
import type { DobotEvent } from "../types";

type Handler = (event: DobotEvent) => void;
type StatusHandler = (connected: boolean, detail?: string) => void;

export class DobotSocket {
  private socket: WebSocket | null = null;
  private handlers = new Set<Handler>();
  private statusHandlers = new Set<StatusHandler>();
  private retry = 0;
  private closedByUs = false;
  private heartbeat: number | null = null;

  connect(): void {
    const state = this.socket?.readyState;
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
      return;
    }
    // A socket that was closed mid-handshake (React StrictMode remounts do this) would otherwise be
    // reused forever, because the constructor never leaves CONNECTING. Drop it and start clean.
    this.socket = null;
    this.closedByUs = false;
    const url = getBaseUrl().replace(/^http/, "ws") + "/ws";
    try {
      this.socket = new WebSocket(url);
    } catch (error) {
      this.scheduleReconnect(String(error));
      return;
    }

    this.socket.onopen = () => {
      this.retry = 0;
      this.emitStatus(true);
      this.startHeartbeat();
    };

    this.socket.onmessage = (raw) => {
      try {
        const payload = JSON.parse(raw.data);
        if (payload.type === "hello" && Array.isArray(payload.replay)) {
          for (const event of payload.replay) {
            this.dispatch(event as DobotEvent);
          }
          // Applied after the replay: the server's current status beats the last replayed one, which
          // may be from a task that has since finished.
          if (payload.dot_status) {
            this.dispatch({
              type: "dot_status",
              task_id: "",
              message: "current status",
              data: { status: payload.dot_status },
              timestamp: new Date().toISOString(),
              sequence: Number.MAX_SAFE_INTEGER,
            });
          }
          return;
        }
        this.dispatch(payload as DobotEvent);
      } catch {
        /* ignore malformed frames */
      }
    };

    this.socket.onclose = () => {
      this.stopHeartbeat();
      this.emitStatus(false);
      if (!this.closedByUs) {
        this.scheduleReconnect("connection closed");
      }
    };

    this.socket.onerror = () => {
      this.emitStatus(false, "socket error");
    };
  }

  private scheduleReconnect(detail: string): void {
    this.retry = Math.min(this.retry + 1, 6);
    const delay = Math.min(1000 * 2 ** (this.retry - 1), 15000);
    this.emitStatus(false, `${detail}; retrying in ${Math.round(delay / 1000)}s`);
    window.setTimeout(() => this.connect(), delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeat = window.setInterval(() => this.send({ type: "ping" }), 25000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeat !== null) {
      window.clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
  }

  private dispatch(event: DobotEvent): void {
    this.handlers.forEach((handler) => {
      try {
        handler(event);
      } catch (error) {
        console.error("event handler failed", error);
      }
    });
  }

  private emitStatus(connected: boolean, detail?: string): void {
    this.statusHandlers.forEach((handler) => handler(connected, detail));
  }

  onEvent(handler: Handler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  send(payload: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
    }
  }

  close(): void {
    this.closedByUs = true;
    this.stopHeartbeat();
    this.socket?.close();
    this.socket = null;
  }
}

export const socket = new DobotSocket();
