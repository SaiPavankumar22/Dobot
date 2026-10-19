import { levelOf, levelStart, levelTitle, streakOf, useProgress } from "../services/gamification";

// The user's progress at a glance: level badge, XP bar, daily streak. Sits at the top of
// Overview — the dashboard's landing section. All numbers come from the local progress
// store; nothing here talks to the backend.

export function LevelCard() {
  const xp = useProgress((state) => state.xp);
  const days = useProgress((state) => state.days);

  const level = levelOf(xp);
  const into = xp - levelStart(level);
  const span = levelStart(level + 1) - levelStart(level);
  const pct = Math.max(3, Math.min(100, Math.round((into / span) * 100)));
  const streak = streakOf(days);

  return (
    <div className="level-card">
      <div className="level-card__badge" title={`Level ${level}`}>
        {level}
      </div>
      <div className="level-card__body">
        <div className="level-card__title">
          <strong>{levelTitle(level)}</strong>
          <span className="subtle"> · {xp} XP</span>
          {streak > 0 && (
            <span className="level-card__streak" title="Days in a row with progress">
              🔥 {streak}-day streak
            </span>
          )}
        </div>
        <div className="xp-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="xp-bar__fill" style={{ width: `${pct}%` }} />
        </div>
        <div className="subtle level-card__hint">
          {span - into} XP to {levelTitle(level + 1)} · finished tasks and completed research earn XP
        </div>
      </div>
    </div>
  );
}
