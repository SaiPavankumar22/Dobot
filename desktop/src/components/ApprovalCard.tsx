// Approvals are where trust is earned. The card shows what will happen, at what risk, and with the
// concrete preview the tool computed (file counts, recipients) — never a vague "allow action?".
//
// When the step was refused because the target sits outside Dobot's workspace, the card becomes a
// permission question with two honest answers: allow once (this attempt only) or always allow
// (remembered per folder, revocable on the Security page).

import { useState } from "react";
import { useDobot } from "../store/dobotStore";
import type { ApprovalRecord, GrantInfo } from "../types";

export function ApprovalCard({ approval }: { approval: ApprovalRecord }) {
  const decide = useDobot((state) => state.decide);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [showNote, setShowNote] = useState(false);

  const grant = approval.payload?.grant as GrantInfo | undefined;

  async function resolve(decision: "approve" | "reject", scope: "once" | "lifetime" = "once") {
    setBusy(true);
    await decide(approval.id, decision, note, scope);
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
      {grant && (
        <div className="notice notice--warn" style={{ marginTop: 8 }}>
          <strong>This is outside Dobot's usual workspace.</strong>
          <div className="subtle" style={{ marginTop: 2 }}>
            {grant.reason ?? "Target path is outside the allowed workspace roots"}
            {" — "}
            <span className="mono">{grant.roots.join(", ")}</span>
            <br />
            <strong>Allow once</strong> runs this attempt only.{" "}
            <strong>Always allow</strong> remembers this folder so Dobot stops asking — you can
            revoke it any time on the Security page.
          </div>
        </div>
      )}
      {showNote && (
        <input
          placeholder="Note for the activity log (optional)"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          style={{ marginTop: 8 }}
        />
      )}
      <div className="approval__actions">
        {grant ? (
          <>
            <button className="primary" disabled={busy} onClick={() => resolve("approve", "once")}>
              Allow once
            </button>
            <button className="primary" disabled={busy} onClick={() => resolve("approve", "lifetime")}>
              Always allow
            </button>
          </>
        ) : (
          <button className="primary" disabled={busy} onClick={() => resolve("approve")}>
            Approve
          </button>
        )}
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
