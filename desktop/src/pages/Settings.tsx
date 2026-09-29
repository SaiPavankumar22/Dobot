import { useEffect, useState } from "react";
import { Switch } from "../components/Switch";
import { ThemeStudio } from "../components/ThemeStudio";
import {
  api,
  authHeader,
  getApiToken,
  getBaseUrl,
  setApiToken,
  setBaseUrl,
  type KeyRecord,
  type OperatorKey,
} from "../services/api";
import { useDobot } from "../store/dobotStore";
import type { VoiceSummary } from "../types";

interface OnboardingStep {
  id: string;
  title: string;
  done: boolean;
  state?: string;
  detail: string;
}

const STEP_LABELS: Record<string, string> = {
  key_rejected: "key rejected",
  unreachable: "unreachable",
};

/**
 * What each credential the *user* owns actually buys them. The backend publishes the list of
 * writable names; this map only adds the human sentence, and unknown names fall back to the raw id.
 */
const KEY_COPY: Record<string, { label: string; hint: string; placeholder: string; href?: string }> = {
  nebius: {
    label: "Nebius API key",
    hint: "Powers every Nemotron model: reasoning, the vision model that reads attachments, and screen analysis.",
    placeholder: "paste key",
    href: "https://studio.nebius.com/settings/api-keys",
  },
  tavily: {
    label: "Tavily API key",
    hint: "Web search for research answers, deep research and the news automations.",
    placeholder: "tvly-…",
    href: "https://app.tavily.com/home",
  },
  zilliz_token: {
    label: "Zilliz token",
    hint: "Vector memory: recall across past conversations and documents. Needs the URL below too.",
    placeholder: "paste token",
    href: "https://cloud.zilliz.com",
  },
  zilliz_uri: {
    label: "Zilliz URL",
    hint: "The cluster endpoint the token belongs to, e.g. https://in03-xxxx.serverless.gcp-us-west1.cloud.zilliz.com",
    placeholder: "https://…zilliz.com",
  },
};

