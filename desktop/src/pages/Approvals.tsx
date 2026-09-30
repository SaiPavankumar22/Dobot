import { useEffect, useMemo, useState } from "react";
import { ApprovalCard } from "../components/ApprovalCard";
import { Record, Stats, Toolbar } from "../components/PageBits";
import { api } from "../services/api";
import { useDobot } from "../store/dobotStore";
import type { ApprovalRecord } from "../types";

type Filter = "all" | "approved" | "rejected";

const FILTERS = [
  { id: "all", label: "All decisions" },
  { id: "approved", label: "Approved" },
  { id: "rejected", label: "Rejected" },
];

function decisionStyle(status: ApprovalRecord["status"]): string {
  if (status === "APPROVED") return "COMPLETED";
  if (status === "REJECTED") return "FAILED";
  return "WAITING_USER";
}

export function Approvals() {
  const pending = useDobot((state) => state.approvals);
  const refreshApprovals = useDobot((state) => state.refreshApprovals);
  const [history, setHistory] = useState<ApprovalRecord[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  async function loadHistory() {
    const records = await api.approvals(true);
    setHistory(records.filter((record) => record.status !== "PENDING"));
  }

  useEffect(() => {
    void refreshApprovals();
    void loadHistory();
  }, [refreshApprovals, pending.length]);

  const stats = useMemo(() => {
    const approved = history.filter((record) => record.status === "APPROVED").length;
    const rejected = history.filter((record) => record.status === "REJECTED").length;
    return {
      waiting: pending.length,
      approved,
      rejected,
      other: history.length - approved - rejected,
    };
  }, [history, pending.length]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return history.filter((record) => {
      if (filter === "approved" && record.status !== "APPROVED") return false;
      if (filter === "rejected" && record.status !== "REJECTED") return false;
      if (!needle) return true;
      return `${record.description} ${record.action}`.toLowerCase().includes(needle);
    });
  }, [history, query, filter]);

  return (
    <>
      <h1>Approvals</h1>
      <p className="subtle">
        Sensitive actions stop here. HIGH and CRITICAL actions never run without your decision, and the
        CRITICAL ones cannot be auto-approved at all. When Dobot asks about a folder outside its
        workspace you can allow it <strong>once</strong> or <strong>always</strong> — remembered
        permissions live on the Security page.
      </p>

      <Stats
        items={[
          { label: "Waiting on you", value: stats.waiting, tone: stats.waiting ? "warn" : undefined },
          { label: "Approved", value: stats.approved, tone: stats.approved ? "ok" : undefined },
          { label: "Rejected", value: stats.rejected, tone: stats.rejected ? "danger" : undefined },
          { label: "Edited / expired", value: stats.other },
        ]}
      />

      <h2>Waiting on you</h2>
      {pending.length === 0 ? (
        <div className="notice">Nothing is waiting for approval.</div>
      ) : (
        <div className="record-list">
          {pending.map((approval) => (
            <ApprovalCard key={approval.id} approval={approval} />
          ))}
        </div>
      )}

      <h2>History</h2>
      <Toolbar
        query={query}
        onQuery={setQuery}
        placeholder="Search what was asked…"
        options={FILTERS}
        active={filter}
        onActive={(id) => setFilter(id as Filter)}
        count={
          filter === "all" && !query
            ? `${history.length} decision${history.length === 1 ? "" : "s"}`
            : `${visible.length} of ${history.length}`
        }
      />

      {history.length === 0 && <div className="notice">No decisions recorded yet.</div>}
      {history.length > 0 && visible.length === 0 && (
        <div className="notice">Nothing matches this filter.</div>
      )}

      <div className="record-list">
        {visible.map((record) => (
          <Record
            key={record.id}
            title={record.description || record.action}
            tone={record.status === "REJECTED" ? "danger" : undefined}
            status={
              <span className={`status status--${decisionStyle(record.status)}`}>
                {record.status}
              </span>
            }
            meta={
              <>
                <span className={`risk risk--${record.risk}`}>{record.risk}</span>
                <span>{record.action}</span>
                <span>{new Date(record.created_at).toLocaleString()}</span>
                {record.decision_note && <span>note: {record.decision_note}</span>}
              </>
            }
          />
        ))}
      </div>
    </>
  );
}
