import { useEffect, useMemo, useState } from "react";
import { Record, Stats, Toolbar } from "../components/PageBits";
import { api, ApiError } from "../services/api";
import type { AutomationRecord } from "../types";

const PRESETS = [
  { label: "Every Friday at 6 PM", cron: "0 18 * * 5" },
  { label: "Every morning at 8 AM", cron: "0 8 * * *" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Weekdays at 9 AM", cron: "0 9 * * 1-5" },
];

type Filter = "all" | "active" | "paused";

const FILTERS = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "paused", label: "Paused" },
];

export function Automations() {
  const [automations, setAutomations] = useState<AutomationRecord[]>([]);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("0 18 * * 5");
  const [task, setTask] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  async function load() {
    try {
      setAutomations(await api.automations());
    } catch (loadError) {
      setError(String(loadError));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const stats = useMemo(() => {
    const active = automations.filter((item) => item.status === "active");
    const upcoming = active
      .map((item) => item.next_run_at)
      .filter((value): value is string => Boolean(value))
      .sort()[0];
    return {
      total: automations.length,
      active: active.length,
      paused: automations.length - active.length,
      next: upcoming ? new Date(upcoming).toLocaleString() : "—",
    };
  }, [automations]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return automations.filter((item) => {
      if (filter === "active" && item.status !== "active") return false;
      if (filter === "paused" && item.status === "active") return false;
      if (!needle) return true;
      return `${item.name} ${item.prompt} ${item.schedule}`.toLowerCase().includes(needle);
    });
  }, [automations, query, filter]);

  return (
    <>
      <h1>Automations</h1>
      <p className="subtle">
        Scheduled work runs through the same pipeline as a chat request: same context, same decision
        engine, same approvals, same verification.
      </p>

      <Stats
        items={[
          { label: "Automations", value: stats.total },
          { label: "Active", value: stats.active, tone: stats.active ? "ok" : undefined },
          { label: "Paused", value: stats.paused },
          { label: "Next run", value: stats.next, hint: "earliest scheduled" },
        ]}
      />

      <div className="record" style={{ marginTop: 14 }}>
        <div className="record__head">
          <span className="record__title">New automation</span>
        </div>
        <div className="row">
          <input
            placeholder="Name, e.g. Weekly AI research"
            value={name}
            onChange={(event) => setName(event.target.value)}
            style={{ maxWidth: 260 }}
          />
          <input
            placeholder="Cron (0 18 * * 5)"
            value={schedule}
            onChange={(event) => setSchedule(event.target.value)}
            style={{ maxWidth: 180 }}
            className="mono"
          />
        </div>
        <div className="chip-row">
          {PRESETS.map((preset) => (
            <button key={preset.cron} className="chip" onClick={() => setSchedule(preset.cron)}>
              {preset.label}
            </button>
          ))}
        </div>
        <textarea
          placeholder="What should Dobot do each time? e.g. Research new AI agent releases and summarise them."
          value={task}
          onChange={(event) => setTask(event.target.value)}
          rows={2}
        />
        <div className="row">
          <button
            className="primary"
            disabled={!name.trim() || !task.trim()}
            onClick={async () => {
              setError(null);
              try {
                await api.createAutomation({ name: name.trim(), schedule, task: task.trim() });
                setName("");
                setTask("");
                await load();
              } catch (createError) {
                setError(
                  createError instanceof ApiError ? createError.message : String(createError),
                );
              }
            }}
          >
            Create automation
          </button>
        </div>
      </div>

      {error && <div className="notice notice--danger">{error}</div>}

      <Toolbar
        query={query}
        onQuery={setQuery}
        placeholder="Search name, task or schedule…"
        options={FILTERS}
        active={filter}
        onActive={(id) => setFilter(id as Filter)}
        count={
          filter === "all" && !query
            ? `${automations.length} scheduled`
            : `${visible.length} of ${automations.length}`
        }
      />

      {automations.length === 0 && <div className="notice">Nothing scheduled yet.</div>}
      {automations.length > 0 && visible.length === 0 && (
        <div className="notice">Nothing matches this filter.</div>
      )}

      <div className="record-list">
        {visible.map((automation) => (
          <Record
            key={automation.id}
            title={automation.name}
            tone={automation.status === "active" ? undefined : "warn"}
            status={
              <span
                className={`status status--${automation.status === "active" ? "COMPLETED" : "WAITING_USER"}`}
              >
                {automation.status}
              </span>
            }
            meta={
              <>
                <span>{automation.schedule}</span>
                <span>
                  next:{" "}
                  {automation.next_run_at
                    ? new Date(automation.next_run_at).toLocaleString()
                    : "—"}
                </span>
                <span>ran {automation.run_count}×</span>
              </>
            }
            body={automation.prompt.slice(0, 220)}
            actions={
              <>
                <button onClick={() => void api.runAutomation(automation.id)}>Run now</button>
                <button
                  onClick={() =>
                    void api
                      .setAutomationStatus(
                        automation.id,
                        automation.status === "active" ? "paused" : "active",
                      )
                      .then(load)
                  }
                >
                  {automation.status === "active" ? "Pause" : "Resume"}
                </button>
                <button
                  className="ghost"
                  onClick={() => void api.deleteAutomation(automation.id).then(load)}
                >
                  Delete
                </button>
              </>
            }
          />
        ))}
      </div>
    </>
  );
}
