import { useEffect, useMemo, useState } from "react";
import { Record, Stats, Toolbar } from "../components/PageBits";
import { api } from "../services/api";
import type { TaskRecord } from "../types";

type Filter = "all" | "active" | "waiting" | "done" | "failed";

const FILTERS = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "waiting", label: "Waiting" },
  { id: "done", label: "Completed" },
  { id: "failed", label: "Failed" },
];

const ACTIVE = new Set(["PENDING", "PLANNING", "IN_PROGRESS"]);
const WAITING = new Set(["WAITING_APPROVAL", "WAITING_USER"]);
const DONE = new Set(["COMPLETED"]);
const FAILED = new Set(["FAILED", "CANCELLED"]);

function bucket(status: TaskRecord["status"]): Filter {
  if (ACTIVE.has(status)) return "active";
  if (WAITING.has(status)) return "waiting";
  if (DONE.has(status)) return "done";
  if (FAILED.has(status)) return "failed";
  return "all";
}

function when(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function Tasks() {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  async function load() {
    try {
      setTasks(await api.tasks());
      setError(null);
    } catch (loadError) {
      setError(String(loadError));
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, []);

  const stats = useMemo(() => {
    let active = 0;
    let waiting = 0;
    let attention = 0;
    for (const task of tasks) {
      const group = bucket(task.status);
      if (group === "active") active += 1;
      else if (group === "waiting") waiting += 1;
      else if (group === "failed") attention += 1;
    }
    return { total: tasks.length, active, waiting, attention };
  }, [tasks]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return tasks.filter((task) => {
      if (filter !== "all" && bucket(task.status) !== filter) return false;
      if (!needle) return true;
      return `${task.title} ${task.answer ?? ""} ${task.source}`.toLowerCase().includes(needle);
    });
  }, [tasks, query, filter]);

  return (
    <>
      <h1>Tasks</h1>
      <p className="subtle">
        Every task Dobot runs is tracked here, including the ones that keep running after you close
        the panel.
      </p>

      <Stats
        items={[
          { label: "Tasks", value: stats.total },
          { label: "Active", value: stats.active, tone: stats.active ? "accent" : undefined },
          { label: "Waiting on you", value: stats.waiting, tone: stats.waiting ? "warn" : undefined },
          { label: "Failed", value: stats.attention, tone: stats.attention ? "danger" : undefined },
        ]}
      />

      <div className="record" style={{ marginTop: 14 }}>
        <div className="record__head">
          <span className="record__title">New task</span>
        </div>
        <div className="row">
          <input
            placeholder="e.g. Prepare the weekly project report"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            style={{ maxWidth: 460 }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && title.trim()) void create();
            }}
          />
          <button className="primary" disabled={!title.trim()} onClick={() => void create()}>
            Create
          </button>
        </div>
      </div>

      {error && <div className="notice notice--danger">{error}</div>}

      <Toolbar
        query={query}
        onQuery={setQuery}
        placeholder="Search title, answer or source…"
        options={FILTERS}
        active={filter}
        onActive={(id) => setFilter(id as Filter)}
        count={filter === "all" && !query ? `${tasks.length} task${tasks.length === 1 ? "" : "s"}` : `${visible.length} of ${tasks.length}`}
      />

      {tasks.length === 0 && <div className="notice">No tasks yet.</div>}
      {tasks.length > 0 && visible.length === 0 && (
        <div className="notice">Nothing matches this filter.</div>
      )}

      <div className="record-list">
        {visible.map((task) => {
          const group = bucket(task.status);
          const stepsDone = task.steps.filter((step) => step.status === "COMPLETED").length;
          const finished = ["COMPLETED", "FAILED", "CANCELLED"].includes(task.status);
          return (
            <Record
              key={task.id}
              title={task.title || task.id}
              tone={group === "failed" ? "danger" : group === "waiting" ? "warn" : undefined}
              status={<span className={`status status--${task.status}`}>{task.status}</span>}
              meta={
                <>
                  <span>{task.source}</span>
                  <span>{when(task.updated_at)}</span>
                  {task.steps.length > 0 && (
                    <span>
                      {stepsDone}/{task.steps.length} steps verified
                    </span>
                  )}
                </>
              }
              body={task.answer ? task.answer.slice(0, 220) : undefined}
              actions={
                <>
                  <button onClick={() => void api.runTask(task.id).then(load)}>Run</button>
                  <button
                    className="danger"
                    disabled={finished}
                    onClick={() => void api.cancelTask(task.id).then(load)}
                  >
                    Cancel
                  </button>
                  <button className="ghost" onClick={() => void api.deleteTask(task.id).then(load)}>
                    Delete
                  </button>
                </>
              }
            />
          );
        })}
      </div>
    </>
  );

  async function create() {
    await api.createTask({ title: title.trim() });
    setTitle("");
    await load();
  }
}
