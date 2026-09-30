import { useEffect, useState } from "react";
import { ApprovalCard } from "../components/ApprovalCard";
import { api } from "../services/api";
import { useDobot } from "../store/dobotStore";
import type { ApprovalRecord } from "../types";

export function Approvals() {
  const pending = useDobot((state) => state.approvals);
  const refreshApprovals = useDobot((state) => state.refreshApprovals);
  const [history, setHistory] = useState<ApprovalRecord[]>([]);

  useEffect(() => {
    void refreshApprovals();
    void api.approvals(true).then((records) => setHistory(records.filter((record) => record.status !== "PENDING")));
  }, [refreshApprovals, pending.length]);

  return (
    <>
      <h1>Approvals</h1>
      <p className="subtle">
        Sensitive actions stop here. HIGH and CRITICAL actions never run without your decision, and the
        CRITICAL ones cannot be auto-approved at all. When Dobot asks about a folder outside its
        workspace you can allow it <strong>once</strong> or <strong>always</strong> — remembered
        permissions live on the Security page.
      </p>

      <h2>Waiting on you</h2>
      {pending.length === 0 ? (
        <div className="notice">Nothing is waiting for approval.</div>
      ) : (
        pending.map((approval) => <ApprovalCard key={approval.id} approval={approval} />)
      )}

      <h2>History</h2>
      <table className="table">
        <thead>
          <tr>
            <th>Action</th>
            <th>Risk</th>
            <th>Decision</th>
            <th>When</th>
          </tr>
        </thead>
        <tbody>
          {history.map((approval) => (
            <tr key={approval.id}>
              <td>{approval.description}</td>
              <td>
                <span className={`risk risk--${approval.risk}`}>{approval.risk}</span>
              </td>
              <td>
                <span
                  className={`status status--${
                    approval.status === "APPROVED" ? "COMPLETED" : approval.status === "REJECTED" ? "FAILED" : "WAITING_USER"
                  }`}
                >
                  {approval.status}
                </span>
              </td>
              <td className="mono">{new Date(approval.created_at).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {history.length === 0 && <div className="notice">No decisions recorded yet.</div>}
    </>
  );
}
