// The dot's radial menu. The shell opens this window centred on the icon, so the dot itself is the
// hub the three options spring out of:
//
//   chat     the compact dialog a double click on the dot also opens
//   snip     grab a region of the screen; the shell sends it back and raises the dialog to ask about
//   voice    the voice agent — the option ships now, the engine is still being researched
//
// It dismisses itself on a pick, a click anywhere on it (including on the icon at its centre),
// Esc, losing focus, or a few idle seconds.

import { useEffect, useMemo, useRef, useState } from "react";
import { IconChat, IconCrop, IconPhone } from "./Icons";
import { native } from "../services/native";
import { useDobot } from "../store/dobotStore";

type ItemId = "chat" | "snip" | "voice";

const ITEMS: { id: ItemId; label: string; hint: string; Icon: typeof IconChat }[] = [
  { id: "chat", label: "Chat", hint: "Ask Dobot in the quick dialog", Icon: IconChat },
  { id: "snip", label: "Snip", hint: "Snip a piece of the screen and ask about it", Icon: IconCrop },
  { id: "voice", label: "Voice", hint: "Talk to Dobot out loud, on a live call", Icon: IconPhone },
];

/** Nothing chosen: the menu retires by itself so it can never strand an invisible click shield. */
const IDLE_MS = 15_000;
/** A press this soon after the menu opens is the tail of a double-click on the dot, not a miss —
 *  the menu window covers the icon by then, so the dot never sees that second press and the menu
 *  has to honour it on its behalf. It must match the dot's own double-click window (DOUBLE_MS in
 *  DobotDot): any shorter and a normally paced double click flashes the menu open and shut instead
 *  of opening the dialog. */
const DOUBLE_GRACE_MS = 460;

export function DotMenu() {
  const [open, setOpen] = useState(false);
  const openedAt = useRef(Date.now());

  // The arc opens toward the middle of the screen: wherever the dot is parked — a corner, an edge,
  // a second monitor — the options fan into the space the user is actually looking at.
  const base = useMemo(() => {
    const cx = window.screenX + window.innerWidth / 2;
    const cy = window.screenY + window.innerHeight / 2;
    const tx = (window.screen.availWidth || window.screen.width) / 2 - cx;
    const ty = (window.screen.availHeight || window.screen.height) / 2 - cy;
    const angle = (Math.atan2(ty, tx) * 180) / Math.PI;
    return Number.isFinite(angle) ? Math.round(angle) : 180;
  }, []);

  useEffect(() => {
    document.body.classList.add("transparent");
    // Render collapsed first, then spring open — that is what turns the entrance into animation.
    const frame = requestAnimationFrame(() => setOpen(true));
    const idle = window.setTimeout(() => void native.closeMenu(), IDLE_MS);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        void native.closeMenu();
      }
    };
    // Focus went elsewhere (a click on another app): it dismissed the menu as surely as a click on it.
    const onBlur = () => void native.closeMenu();
    window.addEventListener("keydown", onKey);
    window.addEventListener("blur", onBlur);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(idle);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onBlur);
    };
  }, []);

  async function choose(id: ItemId) {
    if (id === "chat") {
      // The shell retires the menu while it raises (or lowers) the dialog.
      await native.togglePanel();
      return;
    }
    if (id === "snip") {
      if (native.isNative) {
        await native.closeMenu();
        // Drag a region: the shell captures it, hands it to every window and opens the dialog
        // with the snip already attached — type the question, press Enter.
        await native.openOverlay();
      } else {
        await native.closeMenu();
        // Browser demo path: the selector draws over this window, as it does in chat.
        useDobot.getState().setCapturing(true);
      }
      return;
    }
    // Voice agent: the shell raises the dialog as the phone — the panel dials the backend from
    // there, so this window only has to get out of the way (open_panel retires the menu itself).
    if (id === "voice") {
      await native.startVoiceCall();
      return;
    }
  }

  return (
    <div
      className={`radial ${open ? "radial--open" : ""}`}
      onMouseDown={(event) => {
        if (event.target instanceof Element && event.target.closest(".radial__item")) return;
        // The second press of a double-click lands here (the menu covers the dot by now): honour it.
        if (Date.now() - openedAt.current < DOUBLE_GRACE_MS) {
          void native.togglePanel();
          return;
        }
        void native.closeMenu();
      }}
      title="Pick one — or click away to dismiss"
    >
      {ITEMS.map((item, index) => (
        <button
          key={item.id}
          className="radial__item"
          style={{ "--i": index, "--angle": `${base + (index - 1) * 45}deg` } as React.CSSProperties}
          // mousedown, not click: the menu window hides itself before a click would ever fire.
          onMouseDown={(event) => {
            event.stopPropagation();
            void choose(item.id);
          }}
          title={item.hint}
          aria-label={item.label}
        >
          <item.Icon size={20} />
          <span className="radial__label">{item.label}</span>
        </button>
      ))}
    </div>
  );
}
