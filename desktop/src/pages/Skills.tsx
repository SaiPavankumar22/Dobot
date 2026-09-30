// The Skills page answers three questions per skill — what it does, whether it is allowed to run,
// and what it touches — without stacking all of it on screen at once. Collapsed cards are scannable
// in one pass; one click opens the trigger, the safety sentence, the step list and the file.

import { useEffect, useMemo, useState } from "react";
import { Stats, Toolbar } from "../components/PageBits";
import { api } from "../services/api";
import type { SkillRecord } from "../types";

type Filter = "all" | "clean" | "review" | "blocked";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "clean", label: "Scanned clean" },
  { id: "review", label: "Needs review" },
  { id: "blocked", label: "Refused" },
];

function statusOf(skill: SkillRecord): NonNullable<SkillRecord["scan_status"]> {
  return skill.scan_status ?? "clean";
}

function stepLabel(step: Record<string, unknown>): string {
  const text = String(step.description ?? step.reason ?? "");
  return text || String(step.tool ?? JSON.stringify(step).slice(0, 90));
}

export function Skills() {
  const [skills, setSkills] = useState<SkillRecord[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  async function load() {
    try {
      setSkills(await api.skills());
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const stats = useMemo(() => {
    const review = skills.filter((skill) => statusOf(skill) === "warn").length;
    const blocked = skills.filter((skill) => statusOf(skill) === "blocked").length;
    return { total: skills.length, review, blocked, runnable: skills.length - blocked };
  }, [skills]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return skills.filter((skill) => {
      const status = statusOf(skill);
      if (filter === "blocked" && status !== "blocked") return false;
      if (filter === "review" && status !== "warn") return false;
      if (filter === "clean" && status !== "clean" && status !== "allowlisted" && status !== "off")
        return false;
      if (!needle) return true;
      return [skill.name, skill.description, skill.trigger, skill.safety, ...skill.required_tools]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [skills, query, filter]);

  function clearFilters() {
    setQuery("");
    setFilter("all");
  }

  async function run(skill: SkillRecord) {
    setBusy(skill.name);
    setMessage(`Running “${skill.name}”…`);
    try {
      const response = await api.runSkill(skill.name);
      setMessage(response.answer || `Task ${response.task_id} started.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  async function remove(skill: SkillRecord) {
    setBusy(skill.name);
    try {
      await api.deleteSkill(skill.name);
      setConfirmDelete(null);
      setOpen(null);
      setMessage(`Deleted “${skill.name}”.`);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  function askDelete(skill: SkillRecord) {
    setConfirmDelete(skill.name);
    window.setTimeout(() => setConfirmDelete((current) => (current === skill.name ? null : current)), 4000);
  }

  return (
    <>
      <h1>Skills</h1>
      <p className="subtle">
        Reusable workflows Dobot runs on request. Each one goes through the decision engine — a skill
        cannot grant itself more permission than the plan was approved for — and each is scanned
        before the planner is allowed to see it.
      </p>

      <Stats
        items={[
          { label: "Skills", value: stats.total },
          { label: "Runnable", value: stats.runnable, tone: "ok" },
          { label: "Needs review", value: stats.review, tone: stats.review ? "warn" : undefined },
          { label: "Refused", value: stats.blocked, tone: stats.blocked ? "danger" : undefined },
        ]}
      />

      {message && <div className="notice" style={{ marginTop: 14 }}>{message}</div>}

      <Toolbar
        query={query}
        onQuery={setQuery}
        placeholder="Search by name, tool or what it does…"
        options={FILTERS}
        active={filter}
        onActive={(id) => setFilter(id as Filter)}
        count={
          visible.length === skills.length
            ? `${skills.length} skill${skills.length === 1 ? "" : "s"}`
            : `${visible.length} of ${skills.length}`
        }
      />

      {skills.length === 0 && (
        <div className="notice">No skills found in the skills/ directory.</div>
      )}

      {skills.length > 0 && visible.length === 0 && (
        <div className="notice">
          Nothing matches this filter.{" "}
          <button className="ghost" onClick={clearFilters}>
            Clear filters
          </button>
        </div>
      )}

      <div className="skills__grid">
        {visible.map((skill) => {
          const status = statusOf(skill);
          const expanded = open === skill.name;
          const findings = skill.scan_findings ?? [];
          const tools = skill.required_tools;
          return (
            <div
              key={skill.name}
              className={`skill ${status === "blocked" ? "skill--blocked" : status === "warn" ? "skill--warn" : ""} ${expanded ? "skill--open" : ""}`}
            >
              <div className="skill__head">
                <span className="skill__name" title={skill.name}>
                  {skill.name}
                </span>
                <span
                  className={`pill ${
                    status === "blocked" ? "pill--deny" : status === "warn" ? "pill--ask" : "pill--auto"
                  }`}
                  title={findings.map((finding) => `${finding.rule}: ${finding.detail}`).join("\n") || undefined}
                >
                  {status === "blocked" ? "refused" : status === "warn" ? "needs review" : status}
                </span>
              </div>

              <p className="skill__desc">{skill.description || "No description."}</p>

              {tools.length > 0 && (
                <div className="skill__tools">
                  {tools.slice(0, expanded ? tools.length : 4).map((tool) => (
                    <span className="skill__tool" key={tool}>
                      {tool}
                    </span>
                  ))}
                  {!expanded && tools.length > 4 && (
                    <span className="skill__tool">+{tools.length - 4}</span>
                  )}
                </div>
              )}

              {status === "blocked" && (
                <div className="notice notice--danger">
                  The scanner refused this skill, so it is hidden from the planner and cannot run.
                </div>
              )}

              {expanded && (
                <>
                  {skill.trigger && (
                    <div className="skill__meta">say: “{skill.trigger}”</div>
                  )}
                  {skill.safety && <div className="skill__safety">{skill.safety}</div>}
                  {skill.workflow.length > 0 && (
                    <ol className="skill__steps">
                      {skill.workflow.map((step, index) => (
                        <li key={index}>
                          {stepLabel(step)}
                          {typeof step.tool === "string" && step.tool && (
                            <span className="skill__tool">{step.tool}</span>
                          )}
                        </li>
                      ))}
                    </ol>
                  )}
                  {findings.length > 0 && (
                    <div className="skill__meta" style={{ whiteSpace: "pre-wrap" }}>
                      {findings
                        .slice(0, 5)
                        .map((finding) => `[${finding.severity}] ${finding.rule} @ ${finding.location}`)
                        .join("\n")}
                    </div>
                  )}
                  {skill.path && (
                    <div className="skill__path" title={skill.path}>
                      {skill.path}
                    </div>
                  )}
                </>
              )}

              <div className="skill__actions">
                <button
                  className="primary"
                  disabled={busy !== null || status === "blocked"}
                  onClick={() => void run(skill)}
                >
                  {busy === skill.name ? "Running…" : "Run"}
                </button>
                <button
                  className="ghost"
                  onClick={() => setOpen(expanded ? null : skill.name)}
                  aria-expanded={expanded}
                >
                  {expanded ? "Less" : "Details"}
                </button>
                {confirmDelete === skill.name ? (
                  <button className="danger" disabled={busy !== null} onClick={() => void remove(skill)}>
                    Confirm delete
                  </button>
                ) : (
                  <button className="ghost" disabled={busy !== null} onClick={() => askDelete(skill)}>
                    Delete
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
