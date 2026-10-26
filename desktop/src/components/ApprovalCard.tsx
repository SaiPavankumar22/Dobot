// Approvals are where trust is earned. The card shows what will happen, at what risk, and with the
// concrete preview the tool computed (file counts, recipients) — never a vague "allow action?".
//
// When the step was refused because the target sits outside Dobot's workspace, the card becomes a
// permission question with two honest answers: allow once (this attempt only) or always allow
// (remembered per folder, revocable on the Security page).
//
// Editable fields: the user can change path/command/URL before approving so a slightly wrong plan
// can be corrected on the spot rather than rejected and restarted.

import { useState } from "react";
import { useDobot } from "../store/dobotStore";
import type { ApprovalRecord, GrantInfo } from "../types";

/** Param keys that are safe to expose as editable text inputs. */
const EDITABLE_KEYS = ["path", "command", "url", "query", "name", "target", "message"];

function getEditableParams(params: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of EDITABLE_KEYS) {
    if (key in params && params[key] !== undefined && params[key] !== null) {
      out[key] = String(params[key]);
    }
  }
  return out;
}

export function ApprovalCard({ approval }: { approval: ApprovalRecord }) {
  const decide = useDobot((state) => state.decide);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [showNote, setShowNote] = useState(false);
  const [showEdit, setShowEdit] = useState(false);

  const params = (approval.payload?.params ?? {}) as Record<string, unknown>;
  const [edits, setEdits] = useState<Record<string, string>>(() => getEditableParams(params));

  const grant = approval.payload?.grant as GrantInfo | undefined;
  const hasEditableFields = Object.keys(getEditableParams(params)).length > 0;

  const dirtyEdits = Object.fromEntries(
    Object.entries(edits).filter(([key, value]) => value !== String(params[key] ?? "")),
  );
  const hasEdits = Object.keys(dirtyEdits).length > 0;

  async function resolve(decision: "approve" | "reject", scope: "once" | "lifetime" = "once") {
    setBusy(true);
    if (decision === "approve" && hasEdits) {
      await decide(approval.id, "edit", note, dirtyEdits, scope);
    } else {
      await decide(approval.id, decision, note, undefined, scope);
    }
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

      {/* Editable params panel — only shown when the tool has recognisable fields */}
      {showEdit && hasEditableFields && (
        <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
          {Object.keys(edits).map((key) => (
            <label key={key} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span className="mono" style={{ color: "var(--text-dim)", fontSize: "0.8em" }}>{key}</span>
              <input
                value={edits[key]}
                onChange={(e) => setEdits((prev) => ({ ...prev, [key]: e.target.value }))}
                style={{ fontFamily: "monospace" }}
              />
            </label>
          ))}
          {hasEdits && (
            <div className="mono" style={{ color: "var(--text-dim)", fontSize: "0.8em" }}>
              Approving will apply your edits above.
            </div>
          )}
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
              {hasEdits ? "Apply & Allow once" : "Allow once"}
            </button>
            <button className="primary" disabled={busy} onClick={() => resolve("approve", "lifetime")}>
              {hasEdits ? "Apply & Always allow" : "Always allow"}
            </button>
          </>
        ) : (
          <button className="primary" disabled={busy} onClick={() => resolve("approve")}>
            {hasEdits ? "Apply edits & Approve" : "Approve"}
          </button>
        )}
        <button className="danger" disabled={busy} onClick={() => resolve("reject")}>
          Reject
        </button>
        {hasEditableFields && (
          <button className="ghost" disabled={busy} onClick={() => setShowEdit((v) => !v)}>
            {showEdit ? "Hide edit" : "Edit params"}
          </button>
        )}
        <button className="ghost" disabled={busy} onClick={() => setShowNote((value) => !value)}>
          {showNote ? "Hide note" : "Add note"}
        </button>
      </div>
    </div>
  );
}
