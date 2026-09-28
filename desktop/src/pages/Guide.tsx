// The Guide — everything the app can do, written for someone who has never seen it.
//
// Deliberately a page and not a README: the person who needs this is already inside the app and does
// not know what "JEV", "approvals" or "doctor" mean. Each entry answers three questions in the same
// order every time: what it is, where to find it, how to use it.

import { useMemo, useState } from "react";
import { IconBook, IconClose } from "../components/Icons";

interface Topic {
  id: string;
  title: string;
  where: string;
  what: string;
  steps?: string[];
  tip?: string;
}

const QUICK_START: { title: string; body: string }[] = [
  {
    title: "1 · Connect your model key",
    body: "Settings → Your API keys → paste your Nebius key → Save. Without it Dobot still runs, but answers are the offline fallback.",
  },
  {
    title: "2 · Just ask",
    body: "Type in the box at the bottom and press Enter. Say what you want done, not how — Dobot plans, shows the plan, and asks before anything risky.",
  },
  {
    title: "3 · Let it watch your screen (optional)",
    body: "‘Select screen’ in the composer (or Ctrl+Shift+S) picks a region. The next message can be about what is in it.",
  },
  {
    title: "4 · Keep it always there (optional)",
    body: "Turn on ‘Always on’ in the header for the floating dot: click it anywhere to ask something without finding this window.",
  },
];

