// Shows the plan, each step's risk/verdict/status, and the verification checks — the audit trail of
// what Dobot intended versus what actually happened.

import type { Plan, PlanStep } from "../types";

const STEP_GLYPH: Record<string, string> = {
  PENDING: "○",
  RUNNING: "◐",
  COMPLETED: "✓",
  FAILED: "×",
  SKIPPED: "–",
  BLOCKED: "⛔",
  WAITING_APPROVAL: "!",
};

export function PlanView({ plan }: { plan: Plan }) {
  if (!plan.steps.length) return null;
  return (
    <div className="plan">
      {plan.steps.map((step) => (
        <div key={step.id} className="plan__step">
          <span style={{ width: 16, textAlign: "center" }}>
            {STEP_GLYPH[step.status] ?? "○"}
          </span>
          {step.risk && <span className={`risk risk--${step.risk}`}>{step.risk}</span>}
          <span style={{ flex: 1 }}>
            {step.action.description || step.action.tool}
            {step.verdict === "APPROVAL" && (
              <span style={{ color: "var(--warn)" }}> · approval required</span>
            )}
            {step.verdict === "BLOCK" && <span style={{ color: "var(--danger)" }}> · blocked by policy</span>}
            {step.error && <div style={{ color: "var(--danger)" }}>{step.error}</div>}
            <VerificationLine step={step} />
          </span>
        </div>
      ))}
      {plan.degraded && (
        <div className="mono" style={{ color: "var(--warn)" }}>
          planned offline — Nemotron was unreachable
        </div>
      )}
    </div>
  );
}

function VerificationLine({ step }: { step: PlanStep }) {
  const verification = step.verification;
  if (!verification) return null;
  if (verification.skipped_reason) {
    return (
      <div className="mono" style={{ color: "var(--text-dim)" }}>
        not independently verified: {verification.skipped_reason}
      </div>
    );
  }
  const failed = verification.checks.filter((check) => !check.passed);
  return (
    <div className="mono" style={{ color: failed.length ? "var(--danger)" : "var(--ok)" }}>
      {failed.length
        ? `verification failed: ${failed.map((check) => check.name).join(", ")}`
        : `verified: ${verification.checks.map((check) => check.name).join(", ")}`}
    </div>
  );
}
