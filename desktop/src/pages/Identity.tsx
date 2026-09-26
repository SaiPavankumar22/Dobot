// Identity — the documents that make Dobot yours, and the facts it will never have to look up.
//
// Three documents with three different owners, and the ownership is enforced in the backend rather than
// described here: GENOME.md and TELOS.md can only be written by you. What you write in GENOME.md stops
// being prose the moment it lands — the fenced rules block is compiled into hard runtime rules, so the
// page shows the compiled result next to your text, and a rule tester you can aim at any tool call.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../services/api";
import type { CanonicalFact, IdentitySummary, InterceptorReport } from "../types";

const DOCUMENTS = [
  {
    name: "GENOME.md",
    title: "Genome",
    role: "Yours. Dobot never rewrites it.",
    blurb:
      "Standing rules in plain language, plus an optional ```dobot-rules JSON block that is compiled into enforced rules.",
  },
  {
    name: "TELOS.md",
    title: "Telos",
    role: "Yours. Where you are going.",
    blurb:
      "Current state, ideal state, constraints. Read before every plan, so a request is judged against your goal and not only its wording.",
  },
  {
    name: "MEMORY.md",
    title: "Working notes",
    role: "Dobot's. Maintained automatically.",
    blurb: "What it has learned about how you work, rewritten by each maintenance pass. Read-only here.",
  },
] as const;