const TOPICS: Topic[] = [
  {
    id: "chat",
    title: "Chat — asking for things",
    where: "The main window. Type in the box at the bottom right of the sidebar.",
    what: "Dobot takes a request in plain words, decides what it needs to do, and reports back with the plan and the result. Every answer shows which model produced it and what it cost.",
    steps: ["Type your request", "Press Enter to send, Shift+Enter for a new line", "Watch the status above the composer: thinking → executing → done"],
    tip: "Ask for outcomes ('tidy my Downloads folder'), not steps. Dobot plans the steps and shows them before running anything risky.",
  },
  {
    id: "modes",
    title: "Modes: Ask, Assist, Agent",
    where: "The three pills directly above the message box.",
    what: "How much Dobot may do without asking you. Ask = explains and plans only. Assist = confirms anything beyond reading. Agent = safe, reversible work runs; anything risky stops and waits for you. This is a real permission threshold, not a label.",
    tip: "Start in Assist when trying something new, and move to Agent once you trust the plan previews.",
  },
  {
    id: "shadow",
    title: "Shadow mode",
    where: "The chip in the composer toolbar.",
    what: "Dobot plans and previews every action but executes nothing. The fastest way to see what a request would do to your machine before letting it near your files.",
    tip: "Shadow mode is perfect for testing a workflow on a folder you care about.",
  },
  {
    id: "attachments",
    title: "Attaching images and files",
    where: "The paperclip button in the composer, or drag files onto the chat, or paste an image.",
    what: "Images are read by the vision model (description and any text in them) and files are read as text. Both become context for that one request. Limits: up to 4 images of 5 MB each, up to 4 text files of 256 KB each. PDFs and Office documents are not supported — the honest alternative is to copy the text in.",
    steps: ["Click the paperclip (or drag files in)", "Check the chips under the message box", "Type what you want done with them, then send"],
    tip: "Screenshot an error, attach it, and ask 'what is wrong here?' — that is the fastest debugging loop Dobot has.",
  },
  {
    id: "voice",
    title: "Talking instead of typing",
    where: "The microphone button in the composer (and on the quick panel), or Ctrl+Shift+Space anywhere.",
    what: "Your speech is transcribed locally by Whisper (faster-whisper) — audio goes only to your own backend, never to a third party. It needs the faster-whisper package (and ffmpeg for browser recordings) installed on the machine running the backend. Hold Ctrl+Shift+Space and speak from any application: the quick panel comes up already listening, letting go sends what you said (a short tap waits for the next press to send); Esc cancels, and a press too short to be words is ignored.",
    steps: ["Hold Ctrl+Shift+Space (or click the mic and speak)", "Talk — the waveform shows it hears you", "Let go to send, or press Esc and edit the words first"],
    tip: "If the mic reports 'not installed', the Doctor page shows the exact command. A Hugging Face Space backend does not ship the ~484 MB model, so voice input is a local-backend feature unless you add it to the Space.",
  },
  {
    id: "speak",
    title: "Hearing answers read aloud",
    where: "Settings → Preferences → 'Speak answers aloud', and the Security page's voice panel.",
    what: "Uses the speech engine already on the machine running the backend (Windows SAPI). Off by default. 'Test voice' speaks a confirmation so you can hear it before enabling anything.",
  },
  {
    id: "screen",
    title: "Pointing Dobot at your screen",
    where: "‘Select screen’ in the composer (Ctrl+Shift+S), or ‘This window’ / Ctrl+Alt+L for the whole window you are in.",
    what: "Drag a rectangle around anything on screen and the next message can be about it — or, with one chord, hand over the window you are looking at without dragging anything: Dobot captures exactly that window (never one of its own) and leaves a preview in the composer to check, swap or drop before the message goes. Capture is strictly on demand — there is no continuous monitoring, and the region is only sent with the message you attach it to.",
    tip: "Ctrl+Alt+L is the 'what's this error?' key: press it on the broken window, then ask. The checkbox under the composer decides whether the pending region rides with your next message.",
  },
  {
    id: "alwayson",
    title: "The floating dot and the quick panel",
    where: "The 'Always on' switch in the chat header.",
    what: "Turns Dobot into something always available: a small dot that floats above every window. Click it for a compact ask panel, drag it anywhere, right-click for the tray menu. Turn the switch off and Dobot is an ordinary chat window again.",
    tip: "The dot's colour and motion show what it is doing — listening, thinking, executing, or waiting for your approval — and hovering it says what it is working on right now.",
  },
  {
    id: "hotkeys",
    title: "Keyboard shortcuts",
    where: "Global — they work from any application (tray menu → Shortcuts lists the same set).",
    what: "Ctrl+Shift+Space: talk — the quick panel opens listening, letting go sends · Ctrl+Alt+L: show Dobot the window you are in · Ctrl+Shift+S: select a screen region · Ctrl+Alt+D: dashboard · Ctrl+Shift+Esc: stop the running task.",
    tip: "A chord another application already owns is reported in the Doctor page rather than failing the app.",
  },
  {
    id: "tasks",
    title: "Tasks",
    where: "Sidebar → Workspace → Tasks (or the dashboard window).",
    what: "Everything Dobot has been asked to do, with status, progress and result. Long jobs keep running in the background and land here; you can run, cancel or delete them.",
  },
  {
    id: "approvals",
    title: "Approvals — where you stay in charge",
    where: "Sidebar → Workspace → Approvals (a badge counts the pending ones).",
    what: "When a step is risky (deleting files, sending things, running commands), Dobot stops and asks. Each request shows the exact action, its risk level and a preview of what will change. Approve, reject, or approve with edits.",
    tip: "Nothing HIGH or CRITICAL happens without this card. Read the preview — that preview is the safety mechanism.",
  },
  {
    id: "automations",
    title: "Automations",
    where: "Sidebar → Workspace → Automations.",
    what: "Saved tasks that run on a schedule ('every Friday at 18:00', 'in two hours'). They run through exactly the same decision engine and approval rules as a chat request — a schedule does not grant permission.",
  },
  {
    id: "memory",
    title: "Memory",
    where: "Sidebar → Workspace → Memory.",
    what: "What Dobot has decided is worth remembering about you and your work: preferences, projects, facts, episodes. Memories fade if unused and are reinforced when recalled. Search, add, or forget any of them.",
    tip: "Durable memory across reinstalls needs a MongoDB connection, which is configured on the backend (see 'Your keys').",
  },
  {
    id: "identity",
    title: "Identity — your standing rules",
    where: "Sidebar → Workspace → Identity.",
    what: "GENOME.md holds rules Dobot must always follow; TELOS.md holds what you are working towards. They are injected into every request, and rules become hard checks, not suggestions.",
    tip: "Write ‘never touch ~/Photos’ or ‘always cite sources’ here once instead of repeating it in every request.",
  },
  {
    id: "skills",
    title: "Skills",
    where: "Sidebar → Workspace → Skills.",
    what: "Saved, reusable workflows — a sequence of steps Dobot can run again (and that you can edit as text). Every skill is scanned for risky instructions before it is allowed to run, and a flagged skill is refused, not silently executed.",
  },
  {
    id: "activity",
    title: "Activity",
    where: "Sidebar → Workspace → Activity.",
    what: "The honest log: every stage of every request — context, decision, each tool call, verification. When something looks wrong, this is where you find out what actually happened.",
  },
  {
    id: "security",
    title: "Security",
    where: "Sidebar → Workspace → Security (also its own page in the dashboard).",
    what: "One page that reports the real posture: the permission matrix per tool, the file guard (credential folders are never touched, approved or not), the skill scanner, the kill switch, and whether execution is isolated or simply in-process.",
    tip: "‘Start Dobot with Windows’ lives here too, if you want it always running.",
  },
  {
    id: "doctor",
    title: "Doctor",
    where: "Dashboard window → Doctor (also linked from Settings).",
    what: "Checks every capability and says which are live, degraded, or simply not configured — with the exact fix for each. Deliberately blunt: this page exists so the app never pretends something works when it does not.",
  },
  {
    id: "keys",
    title: "Your keys vs the backend's keys",
    where: "Settings → Your API keys (and the read-only 'Managed for you' list under it).",
    what: "You provide the four keys that belong to you: Nebius (reasoning — required), Tavily (web research), and the Zilliz URL + token (semantic memory). They are stored by the backend you are connected to, never displayed back — a saved field shows only a mask — and applied immediately.",
    tip: "MongoDB and LangSmith are infrastructure, not user settings: when you use a shared backend (a Hugging Face Space) they are set there as secrets by whoever runs it. The app lists them as read-only and refuses to overwrite them, so your keys and theirs cannot collide.",
  },
  {
    id: "backend",
    title: "Backend and connection",
    where: "Settings → Backend.",
    what: "Where the brain runs. The default is the deployed backend; point it at http://127.0.0.1:8756 to use one on this machine instead. A private deployed backend also needs its access token in the field below the address, or nothing will answer.",
    tip: "If the sidebar says 'backend offline', the address or the token is the first thing to check.",
  },
  {
    id: "appearance",
    title: "Appearance and themes",
    where: "Settings → Appearance (the Theme Studio).",
    what: "Six built-in looks plus a fifteen-swatch editor: surfaces, text, accent, risk colours, corner radius and shadow. Themes are stored as plain values you can export, share and re-import.",
  },
  {
    id: "privacy",
    title: "Where things actually run",
    where: "Worth knowing before you attach anything.",
    what: "Dobot splits into a desktop shell (the window, the dot, hotkeys, screen capture) and a backend (reasoning, memory, tools). Requests, attachments and audio go to whichever backend you are connected to. A deployed backend's file and terminal tools act on that server, not on your laptop — a local backend operates this machine.",
    tip: "For a personal assistant that touches your own files, run the backend locally. For shared memory and research across machines, a deployed backend is the right shape.",
  },
];

