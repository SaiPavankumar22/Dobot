import { useEffect } from "react";
import { ActivityTimeline } from "../components/ActivityTimeline";
import { Record, Stats } from "../components/PageBits";
import { useDobot } from "../store/dobotStore";

export function Overview() {
  const dashboard = useDobot((state) => state.dashboard);
  const events = useDobot((state) => state.events);
  const refresh = useDobot((state) => state.refreshDashboard);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const byStatus = dashboard?.tasks.by_status ?? {};
  const completed = byStatus.COMPLETED ?? 0;
  const pending = (byStatus.PENDING ?? 0) + (byStatus.PLANNING ?? 0) + (byStatus.IN_PROGRESS ?? 0);
  const waiting = dashboard?.approvals.pending ?? 0;
  const inProgress = dashboard?.tasks.in_progress ?? [];

  return (
    <>
      <h1>Dobot</h1>
      <p className="subtle">An AI that doesn't just answer you. It works for you.</p>

      <Stats
        items={[
          {
            label: "Tasks today",
            value: dashboard?.tasks.total ?? 0,
            hint: `${pending} open · ${completed} completed`,
          },
          {
            label: "Automations",
            value: dashboard?.automations.active ?? 0,
            hint: `${dashboard?.automations.total ?? 0} total`,
          },
          {
            label: "Approvals",
            value: waiting,
            hint: waiting > 0 ? "waiting on you" : "nothing waiting",
            tone: waiting > 0 ? "warn" : undefined,
          },
          {
            label: "Memories",
            value: dashboard?.memory.total ?? 0,
            hint: dashboard?.memory.store,
          },
        ]}
      />

      <h2>Providers</h2>
      <div className="chip-row">
        {Object.entries(dashboard?.providers ?? {}).map(([name, value]) => (
          <span
            key={name}
            className={`chip ${
              value.includes("connected") || value.includes("available") ? "chip--ok" : "chip--warn"
            }`}
          >
            {name}: {value}
          </span>
        ))}
      </div>

      {inProgress.length > 0 && (
        <>
          <h2>In progress</h2>
          <div className="record-list">
            {inProgress.map((task) => (
              <Record
                key={task.id}
                title={task.title}
                meta={task.id}
                status={
                  <span className={`chip ${task.progress >= 100 ? "chip--ok" : "chip--warn"}`}>
                    {task.progress}%
                  </span>
                }
                body={
                  <div className="progress">
                    <div
                      className="progress__bar"
                      style={{ width: `${Math.max(task.progress, 5)}%` }}
                    />
                  </div>
                }
              />
            ))}
          </div>
        </>
      )}

      <h2>Recent activity</h2>
      <ActivityTimeline events={events} limit={12} />
    </>
  );
}
