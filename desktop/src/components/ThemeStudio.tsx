// Theme Studio — pick a family, then edit the fifteen tokens underneath it.
//
// Two deliberate choices. First, every edit applies immediately: a theme editor that needs a Save to
// show you the result is a theme editor you stop using. Second, the export is the *whole* look, so a
// theme you build here is a file you keep, not a setting that dies with your browser profile.

import { useMemo, useState } from "react";
import {
  TOKEN_GROUPS,
  THEMES,
  downloadTheme,
  findTheme,
  loadOverrides,
  loadThemeId,
  parseTheme,
  resetOverrides,
  setOverrides,
  setTheme,
  type ThemeTokens,
  type TokenKey,
} from "../theme";

export function ThemeStudio() {
  const [themeId, setThemeId] = useState(loadThemeId);
  const [overrides, setLocalOverrides] = useState<Partial<ThemeTokens>>(loadOverrides);
  const [importText, setImportText] = useState("");
  const [message, setMessage] = useState("");
  const [copied, setCopied] = useState(false);

  const theme = useMemo(() => findTheme(themeId), [themeId]);
  const effective = useMemo(() => ({ ...theme.tokens, ...overrides }), [theme, overrides]);
  const editedCount = Object.keys(overrides).length;

  function choose(id: string) {
    setTheme(id, {});
    setThemeId(id);
    setLocalOverrides({});
    setMessage("");
  }

  function edit(key: TokenKey, value: string) {
    const next = { ...overrides, [key]: value };
    setLocalOverrides(next);
    setOverrides(themeId, next);
  }

  function reset() {
    resetOverrides(themeId);
    setLocalOverrides({});
    setMessage("Back to the stock theme.");
  }

  function copy() {
    const json = JSON.stringify({ name: theme.name, basedOn: theme.id, tokens: effective }, null, 2);
    void navigator.clipboard?.writeText(json);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  function doImport() {
    const result = parseTheme(importText);
    if (!result.ok || !result.tokens) {
      setMessage(result.error ?? "Could not read that theme.");
      return;
    }
    setOverrides(themeId, result.tokens);
    setLocalOverrides(result.tokens);
    setMessage(`Applied ${result.name ?? "imported"} theme.`);
  }

  return (
    <div className="stack">
      <section className="card">
        <div className="row row--between">
          <div>
            <h3 style={{ marginBottom: 2 }}>Theme family</h3>
            <p className="dim" style={{ margin: 0 }}>
              The accent and risk colours were chosen against Midnight; the others shift them.
            </p>
          </div>
        </div>
        <div className="theme-grid">
          {THEMES.map((candidate) => (
            <button
              key={candidate.id}
              className={`theme-card ${candidate.id === themeId ? "theme-card--active" : ""}`}
              onClick={() => choose(candidate.id)}
            >
              <span className="theme-card__swatches" aria-hidden>
                <i style={{ background: candidate.tokens["--bg"] }} />
                <i style={{ background: candidate.tokens["--bg-elevated"] }} />
                <i style={{ background: candidate.tokens["--accent"] }} />
                <i style={{ background: candidate.tokens["--ok"] }} />
                <i style={{ background: candidate.tokens["--high"] }} />
              </span>
              <span className="theme-card__name">{candidate.name}</span>
              <span className="theme-card__blurb">{candidate.blurb}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="card">
        <div className="row row--between">
          <h3 style={{ margin: 0 }}>
            Tokens
            {editedCount > 0 && (
              <span className="chip chip--warn" style={{ marginLeft: 8 }}>
                {editedCount} edited
              </span>
            )}
          </h3>
          <div className="row">
            <button onClick={reset} disabled={editedCount === 0}>
              Reset
            </button>
            <button onClick={() => downloadTheme(themeId, overrides)}>Download .json</button>
            <button onClick={copy}>{copied ? "Copied" : "Copy JSON"}</button>
          </div>
        </div>

        {TOKEN_GROUPS.map((group) => (
          <div key={group.label} style={{ marginTop: 14 }}>
            <div className="dim" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 0.6 }}>
              {group.label}
            </div>
            <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
              {group.hint}
            </div>
            <div className="token-grid">
              {group.tokens.map((token) => (
                <label key={token.key} className="token">
                  <span className="token__label">{token.label}</span>
                  {token.kind === "color" ? (
                    <span className="token__row">
                      <input
                        type="color"
                        value={toHex(effective[token.key])}
                        onChange={(event) => edit(token.key, event.target.value)}
                        aria-label={`${token.label} colour`}
                      />
                      <input
                        type="text"
                        value={effective[token.key]}
                        onChange={(event) => edit(token.key, event.target.value)}
                        spellCheck={false}
                      />
                    </span>
                  ) : (
                    <input
                      type="text"
                      value={effective[token.key]}
                      onChange={(event) => edit(token.key, event.target.value)}
                      spellCheck={false}
                    />
                  )}
                </label>
              ))}
            </div>
          </div>
        ))}
        {message && <p className="dim" style={{ marginTop: 10 }}>{message}</p>}
      </section>

      <section className="card">
        <h3 style={{ marginTop: 0 }}>Import a theme</h3>
        <p className="dim" style={{ marginTop: 0 }}>
          Paste an exported theme (or any JSON with a <code>tokens</code> object). Unknown keys are
          ignored rather than applied blindly.
        </p>
        <textarea
          className="text-input"
          rows={5}
          value={importText}
          placeholder='{ "name": "Mine", "tokens": { "--accent": "#ff00aa" } }'
          onChange={(event) => setImportText(event.target.value)}
          spellCheck={false}
        />
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={doImport} disabled={!importText.trim()}>
            Apply pasted theme
          </button>
        </div>
      </section>
    </div>
  );
}

/** `<input type="color">` only accepts `#rrggbb`, so an `rgba()` token needs a representative hex. */
function toHex(value: string): string {
  if (/^#[0-9a-f]{6}$/i.test(value)) return value;
  const rgba = value.match(/rgba?\(([^)]+)\)/i);
  if (!rgba) return "#000000";
  const parts = rgba[1].split(",").map((part) => Number.parseFloat(part.trim()));
  const [r, g, b] = parts;
  if ([r, g, b].some((part) => Number.isNaN(part))) return "#000000";
  return `#${[r, g, b].map((part) => Math.round(part).toString(16).padStart(2, "0")).join("")}`;
}
