import { useEffect, useState } from "react";
import { Switch } from "../components/Switch";
import { api } from "../services/api";
import { native } from "../services/native";
import { useDobot } from "../store/dobotStore";
import type { PermissionMatrix } from "../types";

interface SecurityStatus {
  sandbox: { provider: string; isolation: string; degraded: boolean; details: Record<string, unknown> };
  agent_runtime: { mode: string; name: string; available: boolean; toolsets: string[]; detail: string };
  screen_capture: string;
  continuous_monitoring: boolean;
  shadow_mode: boolean;
  require_write_approval: boolean;
  require_confirmation_for: string[];
  execution_mode: string;
  protected_paths: string[];
  skill_scan: { mode: string; flagged: string[]; blocked: string[] };
  jev: { enabled: boolean; mode: string };
  kill_switch_hotkey: string;
  active_tasks: string[];
  allowed_paths: string[];
  allowed_networks: string[];
  policies: { name: string; verdict: string; reason: string }[];
}

export function Security() {
  const [status, setStatus] = useState<SecurityStatus | null>(null);
  const shadowMode = useDobot((state) => state.shadowMode);
  const setShadowMode = useDobot((state) => state.setShadowMode);
  const dotEnabled = useDobot((state) => state.dotEnabled);
  const setDotEnabled = useDobot((state) => state.setDotEnabled);
  const kill = useDobot((state) => state.kill);
  const [autostart, setAutostart] = useState(false);

  const [matrix, setMatrix] = useState<PermissionMatrix | null>(null);

  useEffect(() => {
    void api.security().then((payload) => setStatus(payload as unknown as SecurityStatus));
    void api.permissions().then(setMatrix).catch(() => setMatrix(null));
    void native.autostartEnabled().then(setAutostart);
  }, []);

  return (
    <>
      <h1>Security</h1>
      <p className="subtle">
        What is actually enforced right now — including where the isolation is weaker than it could be.
      </p>

      <h2>Sandbox</h2>
      <div className="cards">
        <div className="card">
          <div className="card__label">Provider</div>
          <div className="card__value" style={{ fontSize: 18 }}>{status?.sandbox.provider ?? "—"}</div>
          <div className="subtle">isolation: {status?.sandbox.isolation ?? "—"}</div>
        </div>
        <div className="card">
          <div className="card__label">Execution runtime</div>
          <div className="card__value" style={{ fontSize: 16 }}>{status?.agent_runtime.name ?? "—"}</div>
          <div className="subtle">{status?.agent_runtime.available ? "available" : "unavailable"}</div>
        </div>
        <div className="card">
          <div className="card__label">Screen capture</div>
          <div className="card__value" style={{ fontSize: 16 }}>{status?.screen_capture ?? "—"}</div>
          <div className="subtle">
            continuous monitoring: {status?.continuous_monitoring ? "ON" : "OFF"}
          </div>
        </div>
        <div className="card">
          <div className="card__label">Kill switch</div>
          <div className="card__value" style={{ fontSize: 16 }}>{status?.kill_switch_hotkey ?? "—"}</div>
          <div className="subtle">{status?.active_tasks.length ?? 0} active task(s)</div>
        </div>
      </div>

      {status && status.sandbox.isolation === "none" && (
        <div className="notice notice--warn" style={{ marginTop: 12 }}>
          Kernel-level isolation is <strong>not</strong> active: Dobot's action firewall enforces paths,
          hosts and timeouts in-process, but a real sandbox is not running. To enable it, run the
          NemoClaw / OpenShell stack and set <span className="mono">SANDBOX_PROVIDER=nemoclaw</span> plus{" "}
          <span className="mono">OPENSHIELD_GATEWAY_URL</span>.
        </div>
      )}

      <h2>Human control</h2>
      <div className="notice" style={{ marginBottom: 12 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span>
            <strong>Always-on floating dot</strong>
            <div className="subtle" style={{ marginTop: 2 }}>
              Off by default: Dobot is just a chat window. On: a dot stays above every window with the
              quick-ask panel one click away.
            </div>
          </span>
          <Switch
            checked={dotEnabled}
            onChange={(value) => void setDotEnabled(value)}
            hint="Keep the floating dot on screen"
          />
        </div>
      </div>
      <div className="row">
        <button className="danger" onClick={() => void kill()}>
          STOP DOBOT
        </button>
        <button onClick={() => void setShadowMode(!shadowMode)}>
          {shadowMode ? "Turn shadow mode off" : "Turn shadow mode on"}
        </button>
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={autostart}
            onChange={async (event) => {
              await native.setAutostart(event.target.checked);
              setAutostart(event.target.checked);
            }}
          />
          Start Dobot with Windows
        </label>
      </div>

      <h2>Confirmation</h2>
      <div className={`notice ${status?.require_write_approval ? "notice--warn" : ""}`}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span>
            <strong>Every change to your files needs approval</strong>
            <div className="subtle" style={{ marginTop: 2 }}>
              {status?.require_write_approval
                ? "On (REQUIRE_WRITE_APPROVAL). Moves, writes, deletes, terminal commands and skills all stop for you, even at MEDIUM risk. Reading stays automatic."
                : "Off. Reversible MEDIUM-risk actions such as moving files run without asking, as the specification's default. Set REQUIRE_WRITE_APPROVAL=true in .env to change that."}
            </div>
          </span>
          <span className={`chip ${status?.require_write_approval ? "chip--warn" : ""}`}>
            {status?.require_write_approval ? "ON" : "OFF"}
          </span>
        </div>
        <div className="mono" style={{ marginTop: 6, color: "var(--text-dim)" }}>
          always confirmed: {(status?.require_confirmation_for ?? []).join(", ") || "—"}
        </div>
      </div>

      <h2>File guard</h2>
      <div className="notice">
        <div className="subtle" style={{ marginBottom: 6 }}>
          Independent of the command guards and of the workspace roots: these locations are never read
          or written by a tool, whether or not you approved it. Reading a private key is a credential
          disclosure even though it changes nothing, so the guard covers reads.
        </div>
        <div className="mono">{(status?.protected_paths ?? []).join("  ·  ") || "—"}</div>
      </div>

      <h2>Skill scanner</h2>
      <div className={`notice ${(status?.skill_scan.blocked.length ?? 0) > 0 ? "notice--danger" : ""}`}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span>
            A skill is prose the model obeys, so each one is scanned before it can be offered to the
            planner — prompt injection, hardcoded credentials, exfiltration, dangerous or obfuscated
            commands.
          </span>
          <span className={`pill ${(status?.skill_scan.blocked.length ?? 0) > 0 ? "pill--deny" : "pill--auto"}`}>
            mode: {status?.skill_scan.mode ?? "—"}
          </span>
        </div>
        <div className="mono" style={{ marginTop: 6, color: "var(--text-dim)" }}>
          flagged: {(status?.skill_scan.flagged ?? []).join(", ") || "none"}
        </div>
        <div className="mono" style={{ color: "var(--text-dim)" }}>
          refused: {(status?.skill_scan.blocked ?? []).join(", ") || "none"}
        </div>
      </div>

      <h2>Access policy</h2>
      <p className="subtle">
        What each capability does unattended in mode <strong>{matrix?.mode ?? "—"}</strong>, where the
        approval threshold is <strong>{matrix?.approval_threshold ?? "—"}</strong>. A tool marked
        automatic can still be gated or refused at runtime by a condition — a path outside the
        workspace, a credential argument, an evasion pattern.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Tool</th>
            <th>Risk floor</th>
            <th>Unattended</th>
            <th>Guards</th>
          </tr>
        </thead>
        <tbody>
          {(matrix?.tools ?? []).map((row) => (
            <tr key={row.tool}>
              <td className="matrix__tool">{row.tool}</td>
              <td>
                <span className={`risk risk--${row.risk_floor}`}>{row.risk_floor}</span>
              </td>
              <td>
                <span className={`pill ${row.automatic ? "pill--auto" : "pill--ask"}`}>
                  {row.automatic ? "runs" : "asks"}
                </span>
                {row.can_deny && <span className="pill pill--deny" style={{ marginLeft: 4 }}>can refuse</span>}
              </td>
              <td className="matrix__guards">{row.guards.join(", ") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Boundaries</h2>
      <div className="notice">
        <div className="mono">allowed paths: {(status?.allowed_paths ?? []).join(", ") || "—"}</div>
        <div className="mono">allowed network: {(status?.allowed_networks ?? []).join(", ") || "—"}</div>
        <div className="mono">toolsets: {(status?.agent_runtime.toolsets ?? []).join(", ") || "—"}</div>
        <div className="mono">
          JEV: {status?.jev.enabled ? `enabled (${status.jev.mode})` : "disabled"}
        </div>
      </div>

      <h2>Policies</h2>
      <table className="table">
        <thead>
          <tr>
            <th>Policy</th>
            <th>Verdict</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {(status?.policies ?? []).map((policy) => (
            <tr key={policy.name}>
              <td className="mono">{policy.name}</td>
              <td>
                <span
                  className={`status status--${
                    policy.verdict === "BLOCK" ? "FAILED" : policy.verdict === "APPROVAL" ? "WAITING_USER" : "COMPLETED"
                  }`}
                >
                  {policy.verdict}
                </span>
              </td>
              <td>{policy.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