export function Identity() {
  const [summary, setSummary] = useState<IdentitySummary | null>(null);
  const [interceptors, setInterceptors] = useState<InterceptorReport | null>(null);
  const [facts, setFacts] = useState<CanonicalFact[]>([]);
  const [active, setActive] = useState<string>("GENOME.md");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [newFact, setNewFact] = useState("");
  const [probeTool, setProbeTool] = useState("fs_write");
  const [probeParams, setProbeParams] = useState('{"path": "~/Documents/Tax/2025.pdf"}');
  const [probeResult, setProbeResult] = useState<{ verdict: string; fired: string[]; reasons: string[] } | null>(
    null,
  );

  const load = useCallback(async () => {
    try {
      const [nextSummary, nextInterceptors, canonical] = await Promise.all([
        api.identity(),
        api.interceptors(),
        api.canonical(),
      ]);
      setSummary(nextSummary);
      setInterceptors(nextInterceptors);
      setFacts(canonical.facts);
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    void api
      .identityDocument(active)
      .then((document) => {
        if (!cancelled) setDraft(document.content);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  const meta = DOCUMENTS.find((document) => document.name === active)!;
  const readOnly = meta.name === "MEMORY.md";

  async function save() {
    try {
      await api.saveIdentityDocument(active, draft);
      setNote(
        active === "GENOME.md"
          ? "Saved. Your rules were recompiled immediately — no restart needed."
          : "Saved.",
      );
      await load();
    } catch (caught) {
      setNote(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  async function runProbe() {
    let params: Record<string, unknown>;
    try {
      params = JSON.parse(probeParams) as Record<string, unknown>;
    } catch {
      setProbeResult({ verdict: "INVALID", fired: [], reasons: ["That is not valid JSON."] });
      return;
    }
    try {
      const result = await api.testInterceptors(probeTool, params);
      setProbeResult(result.outcome);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <div className="stack">
      <section className="card">
        <h2 style={{ margin: 0 }}>Identity</h2>
        <p className="dim" style={{ marginTop: 4 }}>
          Injected on <strong>every</strong> turn with no model call. The things that must never be
          forgotten should not have to win a search to be present.
        </p>
        {error && <p className="error">{error}</p>}
        {summary && (
          <div className="row" style={{ flexWrap: "wrap" }}>
            {summary.axioms.length > 0 ? (
              summary.axioms.map((axiom) => (
                <span key={axiom} className="chip chip--muted" title="Enforced as a runtime rule when the rules block compiles">
                  {axiom.slice(0, 60)}
                </span>
              ))
            ) : (
              <span className="dim">
                No standing rules yet. Add some under <code className="code-inline">## Axioms</code> in
                GENOME.md and they will be attached to every request.
              </span>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <div className="row">
          {DOCUMENTS.map((document) => (
            <button
              key={document.name}
              className={active === document.name ? "tab tab--active" : "tab"}
              onClick={() => {
                setActive(document.name);
                setNote("");
              }}
            >
              {document.title}
            </button>
          ))}
        </div>
        <p className="dim" style={{ marginTop: 10 }}>
          <strong>{meta.name}</strong> · {meta.role} {meta.blurb}
        </p>
        <textarea
          className="text-input mono"
          rows={18}
          value={draft}
          readOnly={readOnly}
          spellCheck={false}
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={() => void save()} disabled={readOnly}>
            Save {meta.name}
          </button>
          <button onClick={() => void load()} disabled={readOnly}>
            Reload from disk
          </button>
          {readOnly && <span className="dim">Written by the maintenance pass.</span>}
          {note && <span className="dim">{note}</span>}
        </div>
      </section>

      <section className="card">
        <div className="row row--between">
          <div>
            <h3 style={{ margin: 0 }}>Enforced rules</h3>
            <p className="dim" style={{ margin: "2px 0 0" }}>
              A prompt instruction is advice. These are code paths: they run before every tool call and
              can only ever make a decision <em>stricter</em>.
            </p>
          </div>
          <button
            onClick={async () => {
              const result = await api.reloadInterceptors();
              setNote(`Recompiled ${result.rules} rule(s).`);
              await load();
            }}
          >
            Recompile
          </button>
        </div>

        {interceptors?.errors && interceptors.errors.length > 0 && (
          <div className="notice notice--warn">
            <strong>{interceptors.errors.length} rule problem(s)</strong>
            <ul>
              {interceptors.errors.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </div>
        )}

        {interceptors && interceptors.count === 0 ? (
          <p className="dim">
            No rules compiled. Add a <code className="code-inline">```dobot-rules</code> block to
            GENOME.md, or an <code className="code-inline">interceptors</code> array to a skill's
            workflow.json.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Rule</th>
                <th>Action</th>
                <th>Tools</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {interceptors?.rules.map((rule) => (
                <tr key={rule.id}>
                  <td title={rule.reason}>
                    {rule.id}
                    {rule.reason && <div className="dim" style={{ fontSize: 12 }}>{rule.reason}</div>}
                  </td>
                  <td>
                    <span
                      className={`chip ${
                        rule.action === "BLOCK"
                          ? "chip--danger"
                          : rule.action === "APPROVAL"
                            ? "chip--warn"
                            : "chip--muted"
                      }`}
                    >
                      {rule.action}
                    </span>
                  </td>
                  <td>{rule.tools.length ? rule.tools.join(", ") : "all"}</td>
                  <td className="dim">{rule.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <h4 style={{ marginBottom: 4 }}>Rule tester</h4>
        <p className="dim" style={{ marginTop: 0 }}>
          Aim a hypothetical tool call at the rules. Nothing is executed.
        </p>
        <div className="row" style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
          <input
            className="text-input"
            style={{ maxWidth: 180 }}
            value={probeTool}
            onChange={(event) => setProbeTool(event.target.value)}
            spellCheck={false}
            aria-label="Tool name"
          />
          <input
            className="text-input mono"
            style={{ flex: 1, minWidth: 260 }}
            value={probeParams}
            onChange={(event) => setProbeParams(event.target.value)}
            spellCheck={false}
            aria-label="Tool parameters as JSON"
          />
          <button onClick={() => void runProbe()}>Test</button>
        </div>
        {probeResult && (
          <div
            className={`notice ${
              probeResult.verdict === "BLOCK"
                ? "notice--danger"
                : probeResult.verdict === "APPROVAL"
                  ? "notice--warn"
                  : ""
            }`}
          >
            <strong>{probeResult.verdict}</strong>
            {probeResult.fired.length > 0 && <> — fired {probeResult.fired.join(", ")}</>}
            {probeResult.reasons.length > 0 && (
              <ul>
                {probeResult.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            )}
            {probeResult.verdict === "ALLOW" && probeResult.fired.length === 0 && (
              <span className="dim"> No rule matched this call.</span>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <div className="row row--between">
          <div>
            <h3 style={{ margin: 0 }}>Canonical facts</h3>
            <p className="dim" style={{ margin: "2px 0 0" }}>
              Capped on purpose. Restating a fact strengthens it; contradicting a well-established one
              is recorded as a candidate rather than obeyed immediately, and superseding keeps the old
              belief in history.
            </p>
          </div>
          <span className="chip chip--muted">{facts.length} resident</span>
        </div>

        <div className="row" style={{ marginTop: 10 }}>
          <input
            className="text-input"
            style={{ flex: 1 }}
            placeholder="State a fact you want never to have to repeat…"
            value={newFact}
            onChange={(event) => setNewFact(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && newFact.trim()) {
                void api.stateFact({ content: newFact }).then(() => {
                  setNewFact("");
                  return load();
                });
              }
            }}
          />
          <button
            disabled={!newFact.trim()}
            onClick={() =>
              void api.stateFact({ content: newFact }).then(() => {
                setNewFact("");
                return load();
              })
            }
          >
            State it
          </button>
        </div>

        {facts.length === 0 ? (
          <p className="dim">
            Empty. Facts arrive here by repetition — state something a few times, or run a maintenance
            pass from the Doctor page, and it graduates out of ordinary memory.
          </p>
        ) : (
          <div className="cap-list">
            {facts.map((fact) => (
              <div key={fact.id} className="cap">
                <div className="cap__head">
                  <strong style={{ flex: 1 }}>{fact.content}</strong>
                  <span className="chip chip--muted">{fact.confidence.toFixed(2)}</span>
                  <span className="dim" style={{ fontSize: 12 }}>
                    stated {fact.reinforcements}×
                  </span>
                  {fact.pinned && <span className="chip chip--ok">pinned</span>}
                </div>
                {fact.candidates.length > 0 && (
                  <p className="cap__detail">
                    A contradicting statement is being counted, not yet believed:{" "}
                    <em>{fact.candidates[0].text}</em> ({fact.candidates[0].count}×)
                  </p>
                )}
                {fact.history.length > 0 && (
                  <p className="cap__detail dim">
                    previous: {fact.history[fact.history.length - 1].text} —{" "}
                    {fact.history[fact.history.length - 1].reason}
                  </p>
                )}
                <div className="row" style={{ marginTop: 6 }}>
                  <button
                    onClick={() => void api.pinFact(fact.id, !fact.pinned).then(() => load())}
                  >
                    {fact.pinned ? "Unpin" : "Pin"}
                  </button>
                  <button
                    onClick={() => void api.forgetFact(fact.id).then(() => load())}
                    className="danger"
                  >
                    Forget
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {summary && (
        <section className="card">
          <h3 style={{ marginTop: 0 }}>What the model actually receives</h3>
          <p className="dim" style={{ marginTop: 0 }}>
            Rendered from your documents, on every turn. Also visible in the Doctor page's context
            count.
          </p>
          <pre className="code-block">{summary.rendered || "(nothing yet — add axioms or fill in TELOS.md)"}</pre>
          <p className="dim" style={{ fontSize: 12 }}>
            Files live in {summary.directory}
          </p>
        </section>
      )}
    </div>
  );
}
