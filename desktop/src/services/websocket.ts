// Event stream client. The dot's state, approvals, notifications and activity all come from here, so
// reconnection has to be automatic and non-destructive: the backend replays recent events on connect.

import { authHeader, getApiToken, getBaseUrl } from "./api";
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
  /** HTTP-fallback state: active only while the WebSocket cannot connect (see startPolling). */
  private pollTimer: number | null = null;
  private pollOk = false;
  private cursor = -1;
  private lastDot = "";

  connect(): void {
    const state = this.socket?.readyState;
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
      return;
    }
    // A socket that was closed mid-handshake (React StrictMode remounts do this) would otherwise be
    // reused forever, because the constructor never leaves CONNECTING. Drop it and start clean.
    this.socket = null;
    this.closedByUs = false;
    // A WebSocket handshake cannot carry an Authorization header, so the token rides in the query:
    // the backend's gate reads `access_token`, and it is also how a private Hugging Face Space
    // authenticates the upgrade if its proxy asks for one.
    let url = getBaseUrl().replace(/^http/, "ws") + "/ws";
    const token = getApiToken();
    if (token) url += `?access_token=${encodeURIComponent(token)}`;
    try {
      this.socket = new WebSocket(url);
    } catch (error) {
      this.scheduleReconnect(String(error));
      return;
    }

    this.socket.onopen = () => {
      this.retry = 0;
      this.stopPolling(); // the real stream is back; HTTP polling was only the fallback
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
      if (this.closedByUs) {
        this.emitStatus(false);
        return;
      }
      // Status is reported by scheduleReconnect: with a healthy HTTP fallback there is nothing
      // "disconnected" to tell the user about.
      this.scheduleReconnect("connection closed");
    };

    this.socket.onerror = () => {
      if (!this.pollOk) this.emitStatus(false, "socket error");
    };
  }

  private scheduleReconnect(detail: string): void {
    // The handshake is being refused (a private Hugging Face Space rejects it — the proxy wants an
    // Authorization header and a browser WebSocket cannot send one). Keep the event stream alive
    // over HTTP meanwhile; the socket keeps retrying underneath and takes over the moment it opens.
    this.startPolling();
    this.retry = Math.min(this.retry + 1, 6);
    const delay = Math.min(1000 * 2 ** (this.retry - 1), 15000);
    if (!this.pollOk) {
      this.emitStatus(false, `${detail}; retrying in ${Math.round(delay / 1000)}s`);
    }
    window.setTimeout(() => this.connect(), delay);
  }

  /** Fallback event stream: poll GET /events over plain HTTP while the WebSocket is blocked. */
  private startPolling(): void {
    if (this.pollTimer !== null) return;
    this.pollTimer = window.setInterval(() => void this.poll(), 2500);
    void this.poll();
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.pollOk = false;
    this.cursor = -1;
    this.lastDot = "";
  }

  private async poll(): Promise<void> {
    try {
      const response = await fetch(`${getBaseUrl()}/events?after=${this.cursor}`, {
        headers: authHeader(),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = (await response.json()) as {
        events?: DobotEvent[];
        dot_status?: string;
        after?: number;
      };
      if (typeof body.after === "number") this.cursor = body.after;
      for (const event of body.events ?? []) this.dispatch(event);
      // The replay can carry a stale status, so the server's current value rides along after it —
      // the same ordering the WebSocket hello uses.
      const dot = body.dot_status ?? "";
      if (dot && dot !== this.lastDot) {
        this.lastDot = dot;
        this.dispatch({
          type: "dot_status",
          task_id: "",
          message: "current status",
          data: { status: dot },
          timestamp: new Date().toISOString(),
          sequence: Number.MAX_SAFE_INTEGER,
        });
      }
      if (!this.pollOk) {
        this.pollOk = true;
        this.emitStatus(true, "live (HTTP polling)");
      }
    } catch {
      if (this.pollOk) {
        this.pollOk = false;
        this.emitStatus(false, "event stream unavailable");
      }
    }
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
    this.stopPolling();
    this.socket?.close();
    this.socket = null;
  }
}

export const socket = new DobotSocket();