export function Guide() {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>("chat");

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return TOPICS;
    return TOPICS.filter((topic) =>
      [topic.title, topic.what, topic.where, topic.tip ?? "", ...(topic.steps ?? [])]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [query]);

  const showingAll = query.trim().length === 0;

  return (
    <div className="guide">
      <header className="guide__head">
        <div className="guide__title">
          <IconBook size={22} />
          <div>
            <h1>How Dobot works</h1>
            <p className="subtle">
              Every feature, where it lives, and how to use it — written for someone seeing this for the
              first time.
            </p>
          </div>
        </div>
        <input
          className="guide__search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search the guide — try “approval”, “voice”, “attach”…"
          spellCheck={false}
        />
      </header>

      {showingAll && (
        <section className="guide__quick">
          {QUICK_START.map((step) => (
            <div className="guide__quick-card" key={step.title}>
              <strong>{step.title}</strong>
              <span>{step.body}</span>
            </div>
          ))}
        </section>
      )}

      <div className="guide__topics">
        {matches.length === 0 && (
          <div className="notice">
            Nothing in the guide matches “{query}”. Try a simpler word, or ask Dobot itself.
          </div>
        )}
        {matches.map((topic) => {
          const expanded = open === topic.id || !showingAll;
          return (
            <article key={topic.id} className={`guide__topic ${expanded ? "guide__topic--open" : ""}`}>
              <button
                className="guide__topic-head"
                onClick={() => setOpen(open === topic.id ? null : topic.id)}
                aria-expanded={expanded}
              >
                <span className="guide__topic-title">{topic.title}</span>
                <span className="guide__topic-toggle">
                  {open === topic.id && showingAll ? <IconClose size={14} /> : "＋"}
                </span>
              </button>
              {expanded && (
                <div className="guide__topic-body">
                  <div className="guide__where">
                    <span className="guide__label">Where</span>
                    {topic.where}
                  </div>
                  <p>{topic.what}</p>
                  {topic.steps && topic.steps.length > 0 && (
                    <ol className="guide__steps">
                      {topic.steps.map((step) => (
                        <li key={step}>{step}</li>
                      ))}
                    </ol>
                  )}
                  {topic.tip && <div className="guide__tip">Tip · {topic.tip}</div>}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}
