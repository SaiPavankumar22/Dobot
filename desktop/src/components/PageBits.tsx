// The three pieces every Dobot page is built from, so the app reads as one product rather than a
// folder of individually-styled pages:
//
//   Stats    the numbers that page is about, in one strip at the top
//   Toolbar  search + filter chips + a count, one anatomy everywhere
//   Record   a list item: title, status, mono metadata, body, actions pinned right
//
// Each is deliberately small: pages own their data and their copy, these own the shape.

import type { ReactNode } from "react";

const TONE: Record<string, string> = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  danger: "var(--danger)",
  accent: "var(--accent)",
};

export interface StatItem {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: keyof typeof TONE;
}

export function Stats({ items }: { items: StatItem[] }) {
  return (
    <div className="cards">
      {items.map((item) => (
        <div className="card" key={item.label}>
          <div className="card__label">{item.label}</div>
          <div className="card__value" style={item.tone ? { color: TONE[item.tone] } : undefined}>
            {item.value}
          </div>
          {item.hint && <div className="subtle">{item.hint}</div>}
        </div>
      ))}
    </div>
  );
}

export interface FilterOption {
  id: string;
  label: string;
}

export function Toolbar({
  query,
  onQuery,
  placeholder,
  options,
  active,
  onActive,
  count,
  right,
}: {
  query?: string;
  onQuery?: (value: string) => void;
  placeholder?: string;
  options: FilterOption[];
  active: string;
  onActive: (id: string) => void;
  /** `"12 of 40"` or `"40 tasks"` — whatever the page wants to say. */
  count?: string;
  right?: ReactNode;
}) {
  return (
    <div className="skills__toolbar">
      {onQuery && (
        <input
          className="skills__search"
          placeholder={placeholder ?? "Search…"}
          value={query ?? ""}
          onChange={(event) => onQuery(event.target.value)}
        />
      )}
      {options.map((option) => (
        <button
          key={option.id}
          className={`skills__filter ${active === option.id ? "skills__filter--active" : ""}`}
          onClick={() => onActive(option.id)}
          aria-pressed={active === option.id}
        >
          {option.label}
        </button>
      ))}
      {right}
      {count && <span className="skills__count">{count}</span>}
    </div>
  );
}

export function Record({
  title,
  status,
  meta,
  body,
  children,
  actions,
  tone,
}: {
  title: ReactNode;
  /** Rendered on the head row, right-aligned: a status pill or chip. */
  status?: ReactNode;
  /** One mono line: when, where, how much. */
  meta?: ReactNode;
  body?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  tone?: "warn" | "danger";
}) {
  return (
    <div className={`record ${tone ? `record--${tone}` : ""}`}>
      <div className="record__head">
        <span className="record__title">{title}</span>
        {status}
      </div>
      {meta && <div className="record__meta">{meta}</div>}
      {body && <div className="record__body">{body}</div>}
      {children}
      {actions && <div className="record__actions">{actions}</div>}
    </div>
  );
}
