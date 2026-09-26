// Doctor — the honest state of every capability, and what to type to fix each one.
//
// The page exists because "degraded" is the normal condition of a system like this, and the difference
// between a feature that is absent and one that is broken is invisible from the outside. So each row
// spells out three things: what state it is really in, how the Doctor knows, and the exact command that
// changes it. A capability that is configured but not applied is the dangerous one, and it is called
// out as `stale` rather than quietly shown as working.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../services/api";
import type { Capability, DoctorReport, UsageSummary } from "../types";

const STATE_ORDER: Capability["state"][] = ["broken", "stale", "not_configured", "declined", "live"];

const STATE_LABEL: Record<Capability["state"], string> = {
  broken: "broken",
  stale: "stale",
  not_configured: "not set up",
  declined: "off by choice",
  live: "live",
};

const STATE_CHIP: Record<Capability["state"], string> = {
  broken: "chip--danger",
  stale: "chip--warn",
  not_configured: "chip--muted",
  declined: "chip--muted",
  live: "chip--ok",
};

export function Doctor() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [nextReport, nextUsage] = await Promise.all([api.doctor(), api.usage()]);
      setReport(nextReport);
      setUsage(nextUsage);
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function consolidate(dryRun: boolean) {
    try {
      const result = await api.consolidate(dryRun);
      setNote(
        `${dryRun ? "Would change" : "Changed"}: merged ${result.merged ?? 0}, forgot ${
          result.forgotten ?? 0
        }, promoted ${result.promoted ?? 0}, pruned ${result.journals_pruned ?? 0} journal(s).`,
      );
      if (!dryRun) await load();
    } catch (caught) {
      setNote(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  const capabilities = report
    ? [...report.capabilities].sort(
        (a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state),
      )
    : [];
  const visible = showAll ? capabilities : capabilities.filter((item) => item.state !== "live");

  return (
    <div className="stack">
      <section className="card">
        <div className="row row--between">
          <div>
            <h2 style={{ margin: 0 }}>Doctor</h2>
            <p className="dim" style={{ margin: "2px 0 0" }}>
              What is actually wired up right now — read from the running system, not from your config
              file.
            </p>
          </div>
          <button onClick={() => void load()} disabled={busy}>
            {busy ? "Probing…" : "Re-check"}
          </button>
        </div>

        {error && <p className="error">{error}</p>}

        {report && (
          <>
            <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
              <span className={`chip ${report.overall === "healthy" ? "chip--ok" : "chip--warn"}`}>
                {report.overall}
              </span>
              {STATE_ORDER.map((state) =>
                report.counts[state] ? (
                  <span key={state} className={`chip ${STATE_CHIP[state]}`}>
                    {report.counts[state]} {STATE_LABEL[state]}
                  </span>
                ) : null,
              )}
              <span className="dim" style={{ fontSize: 12, marginLeft: "auto" }}>
                {report.environment.platform} · Python {report.environment.python}
              </span>
            </div>
            {report.overall === "degraded" && (
              <p className="dim" style={{ marginBottom: 0 }}>
                Degraded is not a failure. It means one or more optional capabilities are absent, and
                Dobot is telling you which — instead of pretending the request succeeded.
              </p>
            )}
          </>
        )}
      </section>

      {report && report.fixes.length > 0 && (
        <section className="card">
          <h3 style={{ marginTop: 0 }}>To fix, exactly</h3>
          <table className="table">
            <thead>
              <tr>
                <th>Capability</th>
                <th>State</th>
                <th>Do this</th>
              </tr>
            </thead>
            <tbody>
              {report.fixes.map((fix) => (
                <tr key={fix.id}>
                  <td>{fix.label}</td>
                  <td>
                    <span className={`chip ${STATE_CHIP[fix.state as Capability["state"]] ?? "chip--muted"}`}>
                      {fix.state}
                    </span>
                  </td>
                  <td>
                    {fix.fix}
                    {fix.env && <code className="code-inline"> {fix.env}</code>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <div className="row row--between">
          <h3 style={{ margin: 0 }}>Capabilities</h3>
          <button onClick={() => setShowAll((value) => !value)}>
            {showAll ? "Hide working ones" : "Show everything"}
          </button>
        </div>
        <div className="cap-list">
          {visible.map((capability) => (
            <div key={capability.id} className="cap">
              <div className="cap__head">
                <span className={`chip ${STATE_CHIP[capability.state]}`}>
                  {STATE_LABEL[capability.state]}
                </span>
                <strong>{capability.label}</strong>
                {!capability.optional && <span className="chip chip--muted">required</span>}
              </div>
              <p className="cap__detail">{capability.detail}</p>
              {capability.fix && capability.state !== "live" && (
                <p className="cap__fix">
                  <span className="dim">fix · </span>
                  {capability.fix}
                  {capability.env && <code className="code-inline"> {capability.env}</code>}
                </p>
              )}
            </div>
          ))}
          {visible.length === 0 && (
            <p className="dim">Everything is live. There is nothing here to fix.</p>
          )}
        </div>
      </section>

      <section className="card">
        <div className="row row--between">
          <div>
            <h3 style={{ margin: 0 }}>Memory maintenance</h3>
            <p className="dim" style={{ margin: "2px 0 0" }}>
              Decay, merge duplicates, promote repeated facts into the canonical ledger, and bound the
              run journals. Dry run first if you want to see the numbers before they stick.
            </p>
          </div>
          <div className="row">
            <button onClick={() => void consolidate(true)}>Dry run</button>
            <button onClick={() => void consolidate(false)}>Run now</button>
          </div>
        </div>
        {note && <p className="dim" style={{ marginBottom: 0 }}>{note}</p>}
      </section>

      {usage && (
        <section className="card">
          <div className="row row--between">
            <h3 style={{ margin: 0 }}>This session</h3>
            <button
              onClick={async () => {
                await api.resetUsage();
                await load();
              }}
            >
              Reset counters
            </button>
          </div>
          <div className="stat-grid">
            <Stat label="Model calls" value={usage.model.calls} />
            <Stat label="Tokens in" value={usage.model.prompt_tokens.toLocaleString()} />
            <Stat label="Tokens out" value={usage.model.completion_tokens.toLocaleString()} />
            <Stat
              label="Estimated cost"
              value={usage.pricing_configured ? `$${usage.model.estimated_cost_usd.toFixed(4)}` : "unknown"}
              hint={
                usage.pricing_configured
                  ? undefined
                  : "Set PRICE_PER_MTOK_INPUT/OUTPUT. Without a price Dobot reports unknown rather than guessing."
              }
            />
            <Stat label="Degraded calls" value={usage.model.degraded_calls} />
            <Stat label="Avg latency" value={`${usage.model.avg_latency_ms} ms`} />
          </div>

          <h4 style={{ marginBottom: 4 }}>Compression before the model saw it</h4>
          {usage.tokenjuice.calls === 0 ? (
            <p className="dim" style={{ marginTop: 0 }}>
              Nothing compressed yet this session — the measurement starts with your first request.
            </p>
          ) : (
            <>
              <div className="stat-grid">
                <Stat label="Blobs processed" value={usage.tokenjuice.calls} />
                <Stat label="Characters saved" value={usage.tokenjuice.saved_chars.toLocaleString()} />
                <Stat label="Tokens saved" value={`~${usage.tokenjuice.saved_tokens.toLocaleString()}`} />
                <Stat label="Reduction" value={`${usage.tokenjuice.saved_pct}%`} />
              </div>
              <table className="table">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Blobs</th>
                    <th>Characters saved</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(usage.tokenjuice.by_label).map(([label, totals]) => (
                    <tr key={label}>
                      <td>{label}</td>
                      <td>{totals.calls}</td>
                      <td>{totals.saved_chars.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {usage.recent_calls.length > 0 && (
            <>
              <h4 style={{ marginBottom: 4 }}>Recent calls</h4>
              <table className="table">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Purpose</th>
                    <th>Tokens</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.recent_calls.slice(0, 8).map((call, index) => (
                    <tr key={`${call.at}-${index}`}>
                      <td>
                        {call.model}
                        {call.degraded && <span className="chip chip--warn" style={{ marginLeft: 6 }}>degraded</span>}
                      </td>
                      <td>{call.purpose}</td>
                      <td>{call.total_tokens.toLocaleString()}</td>
                      <td>{call.latency_ms} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="stat" title={hint}>
      <div className="stat__value">{value}</div>
      <div className="stat__label">{label}</div>
    </div>
  );
}
