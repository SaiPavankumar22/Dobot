// Themes and the Theme Studio.
//
// Every colour in the UI comes from fifteen CSS custom properties, so a theme is fifteen values and a
// theme editor is a fifteen-row form. That is the whole trick: the design system is small enough to be
// user-editable without becoming a project.
//
// Persisted in localStorage, and exportable as plain JSON, so a theme is something you can keep, share,
// or paste into a file rather than something buried in a build.

export const TOKEN_KEYS = [
  "--bg",
  "--bg-elevated",
  "--bg-glass",
  "--border",
  "--text",
  "--text-dim",
  "--accent",
  "--accent-dim",
  "--ok",
  "--warn",
  "--danger",
  "--high",
  "--critical",
  "--radius",
  "--shadow",
] as const;

export type TokenKey = (typeof TOKEN_KEYS)[number];
export type ThemeTokens = Record<TokenKey, string>;

export interface ThemeDefinition {
  id: string;
  name: string;
  blurb: string;
  dark: boolean;
  tokens: ThemeTokens;
}

/** The shipped default. Kept in step with the `:root` block in styles.css (pre-paint value). */
export const MIDNIGHT: ThemeTokens = {
  "--bg": "#0b0f17",
  "--bg-elevated": "#131a26",
  "--bg-glass": "rgba(11, 15, 23, 0.78)",
  "--border": "#243043",
  "--text": "#e8eefc",
  "--text-dim": "#94a3b8",
  "--accent": "#5b9dff",
  "--accent-dim": "#2f6fe4",
  "--ok": "#3ddc97",
  "--warn": "#f0b429",
  "--danger": "#ff6b6b",
  "--high": "#ff9f43",
  "--critical": "#ff5c5c",
  "--radius": "14px",
  "--shadow": "0 18px 48px rgba(2, 6, 18, 0.55)",
};

export const THEMES: ThemeDefinition[] = [
  {
    id: "midnight",
    name: "Midnight",
    blurb: "GitHub-dark blue. The default, and the one the asset colours were chosen against.",
    dark: true,
    tokens: MIDNIGHT,
  },
  {
    id: "graphite",
    name: "Graphite",
    blurb: "Neutral greys. Nothing competes with your content, and screenshots read well.",
    dark: true,
    tokens: {
      ...MIDNIGHT,
      "--bg": "#101012",
      "--bg-elevated": "#1a1a1d",
      "--bg-glass": "rgba(16, 16, 18, 0.86)",
      "--border": "#2c2c31",
      "--text": "#ececef",
      "--text-dim": "#8e8e96",
      "--accent": "#b9c4d0",
      "--accent-dim": "#7d8896",
    },
  },
  {
    id: "aurora",
    name: "Aurora",
    blurb: "Teal and violet. Brighter accents for long sessions in a well-lit room.",
    dark: true,
    tokens: {
      ...MIDNIGHT,
      "--bg": "#08131a",
      "--bg-elevated": "#0f1f2b",
      "--bg-glass": "rgba(8, 19, 26, 0.86)",
      "--border": "#1b3342",
      "--text": "#e4f4f6",
      "--text-dim": "#7ea3ad",
      "--accent": "#2ee6c7",
      "--accent-dim": "#12a894",
      "--ok": "#4ade80",
      "--high": "#f0883e",
    },
  },
  {
    id: "ember",
    name: "Ember",
    blurb: "Warm amber on charcoal. The approval and warning states stand out sharply.",
    dark: true,
    tokens: {
      ...MIDNIGHT,
      "--bg": "#141010",
      "--bg-elevated": "#1f1917",
      "--bg-glass": "rgba(20, 16, 16, 0.86)",
      "--border": "#342a25",
      "--text": "#f5ece6",
      "--text-dim": "#a08b80",
      "--accent": "#f0a04b",
      "--accent-dim": "#b8712a",
      "--warn": "#e3b341",
    },
  },
  {
    id: "rosewood",
    name: "Rosewood",
    blurb: "Muted plum. Low contrast on purpose, for working at night without glare.",
    dark: true,
    tokens: {
      ...MIDNIGHT,
      "--bg": "#130f14",
      "--bg-elevated": "#1c1720",
      "--bg-glass": "rgba(19, 15, 20, 0.86)",
      "--border": "#2f2733",
      "--text": "#efe6ee",
      "--text-dim": "#9a8b9b",
      "--accent": "#c99bd6",
      "--accent-dim": "#8c6899",
    },
  },
  {
    id: "daylight",
    name: "Daylight",
    blurb: "Light mode. The only theme here that is not dark; useful for a bright room or a projector.",
    dark: false,
    tokens: {
      ...MIDNIGHT,
      "--bg": "#f6f8fa",
      "--bg-elevated": "#ffffff",
      "--bg-glass": "rgba(255, 255, 255, 0.88)",
      "--border": "#d8dee4",
      "--text": "#1f2328",
      "--text-dim": "#636c76",
      "--accent": "#0969da",
      "--accent-dim": "#0550ae",
      "--ok": "#1a7f37",
      "--warn": "#9a6700",
      "--danger": "#cf222e",
      "--high": "#bc4c00",
      "--critical": "#cf222e",
      "--shadow": "0 12px 40px rgba(31, 35, 40, 0.16)",
    },
  },
];

export interface ThemeTokenGroup {
  label: string;
  hint: string;
  tokens: { key: TokenKey; label: string; kind: "color" | "length" | "shadow" }[];
}

