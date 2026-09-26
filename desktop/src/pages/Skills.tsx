import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { SkillRecord } from "../types";

export function Skills() {
  const [skills, setSkills] = useState<SkillRecord[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  async function load() {
    setSkills(await api.skills());
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <>
      <h1>Skills</h1>
      <p className="subtle">
        Reusable workflows Dobot can run on request. Each one still goes through the decision engine —
        a skill cannot grant itself more permission than the plan was approved for — and each one is
        scanned before the planner is even allowed to see it.
      </p>

      {message && <div className="notice">{message}</div>}

      <div className="cards">
        {skills.map((skill) => (
          <div className="card" key={skill.name}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="card__label">{skill.name}</span>
              {skill.scan_status && skill.scan_status !== "clean" && (
                <span
                  className={`pill ${skill.scan_status === "blocked" ? "pill--deny" : "pill--ask"}`}
                  title={skill.scan_findings?.map((f) => `${f.rule}: ${f.detail}`).join("\n")}
                >
                  {skill.scan_status === "blocked" ? "refused by scanner" : `scan: ${skill.scan_status}`}
                </span>
              )}
            </div>
            <p style={{ minHeight: 44 }}>{skill.description}</p>
            {skill.scan_status === "blocked" && (
              <div className="notice notice--danger" style={{ margin: "6px 0" }}>
                <div>
                  The scanner refused this skill, so it is hidden from the planner and cannot run.
                </div>
                {skill.scan_findings?.slice(0, 3).map((finding, index) => (
                  <div className="mono" key={index} style={{ marginTop: 4 }}>
                    [{finding.severity}] {finding.rule} @ {finding.location}: {finding.detail}
                  </div>
                ))}
              </div>
            )}
            <div className="mono" style={{ color: "var(--text-dim)" }}>
              tools: {skill.required_tools.join(", ") || "unspecified"}
            </div>
            {skill.safety && (
              <div className="mono" style={{ color: "var(--warn)", marginTop: 6 }}>
                safety: {skill.safety}
              </div>
            )}
            {skill.workflow.length > 0 && (
              <ol className="mono" style={{ color: "var(--text-dim)", paddingLeft: 18 }}>
                {skill.workflow.slice(0, 6).map((step, index) => (
                  <li key={index}>{String(step.description ?? step.tool ?? JSON.stringify(step))}</li>
                ))}
              </ol>
            )}
            <div className="row" style={{ marginTop: 10 }}>
              <button
                className="primary"
                onClick={async () => {
                  setMessage(`Running “${skill.name}”…`);
                  const response = await api.runSkill(skill.name);
                  setMessage(response.answer || `Task ${response.task_id} started.`);
                }}
              >
                Run
              </button>
              <button
                className="ghost"
                onClick={async () => {
                  await api.deleteSkill(skill.name);
                  await load();
                }}
              >
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
      {skills.length === 0 && <div className="notice">No skills found in the skills/ directory.</div>}
    </>
  );
}
