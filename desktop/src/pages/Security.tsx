import { useEffect, useState } from "react";
import { Record, Stats, Toolbar } from "../components/PageBits";
import { Switch } from "../components/Switch";
import { api } from "../services/api";
import { native } from "../services/native";
import { useDobot } from "../store/dobotStore";
import type { PermissionGrant, PermissionMatrix } from "../types";

interface SecurityStatus {
  sandbox: { provider: string; isolation: string; degraded: boolean; details: { [key: string]: unknown } };
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
  const [grants, setGrants] = useState<PermissionGrant[]>([]);
  const [toolFilter, setToolFilter] = useState("all");

  const refreshGrants = () => {
    void api
      .grants()
      .then((payload) => setGrants(payload.grants ?? []))
      .catch(() => setGrants([]));
  };

  useEffect(() => {
    void api.security().then((payload) => setStatus(payload as unknown as SecurityStatus));
    void api.permissions().then(setMatrix).catch(() => setMatrix(null));
    void native.autostartEnabled().then(setAutostart);
    refreshGrants();
  }, []);

  return (
    <>
      <h1>Security</h1>
      <p className="subtle">
        What is actually enforced right now — including where the isolation is weaker than it could be.
      </p>

      <h2>Sandbox</h2>
      <Stats
        items={[
          {
            label: "Provider",
            value: status?.sandbox.provider ?? "—",
            hint:
              status?.sandbox.provider === "nebius" && status.sandbox.isolation === "vm"
                ? "isolation: vm · shell runs in the Nebius VM, file tools use the local firewall"
                : `isolation: ${status?.sandbox.isolation ?? "—"}`,
            tone: status && status.sandbox.isolation === "none" ? "warn" : undefined,
          },
          {
            label: "Execution runtime",
            value: status?.agent_runtime.name ?? "—",
            hint: status?.agent_runtime.available ? "available" : "unavailable",
            tone: status && !status.agent_runtime.available ? "warn" : undefined,
          },
          {
            label: "Screen capture",
            value: status?.screen_capture ?? "—",
            hint: `continuous monitoring: ${status?.continuous_monitoring ? "ON" : "OFF"}`,
          },
          {
            label: "Kill switch",
            value: status?.kill_switch_hotkey ?? "—",
            hint: `${status?.active_tasks.length ?? 0} active task(s)`,
          },
        ]}
      />

      {status && status.sandbox.isolation === "none" && (
        <div className="notice notice--warn" style={{ marginTop: 12 }}>
          Kernel-level isolation is <strong>not</strong> active: Dobot's action firewall enforces paths,
          hosts and timeouts in-process, but a real sandbox is not running. Two ways to fix that:
          run the NemoClaw / OpenShell stack (<span className="mono">SANDBOX_PROVIDER=nemoclaw</span>{" "}
          plus <span className="mono">OPENSHIELD_GATEWAY_URL</span>), or set{" "}
          <span className="mono">SANDBOX_PROVIDER=nebius</span> with{" "}
          <span className="mono">NEBIUS_API_KEY</span> + <span className="mono">NEBIUS_PROJECT_ID</span>{" "}
          to execute inside a Nebius Sandboxes VM — no local daemon needed.
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

      <h2>Granted permissions</h2>
      <div className="notice">
        <div className="subtle" style={{ marginBottom: 6 }}>
          When Dobot asks to work outside its workspace, <strong>always allow</strong> lands here and
          is remembered per folder; <strong>allow once</strong> never persists — it expires with the
          attempt. Revoking a permission means the next action in that folder asks again. Credentials,
          system folders and destructive commands are never grantable at all.
        </div>
        {grants.length === 0 ? (
          <div className="mono">No remembered permissions.</div>
        ) : (
          grants.map((grant) => (
            <div
              key={grant.id}
              className="row"
              style={{ justifyContent: "space-between", padding: "6px 0", gap: 12 }}
            >
              <span style={{ minWidth: 0 }}>
                <span className={`pill ${grant.kind === "lifetime" ? "pill--auto" : "pill--ask"}`}>
                  {grant.kind === "lifetime" ? "always" : "once"}
                </span>{" "}
                <span className="mono">{grant.roots.join(", ")}</span>
                <div className="subtle" style={{ marginTop: 2 }}>
                  {grant.policy}
                  {grant.expires_at
                    ? ` · expires ${new Date(grant.expires_at).toLocaleString()}`
                    : grant.kind === "lifetime"
                      ? " · remembered until you revoke it"
                      : ""}
                </div>
              </span>
              <button
                className="ghost"
                onClick={() => {
                  void api.revokeGrant(grant.id).then(refreshGrants).catch(() => undefined);
                }}
              >
                Revoke
              </button>
            </div>
          ))
        )}
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
      <Toolbar
        options={[
          { id: "all", label: "All" },
          { id: "asks", label: "Asks first" },
          { id: "refuse", label: "Can refuse" },
        ]}
        active={toolFilter}
        onActive={setToolFilter}
        count={`${(matrix?.tools ?? []).filter((row) =>
          toolFilter === "asks"
            ? !row.automatic
            : toolFilter === "refuse"
              ? row.can_deny
              : true,
        ).length} of ${(matrix?.tools ?? []).length} tools`}
      />
      <div className="record-list">
        {(matrix?.tools ?? [])
          .filter((row) =>
            toolFilter === "asks"
              ? !row.automatic
              : toolFilter === "refuse"
                ? row.can_deny
                : true,
          )
          .map((row) => (
            <Record
              key={row.tool}
              title={<span className="mono">{row.tool}</span>}
              status={
                <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
                  <span className={`risk risk--${row.risk_floor}`}>{row.risk_floor}</span>
                  <span className={`pill ${row.automatic ? "pill--auto" : "pill--ask"}`}>
                    {row.automatic ? "runs" : "asks"}
                  </span>
                  {row.can_deny && <span className="pill pill--deny">can refuse</span>}
                </span>
              }
              meta={row.guards.length ? `guards: ${row.guards.join(", ")}` : "no extra guards"}
            />
          ))}
      </div>

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
      <div className="record-list">
        {(status?.policies ?? []).map((policy) => (
          <Record
            key={policy.name}
            title={<span className="mono">{policy.name}</span>}
            status={
              <span
                className={`pill ${
                  policy.verdict === "BLOCK"
                    ? "pill--deny"
                    : policy.verdict === "APPROVAL"
                      ? "pill--ask"
                      : "pill--auto"
                }`}
              >
                {policy.verdict}
              </span>
            }
            tone={policy.verdict === "BLOCK" ? "danger" : policy.verdict === "APPROVAL" ? "warn" : undefined}
            body={policy.reason}
          />
        ))}
      </div>
    </>
  );
}