/** Friendly names for the environment-only values shown as read-only. */
const OPERATOR_COPY: Record<string, string> = {
  mongodb_uri: "Durable memory database",
  langsmith_api_key: "LangSmith tracing key",
  langsmith_api_url: "LangSmith host",
  langsmith_project: "LangSmith project",
  dobot_api_token: "Backend access token",
  laya_api_key: "Laya agreement server key",
  laya_server_url: "Laya agreement server URL",
};

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
  const [connNote, setConnNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [steps, setSteps] = useState<OnboardingStep[]>([]);
  const [voice, setVoice] = useState<VoiceSummary | null>(null);
  const [voiceNote, setVoiceNote] = useState("");
  const [authRequired, setAuthRequired] = useState(false);
  const [keys, setKeys] = useState<KeyRecord[]>([]);
  const [operator, setOperator] = useState<OperatorKey[]>([]);
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [keyNote, setKeyNote] = useState("");

  async function load() {
    setSettings(await api.settings());
    const onboarding = await fetch(`${getBaseUrl()}/settings/onboarding`, { headers: authHeader() }).then(
      (response) => response.json(),
    );
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
      setOperator(listing.operator ?? []);
    } catch {
      // an older backend without /settings/keys — the rest of the page still works
      setKeys([]);
      setOperator([]);
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
        see <strong>Your API keys</strong> below.
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
          placeholder="hf_…  (Hugging Face token)"
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
        <button
          className="ghost"
          onClick={async () => {
            // Persist first: the probe must carry the exact credential the app will use.
            setApiToken(token);
            setConnNote(null);
            try {
              const health = await api.health();
              const degraded = health.degraded?.length ? `, degraded: ${health.degraded.join(", ")}` : "";
              setConnNote({ ok: true, text: `Connected — backend ${health.status}${degraded}.` });
              await load();
            } catch (caught) {
              setConnNote({
                ok: false,
                text: caught instanceof Error ? caught.message : String(caught),
              });
            }
          }}
        >
          Test connection
        </button>
      </div>
      {connNote && (
        <p className="subtle" style={{ color: connNote.ok ? "var(--ok, #3ecf8e)" : "var(--warn, #e5484d)" }}>
          {connNote.ok ? "✓ " : "✕ "}
          {connNote.text}
        </p>
      )}
      <p className="subtle">
        A <strong>Hugging Face token</strong> (read scope is enough) is what lets the app reach the
        private Hugging Face Space — HF's proxy checks it on every request, WebSocket included.
        Create one at{" "}
        <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer">
          huggingface.co/settings/tokens
        </a>
        . The same field is the <span className="mono">DOBOT_API_TOKEN</span> when the backend is one
        you run yourself: generate it with{" "}
        <span className="mono">uv run python -m app.selftest --new-token</span>.{" "}
        {authRequired
          ? "This backend requires a token."
          : "The connected backend is not asking for a token right now."}
      </p>

      <h2>Onboarding</h2>
      {steps.map((step) => (
        <div className="card" key={step.id} style={{ marginBottom: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>{step.title}</strong>
            <span className={`chip ${step.done ? "chip--ok" : "chip--warn"}`}>
              {step.done ? "ready" : STEP_LABELS[step.state ?? ""] ?? "not configured"}
            </span>
          </div>
          <div className="subtle">{step.detail}</div>
        </div>
      ))}

      <h2>Your API keys</h2>
      <p className="subtle">
        Four credentials, all yours. They are stored on the backend you are connected to (its{" "}
        <span className="mono">.env</span>), applied live without a restart, and never sent back to this
        window — a saved field shows only a mask. Everything else the backend needs is supplied by
        whoever operates it; those appear below as read-only.
      </p>
      <div className="card keys" style={{ marginBottom: 16 }}>
        {keys.length === 0 && (
          <div className="subtle">Key management needs the Dobot backend running.</div>
        )}
        {keys.map((key) => {
          const copy = KEY_COPY[key.id];
          return (
            <div className="keys__row" key={key.id}>
              <div className="keys__meta">
                <div className="keys__name">
                  {copy?.label ?? key.id}
                  <span className={`chip ${key.set ? "chip--ok" : "chip--warn"}`}>
                    {key.set ? "set" : "empty"}
                  </span>
                  <span className="mono subtle" style={{ fontSize: 11 }}>
                    {key.id}
                  </span>
                </div>
                <div className="subtle">{copy?.hint ?? "Backend credential."}</div>
                {copy?.href && (
                  <a className="keys__link" href={copy.href} target="_blank" rel="noreferrer">
                    where to get one ↗
                  </a>
                )}
              </div>
              <div className="keys__control">
                <input
                  type={key.kind === "uri" ? "text" : "password"}
                  placeholder={key.set ? key.masked : copy?.placeholder ?? "paste value"}
                  value={keyDrafts[key.id] ?? ""}
                  onChange={(event) =>
                    setKeyDrafts((current) => ({ ...current, [key.id]: event.target.value }))
                  }
                  autoComplete="off"
                  spellCheck={false}
                />
                <button
                  onClick={async () => {
                    try {
                      await api.keySet(key.id, keyDrafts[key.id] ?? "");
                      setKeyDrafts((current) => ({ ...current, [key.id]: "" }));
                      setKeyNote(`${copy?.label ?? key.id} saved and applied live.`);
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
                        setKeyNote(`${copy?.label ?? key.id} cleared.`);
                        await load();
                      } catch (caught) {
                        setKeyNote(caught instanceof Error ? caught.message : String(caught));
                      }
                    }}
                  >
                    Clear
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {keyNote && <div className="subtle" style={{ marginTop: 8 }}>{keyNote}</div>}
      </div>

      {operator.length > 0 && (
        <>
          <h3>Managed for you</h3>
          <p className="subtle">
            These belong to the backend's operator (on the Hugging Face Space, its secrets). They are
            intentionally not editable here — nothing on this page can read or overwrite them.
          </p>
          <div className="card keys">
            {operator.map((entry) => (
              <div className="keys__row keys__row--readonly" key={entry.id}>
                <div className="keys__meta">
                  <div className="keys__name">
                    {OPERATOR_COPY[entry.id] ?? entry.id}
                    <span className={`chip ${entry.set ? "chip--ok" : "chip--idle"}`}>
                      {entry.set ? "provided" : "not set"}
                    </span>
                    <span className="mono subtle" style={{ fontSize: 11 }}>
                      {entry.id}
                    </span>
                  </div>
                  <div className="subtle">{entry.why}</div>
                </div>
                <span className="keys__lock" aria-hidden>
                  🔒
                </span>
              </div>
            ))}
          </div>
        </>
      )}

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
