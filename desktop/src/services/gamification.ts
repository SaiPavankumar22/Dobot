// A small progress layer over work the user already does: XP for finished tasks, a level
// that reflects accumulated XP, and a daily streak. Everything is computed and stored
// locally (`localStorage`) — the backend knows nothing about points, and nothing about
// the user's progress leaves this machine.
//
// The curve is deliberately shallow: level n starts at (n-1)^2 · 120 XP, so the first
// levels arrive in one sitting and later ones need real, repeated use — the point is a
// gentle pulse of feedback, not a slot machine.

import { create } from "zustand";

const STORAGE_KEY = "dobot.progress.v1";

export const XP_REWARDS = {
  taskCompleted: 25,
  automationCreated: 15,
  researchRun: 10,
  inviteCreated: 20,
} as const;

export type XpKind = keyof typeof XP_REWARDS;

export const LEVEL_TITLES = [
  "Rookie",
  "Apprentice",
  "Operator",
  "Pro",
  "Expert",
  "Master",
  "Legend",
] as const;

type Progress = { xp: number; days: string[] };

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function load(): Progress {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Progress>;
      if (typeof parsed.xp === "number" && Array.isArray(parsed.days)) {
        return { xp: Math.max(0, parsed.xp), days: parsed.days.filter((d) => typeof d === "string") };
      }
    }
  } catch {
    /* corrupted or unavailable storage — start fresh */
  }
  return { xp: 0, days: [] };
}

function persist(progress: Progress): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(progress));
  } catch {
    /* private mode / quota — progress just won't survive a reload */
  }
}

/** Level n starts at (n-1)^2 · 120 XP. Level 1 starts at 0. */
export function levelStart(level: number): number {
  return (level - 1) * (level - 1) * 120;
}

export function levelOf(xp: number): number {
  return 1 + Math.floor(Math.sqrt(xp / 120));
}

export function levelTitle(level: number): string {
  return LEVEL_TITLES[Math.min(level - 1, LEVEL_TITLES.length - 1)];
}

/** Consecutive days with at least one event, counting today (or yesterday, if today is empty). */
export function streakOf(days: string[]): number {
  const set = new Set(days);
  const dayMs = 86_400_000;
  let cursor = Date.now();
  if (!set.has(new Date(cursor).toISOString().slice(0, 10))) cursor -= dayMs; // today still open
  let streak = 0;
  while (set.has(new Date(cursor).toISOString().slice(0, 10))) {
    streak += 1;
    cursor -= dayMs;
  }
  return streak;
}

// ---------------------------------------------------------------- celebration bus
// Modules that cause a celebration (task completion) shouldn't import React components,
// and every window celebrates independently — this tiny pub/sub keeps both true.

let celebrations = 0;
const listeners = new Set<() => void>();

export function celebrate(): void {
  celebrations += 1;
  listeners.forEach((listener) => listener());
}

export function subscribeCelebrations(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function celebrationCount(): number {
  return celebrations;
}

// ---------------------------------------------------------------- the store

type ProgressState = Progress & {
  award: (kind: XpKind) => number;
};

export const useProgress = create<ProgressState>((set, get) => ({
  ...load(),
  award: (kind) => {
    const gained = XP_REWARDS[kind];
    const day = today();
    const days = get().days.includes(day) ? get().days : [...get().days, day];
    const xp = get().xp + gained;
    const next = { xp, days };
    persist(next);
    set(next);
    return gained;
  },
}));
