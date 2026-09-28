import { useEffect, useState } from "react";
import { Activity } from "./Activity";
import { Approvals } from "./Approvals";
import { Guide } from "./Guide";
import { Automations } from "./Automations";
import { Doctor } from "./Doctor";
import { Identity } from "./Identity";
import { Memory } from "./Memory";
import { Overview } from "./Overview";
import { Security } from "./Security";
import { Settings } from "./Settings";
import { Skills } from "./Skills";
import { Tasks } from "./Tasks";
import { native } from "../services/native";
import { useDobot } from "../store/dobotStore";

const SECTIONS = [
  { id: "guide", label: "Guide" },
  { id: "overview", label: "Overview" },
  { id: "tasks", label: "Tasks" },
  { id: "automations", label: "Automations" },
  { id: "approvals", label: "Approvals" },
  { id: "memory", label: "Memory" },
  { id: "identity", label: "Identity" },
  { id: "skills", label: "Skills" },
  { id: "activity", label: "Activity" },
  { id: "security", label: "Security" },
  { id: "doctor", label: "Doctor" },
  { id: "settings", label: "Settings" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

function sectionFromHash(): SectionId {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const match = SECTIONS.find((section) => section.id === hash);
  return match ? match.id : "overview";
}

export function Dashboard() {
  const [section, setSection] = useState<SectionId>(sectionFromHash);
  const approvals = useDobot((state) => state.approvals);
  const connected = useDobot((state) => state.connected);
  const captureScreen = useDobot((state) => state.captureScreen);

  useEffect(() => {
    const onHash = () => setSection(sectionFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="dashboard">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <span style={{ color: "var(--accent)" }}>●</span> Dobot
        </div>
        {SECTIONS.map((item) => (
          <div
            key={item.id}
            className={`sidebar__item ${section === item.id ? "sidebar__item--active" : ""}`}
            onClick={() => {
              setSection(item.id);
              window.location.hash = `#/${item.id}`;
            }}
          >
            <span>{item.label}</span>
            {item.id === "approvals" && approvals.length > 0 && (
              <span className="chip chip--warn" style={{ marginLeft: "auto" }}>
                {approvals.length}
              </span>
            )}
          </div>
        ))}
        <div className="sidebar__footer">
          <span className={`chip ${connected ? "chip--ok" : "chip--danger"}`}>
            {connected ? "backend connected" : "backend offline"}
          </span>
          <button onClick={() => void captureScreen()}>Select screen</button>
          <button onClick={() => void native.openPanel()}>Open panel</button>
        </div>
      </aside>
      <main className="main">
        {section === "guide" && <Guide />}
        {section === "overview" && <Overview />}
        {section === "tasks" && <Tasks />}
        {section === "automations" && <Automations />}
        {section === "approvals" && <Approvals />}
        {section === "memory" && <Memory />}
        {section === "identity" && <Identity />}
        {section === "skills" && <Skills />}
        {section === "activity" && <Activity />}
        {section === "security" && <Security />}
        {section === "doctor" && <Doctor />}
        {section === "settings" && <Settings />}
      </main>
    </div>
  );
}
