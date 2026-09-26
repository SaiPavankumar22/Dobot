// The timeline is the raw event log rendered as-is — the same record the agent produced, so the user
// can always see what Dobot decided and did.

import type { ActivityRecord, DobotEvent } from "../types";

const INTERESTING = new Set([
  "task_received",
  "context_built",
  "plan_created",
  "decision",
  "approval_required",
  "approval_resolved",
  "execution_started",
  "tool_completed",
  "verification",
  "memory_written",
  "research_progress",
  "shadow_plan",
  "completed",
  "failed",
  "killed",
  "automation_run",
]);

export function ActivityTimeline({
  records,
  events,
  limit = 60,
}: {
  records?: ActivityRecord[];
  events?: DobotEvent[];
  limit?: number;
}) {
  const rows = records
    ? records
        .filter((record) => INTERESTING.has(record.event_type))
        .slice(-limit)
        .map((record) => ({
          key: record.id,
          time: record.timestamp,
          type: record.event_type,
          message: record.message,
        }))
    : (events ?? [])
        .filter((event) => INTERESTING.has(event.type))
        .slice(-limit)
        .reverse()
        .map((event) => ({
          key: String(event.sequence),
          time: event.timestamp,
          type: event.type,
          message: event.message,
        }));

  if (!rows.length) {
    return <div className="notice">No activity yet. Ask Dobot to do something and it will show up here.</div>;
  }

  return (
    <div className="timeline">
      {rows.map((row) => (
        <div className="timeline__row" key={row.key}>
          <span className="timeline__time">{new Date(row.time).toLocaleTimeString()}</span>
          <span>
            <strong style={{ color: "var(--text-dim)" }}>{row.type}</strong>
            {row.message ? ` — ${row.message}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}
