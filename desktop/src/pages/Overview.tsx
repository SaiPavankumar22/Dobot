import { useEffect } from "react";
import { ActivityTimeline } from "../components/ActivityTimeline";
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

  return (
    <>
      <h1>Dobot</h1>
      <p className="subtle">An AI that doesn't just answer you. It works for you.</p>

      <div className="cards">
        <div className="card">
          <div className="card__label">Tasks today</div>
          <div className="card__value">{dashboard?.tasks.total ?? 0}</div>
          <div className="subtle">{pending} open · {completed} completed</div>
        </div>
        <div className="card">
          <div className="card__label">Automations</div>
          <div className="card__value">{dashboard?.automations.active ?? 0}</div>
          <div className="subtle">{dashboard?.automations.total ?? 0} total</div>
        </div>
        <div className="card">
          <div className="card__label">Approvals</div>
          <div className="card__value">{dashboard?.approvals.pending ?? 0}</div>
          <div className="subtle">waiting on you</div>
        </div>
        <div className="card">
          <div className="card__label">Memories</div>
          <div className="card__value">{dashboard?.memory.total ?? 0}</div>
          <div className="subtle">{dashboard?.memory.store}</div>
        </div>
      </div>

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

      {(dashboard?.tasks.in_progress.length ?? 0) > 0 && (
        <>
          <h2>In progress</h2>
          <table className="table">
            <tbody>
              {dashboard!.tasks.in_progress.map((task) => (
                <tr key={task.id}>
                  <td style={{ width: "60%" }}>
                    {task.title}
                    <div className="progress">
                      <div className="progress__bar" style={{ width: `${Math.max(task.progress, 5)}%` }} />
                    </div>
                  </td>
                  <td className="mono">{task.id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h2>Recent activity</h2>
      <ActivityTimeline events={events} limit={12} />
    </>
  );
}