export const TOKEN_GROUPS: ThemeTokenGroup[] = [
  {
    label: "Surfaces",
    hint: "Backgrounds, from the window down to a raised card.",
    tokens: [
      { key: "--bg", label: "Window", kind: "color" },
      { key: "--bg-elevated", label: "Raised", kind: "color" },
      { key: "--bg-glass", label: "Glass", kind: "color" },
      { key: "--border", label: "Border", kind: "color" },
      { key: "--shadow", label: "Shadow", kind: "shadow" },
    ],
  },
  {
    label: "Text",
    hint: "Primary copy and secondary labels.",
    tokens: [
      { key: "--text", label: "Text", kind: "color" },
      { key: "--text-dim", label: "Muted text", kind: "color" },
    ],
  },
  {
    label: "Accent",
    hint: "Selection, focus, and the primary action.",
    tokens: [
      { key: "--accent", label: "Accent", kind: "color" },
      { key: "--accent-dim", label: "Accent (deep)", kind: "color" },
    ],
  },
  {
    label: "Risk",
    hint: "The four levels the decision engine reports, plus success.",
    tokens: [
      { key: "--ok", label: "Success", kind: "color" },
      { key: "--warn", label: "Medium", kind: "color" },
      { key: "--high", label: "High", kind: "color" },
      { key: "--danger", label: "Danger", kind: "color" },
      { key: "--critical", label: "Critical", kind: "color" },
    ],
  },
  {
    label: "Shape",
    hint: "Corner radius for every card and control.",
    tokens: [{ key: "--radius", label: "Radius", kind: "length" }],
  },
];

const THEME_KEY = "dobot.theme";
const OVERRIDE_KEY = "dobot.themeOverrides";

export function findTheme(id: string): ThemeDefinition {
  return THEMES.find((theme) => theme.id === id) ?? THEMES[0];
}

export function loadThemeId(): string {
  return localStorage.getItem(THEME_KEY) || "midnight";
}

export function loadOverrides(): Partial<ThemeTokens> {
  try {
    const raw = localStorage.getItem(OVERRIDE_KEY);
    return raw ? (JSON.parse(raw) as Partial<ThemeTokens>) : {};
  } catch {
    return {};
  }
}

function write(themeId: string, overrides: Partial<ThemeTokens>): void {
  const tokens = { ...findTheme(themeId).tokens, ...overrides };
  const root = document.documentElement;
  for (const key of TOKEN_KEYS) {
    const value = tokens[key];
    if (value) root.style.setProperty(key, value);
  }
  // Light themes need the browser to know, so form controls, scrollbars and the caret follow suit.
  root.style.colorScheme = findTheme(themeId).dark ? "dark" : "light";
}

export function applyTheme(themeId: string, overrides: Partial<ThemeTokens> = loadOverrides()): void {
  write(themeId, overrides);
}

export function setTheme(themeId: string, overrides: Partial<ThemeTokens> = loadOverrides()): void {
  localStorage.setItem(THEME_KEY, themeId);
  write(themeId, overrides);
}

export function setOverrides(themeId: string, overrides: Partial<ThemeTokens>): void {
  const clean = Object.fromEntries(
    Object.entries(overrides).filter(([, value]) => typeof value === "string" && value.trim()),
  ) as Partial<ThemeTokens>;
  if (Object.keys(clean).length) localStorage.setItem(OVERRIDE_KEY, JSON.stringify(clean));
  else localStorage.removeItem(OVERRIDE_KEY);
  write(themeId, clean);
}

export function resetOverrides(themeId: string): void {
  localStorage.removeItem(OVERRIDE_KEY);
  write(themeId, {});
}

/** The current look as a portable document. Everything needed to reproduce it, and nothing else. */
export function exportTheme(themeId: string, overrides: Partial<ThemeTokens> = loadOverrides()): string {
  const theme = findTheme(themeId);
  return JSON.stringify(
    {
      name: theme.name,
      basedOn: theme.id,
      exportedAt: new Date().toISOString(),
      tokens: { ...theme.tokens, ...overrides },
    },
    null,
    2,
  );
}

export interface ImportResult {
  ok: boolean;
  tokens?: ThemeTokens;
  name?: string;
  error?: string;
}

/** Parse an exported theme. Validates rather than trusting: a bad token would silently break the UI. */
export function parseTheme(json: string): ImportResult {
  let payload: unknown;
  try {
    payload = JSON.parse(json);
  } catch {
    return { ok: false, error: "That is not valid JSON." };
  }
  const record = payload as { tokens?: Record<string, unknown>; name?: string };
  if (!record || typeof record !== "object" || !record.tokens || typeof record.tokens !== "object") {
    return { ok: false, error: "A theme needs a `tokens` object." };
  }
  const tokens: Partial<ThemeTokens> = {};
  for (const key of TOKEN_KEYS) {
    const value = record.tokens[key];
    if (typeof value === "string" && value.trim()) tokens[key] = value.trim();
  }
  if (!Object.keys(tokens).length) {
    return { ok: false, error: "None of the expected tokens were present." };
  }
  return { ok: true, tokens: tokens as ThemeTokens, name: record.name };
}

export function downloadTheme(themeId: string, overrides: Partial<ThemeTokens> = loadOverrides()): string {
  const json = exportTheme(themeId, overrides);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const name = `dobot-theme-${themeId}.json`;
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
  return name;
}
