// Approvals are where trust is earned. The card shows what will happen, at what risk, and with the
// concrete preview the tool computed (file counts, recipients) — never a vague "allow action?".

import { useState } from "react";
import { useDobot } from "../store/dobotStore";
import type { ApprovalRecord } from "../types";

export function ApprovalCard({ approval }: { approval: ApprovalRecord }) {
  const decide = useDobot((state) => state.decide);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [showNote, setShowNote] = useState(false);

  async function resolve(decision: "approve" | "reject") {
    setBusy(true);
    await decide(approval.id, decision, note);
    setBusy(false);
  }

  return (
    <div className="approval">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="approval__title">{approval.description}</div>
        <span className={`risk risk--${approval.risk}`}>{approval.risk}</span>
      </div>
      <div className="mono" style={{ color: "var(--text-dim)" }}>
        action: {approval.action}
      </div>
      {approval.preview.length > 0 && (
        <ul>
          {approval.preview.map((line, index) => (
            <li key={index}>{line}</li>
          ))}
        </ul>
      )}
      {(approval.payload.reasons as string[] | undefined)?.slice(0, 2).map((reason, index) => (
        <div key={index} className="mono" style={{ color: "var(--text-dim)" }}>
          why: {reason}
        </div>
      ))}
      {showNote && (
        <input
          placeholder="Note for the activity log (optional)"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          style={{ marginTop: 8 }}
        />
      )}
      <div className="approval__actions">
        <button className="primary" disabled={busy} onClick={() => resolve("approve")}>
          Approve
        </button>
        <button className="danger" disabled={busy} onClick={() => resolve("reject")}>
          Reject
        </button>
        <button className="ghost" disabled={busy} onClick={() => setShowNote((value) => !value)}>
          {showNote ? "Hide note" : "Add note"}
        </button>
      </div>
    </div>
  );
}
