// Inline icons. A dependency-free set on purpose: the app ships inside a 2 MB installer, and a
// dozen paths are not worth an npm package plus its tree-shaking risk.

type IconProps = { size?: number; className?: string };

function Svg({ size = 18, className, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export function IconPaperclip(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21.4 11.05 12.3 20.1a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.9 1.9 0 0 1-2.6-2.6l7.8-7.8" />
    </Svg>
  );
}

export function IconMic(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="9" y="2.5" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3.5" />
    </Svg>
  );
}

export function IconStop(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="6.5" y="6.5" width="11" height="11" rx="2.2" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function IconSend(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 19.5v-15M5.5 11 12 4.5 18.5 11" />
    </Svg>
  );
}

export function IconScreen(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.5 6.5A2 2 0 0 1 5.5 4.5h13a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />
      <path d="M9 20h6" />
    </Svg>
  );
}

/** Showing Dobot the window you are in — the Look chord's button. */
export function IconEye(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.5 12S6.2 5.8 12 5.8 21.5 12 21.5 12 17.8 18.2 12 18.2 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="2.7" />
    </Svg>
  );
}

/** The workspace footer button's icon — replacing the 🗂 emoji that rendered as an unrecognizable mush. */
export function IconGrid(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.6" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.6" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.6" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="1.6" />
    </Svg>
  );
}

export function IconMoon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z" />
    </Svg>
  );
}

export function IconBook(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 5.5A2 2 0 0 1 6 3.5h13v17H6a2 2 0 0 0-2 2z" />
      <path d="M4 5.5v13" />
    </Svg>
  );
}

export function IconSearch(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4.5 4.5" />
    </Svg>
  );
}

export function IconClose(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6 6l12 12M18 6 6 18" />
    </Svg>
  );
}

export function IconImage(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <circle cx="9" cy="10" r="1.6" />
      <path d="m4.5 17.5 4.6-4.6a2 2 0 0 1 2.8 0l5.1 5.1" />
    </Svg>
  );
}

export function IconFile(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M13.5 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-9z" />
      <path d="M13.5 3.5v6h5.5" />
    </Svg>
  );
}

export function IconStopCircle(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <rect x="9.3" y="9.3" width="5.4" height="5.4" rx="1.2" fill="currentColor" stroke="none" />
    </Svg>
  );
}

/** The radial menu's Chat option — the quick dialog. */
export function IconChat(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3.5" y="5" width="17" height="12" rx="3" />
      <path d="M8 17v3.2L12.2 17" />
    </Svg>
  );
}

/** The radial menu's Snip option — cut a piece out of the screen. */
export function IconCrop(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="6" cy="6.5" r="2.5" />
      <circle cx="6" cy="17.5" r="2.5" />
      <path d="M8.2 7.8 20 18M8.2 16.2 20 6" />
    </Svg>
  );
}

/** The radial menu's Voice option — talk to Dobot out loud. */
export function IconPhone(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6.5 3.5h3l1.5 4-2 1.5a12 12 0 0 0 6 6l1.5-2 4 1.5v3a2 2 0 0 1-2 2A16.5 16.5 0 0 1 4.5 5.5a2 2 0 0 1 2-2z" />
    </Svg>
  );
}
