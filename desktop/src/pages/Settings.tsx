import { useEffect, useState } from "react";
import { Switch } from "../components/Switch";
import { ThemeStudio } from "../components/ThemeStudio";
import {
  api,
  authHeader,
  type BackendCapabilities,
  type HardwareProfile,
  getApiToken,
  getBaseUrl,
  setApiToken,
  type KeyRecord,
  type OperatorKey,
} from "../services/api";
import { useDobot } from "../store/dobotStore";
import { isNative } from "../services/native";
import { checkForUpdates } from "../services/updater";
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
  novita: {
    label: "Novita API key",
    hint: "Runs JEV-27B-VL, the judgement model: every proposed action is scored for risk, untrusted on-screen text is checked for prompt injection, and the captured screen is checked for visible credentials.",
    placeholder: "sk_…",
    href: "https://novita.ai/settings/key-management",
  },
  sarvam: {
    label: "Sarvam API key",
    hint: "Enables spoken replies for the live voice agent.",
    placeholder: "paste key",
    href: "https://dashboard.sarvam.ai/",
  },
};

/** Friendly names for the environment-only values shown as read-only. */
const OPERATOR_COPY: Record<string, string> = {
  dobot_api_token: "Backend access token",
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
  const connect = useDobot((state) => state.connect);
  const disconnect = useDobot((state) => state.disconnect);
  const [updateBusy, setUpdateBusy] = useState(false);
  const [updateNote, setUpdateNote] = useState("");
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
  const [capabilities, setCapabilities] = useState<BackendCapabilities | null>(null);
  const [hardware, setHardware] = useState<HardwareProfile | null>(null);
  const [storage, setStorage] = useState<Record<string, unknown> | null>(null);
  const [storageNote, setStorageNote] = useState("");

  async function load() {
    setSettings(await api.settings());
    try {
      setCapabilities(await api.capabilities());
      setHardware(await api.runtimeProfile());
      setStorage(await api.storage());
    } catch {
      setCapabilities(null);
      setHardware(null);
      setStorage(null);
    }
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
        <span className="mono">Local backend · {getBaseUrl()}</span>
      </div>
      <div className="row" style={{ marginTop: 8, alignItems: "flex-start" }}>
        <input
          value={token}
          type="password"
          placeholder="optional local API token"
          onChange={(event) => setToken(event.target.value)}
          style={{ maxWidth: 320 }}
          autoComplete="off"
          spellCheck={false}
        />
        <button
          onClick={() => {
            setApiToken(token);
            // The WebSocket authenticates with the token from its query string, so a token change
            // only takes effect once the handshake is redone.
            disconnect();
            connect();
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
        The local API token is optional. If <span className="mono">DOBOT_API_TOKEN</span> is enabled,
        generate it with{" "}
        <span className="mono">uv run python -m app.selftest --new-token</span>.{" "}
        {authRequired
          ? "This backend requires a token."
          : "The connected backend is not asking for a token right now."}
      </p>

      <h2>Storage and chat history</h2>
      <div className="card">
        <p className="subtle">
          Chats and memories are stored by the connected backend. Old traces and activity can be
          pruned without deleting conversations or persistent memories.
        </p>
        {storage ? (
          <>
            <div className="subtle">
              {Object.entries((storage.collections ?? {}) as Record<string, unknown>)
                .map(([name, count]) => `${name}: ${String(count)}`)
                .join(" · ")}
            </div>
            {!!storage.database && (
              <div className="subtle" style={{ marginTop: 6 }}>
                SQLite: {String((storage.database as Record<string, unknown>).path ?? "unknown")} ·{" "}
                {String((storage.database as Record<string, unknown>).integrity ?? "unknown")} ·{" "}
                {String((storage.database as Record<string, unknown>).bytes ?? 0)} bytes
              </div>
            )}
          </>
        ) : (
          <div className="subtle">Storage details unavailable.</div>
        )}
        <div className="row" style={{ marginTop: 10 }}>
          <button
            className="ghost"
            onClick={async () => {
              setStorage(await api.storage());
              setStorageNote("Storage refreshed.");
            }}
          >
            Refresh storage
          </button>
          <button
            className="ghost"
            onClick={async () => {
              const result = await api.pruneStorage(30, false);
              setStorage(await api.storage());
              setStorageNote(`Removed ${JSON.stringify(result.removed ?? {})}. Chats and memories kept.`);
            }}
          >
            Prune old traces
          </button>
          <button
            className="ghost"
            onClick={async () => {
              await api.checkpointStorage();
              setStorage(await api.storage());
              setStorageNote("Database checkpoint completed.");
            }}
          >
            Checkpoint database
          </button>
          <button
            className="ghost"
            onClick={async () => {
              await api.vacuumStorage();
              setStorage(await api.storage());
              setStorageNote("Database vacuum completed.");
            }}
          >
            Compact database
          </button>
          <button
            className="ghost"
            onClick={async () => {
              const result = await api.backupStorage();
              setStorageNote(`Backup created at ${result.backup}.`);
            }}
          >
            Create backup
          </button>
          <button
            className="ghost"
            onClick={async () => {
              const result = await api.exportStorage();
              const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
              const url = URL.createObjectURL(blob);
              const link = document.createElement("a");
              link.href = url;
              link.download = "dobot-export.json";
              link.click();
              URL.revokeObjectURL(url);
              setStorageNote("Chat and memory export downloaded.");
            }}
          >
            Export chats and memory
          </button>
          {storageNote && <span className="subtle">{storageNote}</span>}
        </div>
      </div>

      <h2>Backend capabilities</h2>
      {capabilities ? (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>{capabilities.backend_location} backend</strong>
            <span className="chip chip--ok">detected</span>
          </div>
          <p className="subtle">
            These describe what this backend can do in its own environment. A hosted backend cannot
            access this laptop's files, screen or desktop applications.
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
            <div><strong>Browser</strong><div className="subtle">
              {capabilities.browser.available
                ? capabilities.browser.logged_in === null
                  ? "available; login not checked"
                  : capabilities.browser.logged_in ? "available; logged in" : "available; signed out"
                : capabilities.browser.detail}
            </div></div>
            <div><strong>Hermes</strong><div className="subtle">
              {capabilities.hermes.available ? "available" : "unavailable"}
            </div></div>
            <div><strong>Screen</strong><div className="subtle">
              {capabilities.local_screen.available ? capabilities.local_screen.detail : "unavailable"}
            </div></div>
            <div><strong>Files</strong><div className="subtle">
              {capabilities.local_files.available ? capabilities.local_files.detail : "unavailable"}
            </div></div>
            <div><strong>Voice</strong><div className="subtle">
              {capabilities.voice.available ? "available" : capabilities.voice.detail}
            </div></div>
            <div><strong>Sandbox</strong><div className="subtle">
              {capabilities.sandbox.provider} / {capabilities.sandbox.isolation}
            </div></div>
          </div>
          {hardware && (
            <p className="subtle" style={{ marginBottom: 0 }}>
              Compute guidance: <strong>{hardware.recommended_mode}</strong> / {hardware.local_model_class}.
              {" "}{hardware.detail}
            </p>
          )}
        </div>
      ) : (
        <p className="subtle">Capability information is unavailable until the backend connects.</p>
      )}

      <h2>Updates</h2>
      <div className="row">
        <button
          disabled={!isNative || updateBusy}
          onClick={async () => {
            setUpdateBusy(true);
            setUpdateNote("");
            try {
              const found = await checkForUpdates();
              setUpdateNote(
                found
                  ? `Dobot ${found.version} is available — the banner offers Update now.`
                  : "You are on the latest version.",
              );
            } catch (caught) {
              setUpdateNote(caught instanceof Error ? caught.message : String(caught));
            }
            setUpdateBusy(false);
          }}
        >
          {updateBusy ? "Checking…" : "Check for updates"}
        </button>
        <span className="subtle">
          {updateNote ||
            (isNative
              ? "Updates arrive from GitHub Releases, are signature-checked, and install here."
              : "Updates only exist in the installed (Tauri) app.")}
        </span>
      </div>

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
        Provider credentials are stored locally by the backend you are connected to (its{" "}
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
            These belong to the local backend operator. They are
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
