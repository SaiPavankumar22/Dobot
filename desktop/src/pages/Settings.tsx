import { useEffect, useState } from "react";
import { Switch } from "../components/Switch";
import { ThemeStudio } from "../components/ThemeStudio";
import { api, getApiToken, getBaseUrl, setApiToken, setBaseUrl, type KeyRecord } from "../services/api";
import { useDobot } from "../store/dobotStore";
import type { VoiceSummary } from "../types";

interface OnboardingStep {
  id: string;
  title: string;
  done: boolean;
  detail: string;
}

export function Settings() {
  const providers = useDobot((state) => state.providers);
  const refreshProviders = useDobot((state) => state.refreshProviders);
  const shadowMode = useDobot((state) => state.shadowMode);
  const setShadowMode = useDobot((state) => state.setShadowMode);
  const dotEnabled = useDobot((state) => state.dotEnabled);
  const setDotEnabled = useDobot((state) => state.setDotEnabled);
  const speakAnswers = useDobot((state) => state.speakAnswers);
  const setSpeakAnswers = useDobot((state) => state.setSpeakAnswers);
  const [baseUrl, setBase] = useState(getBaseUrl());
  const [token, setToken] = useState(getApiToken());
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [steps, setSteps] = useState<OnboardingStep[]>([]);
  const [voice, setVoice] = useState<VoiceSummary | null>(null);
  const [voiceNote, setVoiceNote] = useState("");
  const [authRequired, setAuthRequired] = useState(false);
  const [keys, setKeys] = useState<KeyRecord[]>([]);
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [keyNote, setKeyNote] = useState("");

  async function load() {
    setSettings(await api.settings());
    const onboarding = await fetch(`${getBaseUrl()}/settings/onboarding`).then((response) => response.json());
    setSteps(onboarding.steps ?? []);
    setVoice(await api.voice());
    try {
      setAuthRequired((await api.authStatus()).required);
    } catch {
      setAuthRequired(false);
    }
    await refreshProviders();
    try {
      const listing = await api.keyList();
      setKeys(listing.keys ?? []);
    } catch {
      setKeys([]); // an older backend without /settings/keys — the rest of the page still works
    }
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <>
      <h1>Settings</h1>
      <p className="subtle">
        Credentials live only in the backend. This page never displays a key — but it can store one:
        see API keys below.
      </p>

      <h2>Backend</h2>
      <div className="row">
        <input value={baseUrl} onChange={(event) => setBase(event.target.value)} style={{ maxWidth: 320 }} />
        <button
          onClick={() => {
            setBaseUrl(baseUrl);
            setApiToken(token);
            void load();
          }}
        >
          Save and reconnect
        </button>
      </div>
      <div className="row" style={{ marginTop: 8, alignItems: "flex-start" }}>
        <input
          value={token}
          type="password"
          placeholder="optional API token"
          onChange={(event) => setToken(event.target.value)}
          style={{ maxWidth: 320 }}
          autoComplete="off"
          spellCheck={false}
        />
        <button
          onClick={() => {
            setApiToken(token);
            void load();
          }}
        >
          Save token
        </button>
      </div>
      <p className="subtle">
        {authRequired
          ? "This backend requires a token. Generate one with `uv run python -m app.selftest --new-token`, add it to .env, and paste it here."
          : "The backend is not requiring a token right now. Set DOBOT_API_TOKEN to add a gate — worth it if this machine shares a network."}
      </p>

      <h2>Onboarding</h2>
      {steps.map((step) => (
        <div className="card" key={step.id} style={{ marginBottom: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>{step.title}</strong>
            <span className={`chip ${step.done ? "chip--ok" : "chip--warn"}`}>
              {step.done ? "ready" : "not configured"}
            </span>
          </div>
          <div className="subtle">{step.detail}</div>
        </div>
      ))}

      <h2>API keys</h2>
      <p className="subtle">
        Stored in the backend's <span className="mono">.env</span> and applied live — no restart, and it
        works the same in the packaged app. Values are saved server-side and never displayed back.
      </p>
      <div className="card" style={{ marginBottom: 16 }}>
        {keys.length === 0 && <div className="subtle">Key management needs the Dobot backend running.</div>}
        {keys.map((key) => (
          <div className="row" key={key.id} style={{ marginBottom: 8, alignItems: "center" }}>
            <span className="mono" style={{ width: 150 }}>
              {key.id}
            </span>
            <input
              type="password"
              placeholder={key.set ? key.masked : key.kind === "uri" ? "https://…" : "paste key"}
              value={keyDrafts[key.id] ?? ""}
              onChange={(event) => setKeyDrafts((current) => ({ ...current, [key.id]: event.target.value }))}
              style={{ maxWidth: 320 }}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              onClick={async () => {
                try {
                  await api.keySet(key.id, keyDrafts[key.id] ?? "");
                  setKeyDrafts((current) => ({ ...current, [key.id]: "" }));
                  setKeyNote(`${key.id} saved and applied live.`);
                  await load();
                } catch (caught) {
                  setKeyNote(caught instanceof Error ? caught.message : String(caught));
                }
              }}
              disabled={!(keyDrafts[key.id] ?? "").trim()}
            >
              Save
            </button>
            {key.set && (
              <button
                className="ghost"
                onClick={async () => {
                  try {
                    await api.keyClear(key.id);
                    setKeyNote(`${key.id} cleared.`);
                    await load();
                  } catch (caught) {
                    setKeyNote(caught instanceof Error ? caught.message : String(caught));
                  }
                }}
              >
                Clear
              </button>
            )}
            <span className={`chip ${key.set ? "chip--ok" : "chip--warn"}`}>{key.set ? "set" : "empty"}</span>
          </div>
        ))}
        {keyNote && <div className="subtle">{keyNote}</div>}
      </div>

      <h2>Providers</h2>
      <table className="table">
        <tbody>
          {Object.entries(providers).map(([name, value]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td>
                <span
                  className={`status status--${
                    value.includes("connected") || value.includes("available") ? "COMPLETED" : "WAITING_USER"
                  }`}
                >
                  {value}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Preferences</h2>
      <div className="row">
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={shadowMode}
            onChange={(event) => void setShadowMode(event.target.checked)}
          />
          Shadow mode (plan without executing)
        </label>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <Switch
          checked={dotEnabled}
          onChange={(value) => void setDotEnabled(value)}
          label="Always-on floating dot"
          hint="Keep the dot above every window; off means a plain chat window"
        />
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <Switch
          checked={speakAnswers}
          onChange={(value) => void setSpeakAnswers(value)}
          label="Speak answers aloud"
          hint={`${voice?.engine || "no speech engine detected"}${
            voice?.available ? " — uses the engine already on this machine, offline" : ""
          }`}
        />
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <button
          onClick={async () => {
            try {
              const result = await api.speak("Voice is working. This is Dobot.", true);
              setVoiceNote(`Spoke ${result.spoken_chars} characters via ${result.engine}.`);
            } catch (caught) {
              setVoiceNote(caught instanceof Error ? caught.message : String(caught));
            }
          }}
        >
          Test voice
        </button>
        {voiceNote && <span className="subtle">{voiceNote}</span>}
      </div>
      {voice && !voice.available && voice.probe.fix && (
        <p className="subtle">No engine found: {voice.probe.fix}</p>
      )}

      <h2>Appearance</h2>
      <ThemeStudio />
      {settings && (
        <div className="notice" style={{ marginTop: 12 }}>
          <div className="mono">reasoning model: {String((settings.models as Record<string, string>)?.primary)}</div>
          <div className="mono">agent runtime: {String((settings.agent as Record<string, string>)?.runtime)}</div>
          <div className="mono">sandbox: {String((settings.sandbox as Record<string, string>)?.provider)}</div>
          <div className="mono">research: {String((settings.research as Record<string, string>)?.provider)}</div>
        </div>
      )}
    </>
  );
}
