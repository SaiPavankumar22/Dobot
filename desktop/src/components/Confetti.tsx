import { useEffect, useState } from "react";
import { celebrationCount, subscribeCelebrations } from "../services/gamification";

// A lightweight confetti burst, fired by the celebration bus in services/gamification.
// Purely CSS-animated spans — no canvas, no dependency, and every window renders its own.

const COLORS = ["#7c5cff", "#4cc3ff", "#ffd166", "#6ee7a8", "#ff7aa2"];

type Piece = { left: number; delay: number; color: string; width: number; height: number; drift: number; spin: number };

function makePieces(): Piece[] {
  return Array.from({ length: 28 }, () => ({
    left: 20 + Math.random() * 60,
    delay: Math.random() * 0.25,
    color: COLORS[Math.floor(Math.random() * COLORS.length)],
    width: 6 + Math.random() * 6,
    height: 4 + Math.random() * 5,
    drift: (Math.random() - 0.5) * 220,
    spin: 360 + Math.random() * 540,
  }));
}

export function Confetti() {
  const [burst, setBurst] = useState<{ id: number; pieces: Piece[] } | null>(null);

  useEffect(
    () =>
      subscribeCelebrations(() => {
        setBurst({ id: celebrationCount(), pieces: makePieces() });
      }),
    [],
  );

  useEffect(() => {
    if (!burst) return;
    const timer = window.setTimeout(() => setBurst(null), 1900);
    return () => window.clearTimeout(timer);
  }, [burst]);

  if (!burst) return null;
  return (
    <div className="confetti" key={burst.id} aria-hidden="true">
      {burst.pieces.map((piece, index) => (
        <span
          key={index}
          className="confetti__piece"
          style={{
            left: `${piece.left}%`,
            background: piece.color,
            width: `${piece.width}px`,
            height: `${piece.height}px`,
            animationDelay: `${piece.delay}s`,
            ["--confetti-drift" as string]: `${piece.drift}px`,
            ["--confetti-spin" as string]: `${piece.spin}deg`,
          }}
        />
      ))}
    </div>
  );
}
