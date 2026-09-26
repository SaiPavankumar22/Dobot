import { useEffect, useState } from "react";
import { api, ApiError } from "../services/api";
import type { AutomationRecord } from "../types";

const PRESETS = [
  { label: "Every Friday at 6 PM", cron: "0 18 * * 5" },
  { label: "Every morning at 8 AM", cron: "0 8 * * *" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Weekdays at 9 AM", cron: "0 9 * * 1-5" },
];

export function Automations() {
  const [automations, setAutomations] = useState<AutomationRecord[]>([]);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("0 18 * * 5");
  const [task, setTask] = useState("");
  const [error, setError] = useState<string | null>(null);

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

  return (
    <>
      <h1>Automations</h1>
      <p className="subtle">
        Scheduled work runs through the same pipeline as a chat request: same context, same decision
        engine, same approvals, same verification.
      </p>

      <div className="card">
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
        <div className="chip-row" style={{ marginTop: 8 }}>
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
          style={{ marginTop: 8 }}
        />
        <div className="row" style={{ marginTop: 8 }}>
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
                setError(createError instanceof ApiError ? createError.message : String(createError));
              }
            }}
          >
            Create automation
          </button>
        </div>
      </div>

      {error && <div className="notice notice--danger">{error}</div>}

      <h2>Configured</h2>
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Schedule</th>
            <th>Next run</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {automations.map((automation) => (
            <tr key={automation.id}>
              <td>
                <div>{automation.name}</div>
                <div className="subtle">{automation.prompt.slice(0, 120)}</div>
              </td>
              <td className="mono">{automation.schedule}</td>
              <td className="mono">
                {automation.next_run_at ? new Date(automation.next_run_at).toLocaleString() : "—"}
              </td>
              <td>
                <span className={`status status--${automation.status === "active" ? "COMPLETED" : "WAITING_USER"}`}>
                  {automation.status}
                </span>
              </td>
              <td>
                <div className="row">
                  <button onClick={() => void api.runAutomation(automation.id)}>Run now</button>
                  <button
                    onClick={() =>
                      void api
                        .setAutomationStatus(automation.id, automation.status === "active" ? "paused" : "active")
                        .then(load)
                    }
                  >
                    {automation.status === "active" ? "Pause" : "Resume"}
                  </button>
                  <button className="ghost" onClick={() => void api.deleteAutomation(automation.id).then(load)}>
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {automations.length === 0 && <div className="notice">Nothing scheduled yet.</div>}
    </>
  );
}
