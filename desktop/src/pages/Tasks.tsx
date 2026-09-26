import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { TaskRecord } from "../types";

export function Tasks() {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setTasks(await api.tasks());
    } catch (loadError) {
      setError(String(loadError));
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <>
      <h1>Tasks</h1>
      <p className="subtle">Every task Dobot runs is tracked here, including the ones that keep running after you close the panel.</p>

      <div className="row">
        <input
          placeholder="New task, e.g. Prepare the weekly project report"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          style={{ maxWidth: 460 }}
        />
        <button
          className="primary"
          disabled={!title.trim()}
          onClick={async () => {
            await api.createTask({ title: title.trim() });
            setTitle("");
            await load();
          }}
        >
          Create
        </button>
      </div>

      {error && <div className="notice notice--danger">{error}</div>}

      <h2>All tasks</h2>
      <table className="table">
        <thead>
          <tr>
            <th>Task</th>
            <th>Status</th>
            <th>Source</th>
            <th>Updated</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id}>
              <td>
                <div>{task.title}</div>
                {task.answer && <div className="subtle">{task.answer.slice(0, 140)}</div>}
                {task.steps.length > 0 && (
                  <div className="mono" style={{ color: "var(--text-dim)" }}>
                    {task.steps.filter((step) => step.status === "COMPLETED").length}/{task.steps.length} steps verified
                  </div>
                )}
              </td>
              <td>
                <span className={`status status--${task.status}`}>{task.status}</span>
              </td>
              <td className="mono">{task.source}</td>
              <td className="mono">{task.updated_at ? new Date(task.updated_at).toLocaleString() : "—"}</td>
              <td>
                <div className="row">
                  <button onClick={() => void api.runTask(task.id).then(load)}>Run</button>
                  <button
                    className="danger"
                    onClick={() => void api.cancelTask(task.id).then(load)}
                    disabled={["COMPLETED", "FAILED", "CANCELLED"].includes(task.status)}
                  >
                    Cancel
                  </button>
                  <button className="ghost" onClick={() => void api.deleteTask(task.id).then(load)}>
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {tasks.length === 0 && <div className="notice">No tasks yet.</div>}
    </>
  );
}
