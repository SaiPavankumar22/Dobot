import { useEffect, useState } from "react";
import { ActivityTimeline } from "../components/ActivityTimeline";
import { api } from "../services/api";
import { useDobot } from "../store/dobotStore";
import type { ActivityRecord } from "../types";

export function Activity() {
  const events = useDobot((state) => state.events);
  const [records, setRecords] = useState<ActivityRecord[]>([]);
  const [trace, setTrace] = useState<string[]>([]);
  const [view, setView] = useState<"timeline" | "debug">("timeline");

  useEffect(() => {
    void api.activity(200).then(setRecords);
    const timer = window.setInterval(() => {
      void api.activity(200).then(setRecords);
      if (view === "debug") void api.trace().then((payload) => setTrace(payload.lines));
    }, 4000);
    return () => window.clearInterval(timer);
  }, [view]);

  return (
    <>
      <h1>Activity</h1>
      <p className="subtle">
        The persisted event log — the same record the agent produced. Dobot does not summarise away the
        parts that did not work.
      </p>

      <div className="row">
        <button className={view === "timeline" ? "primary" : ""} onClick={() => setView("timeline")}>
          Timeline
        </button>
        <button className={view === "debug" ? "primary" : ""} onClick={() => setView("debug")}>
          Debug trace
        </button>
      </div>

      {view === "timeline" ? (
        <>
          <h2>Persisted</h2>
          <ActivityTimeline records={records} limit={80} />
          <h2>Live (this session)</h2>
          <ActivityTimeline events={events} limit={40} />
        </>
      ) : (
        <>
          <h2>Raw stream</h2>
          <pre className="mono" style={{ background: "var(--bg-elevated)", padding: 12, borderRadius: 8, overflowX: "auto" }}>
            {trace.join("\n") || "waiting for events…"}
          </pre>
        </>
      )}
    </>
  );
}
