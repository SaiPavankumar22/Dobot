// Region selection overlay. Covers the screen, lets the user drag a box, then captures exactly that
// rectangle. Native path captures in Rust (xcap); browser path crops a shared display.

import { useCallback, useEffect, useRef, useState } from "react";
import { native, type SelectionPayload } from "../services/native";
import { browserCapture, type Selection } from "../services/screen";

interface Props {
  onDone: (selection: SelectionPayload | Selection) => void;
  onCancel: () => void;
}

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function ScreenSelector({ onDone, onCancel }: Props) {
  const [box, setBox] = useState<Box | null>(null);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState("Drag to select a region · Esc to cancel");
  const start = useRef<{ x: number; y: number } | null>(null);
  const boxRef = useRef<Box | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const finish = useCallback(
    async (region: Box) => {
      if (region.width < 8 || region.height < 8) {
        setHint("Selection too small — drag a larger area");
        setBox(null);
        return;
      }
      setBusy(true);
      setHint("Capturing…");
      if (native.isNative) {
        try {
          const image = await native.captureRegion(region);
          if (!image) {
            throw new Error("Capture failed");
          }
          await native.finishSelection({
            image,
            region,
            application: "",
            window_title: "",
          });
        } catch (error) {
          setHint(error instanceof Error ? error.message : "Capture failed");
          setBusy(false);
          boxRef.current = null;
          setBox(null);
          onCancel();
        }
        return;
      }
      try {
        const selection = await browserCapture(region);
        if (!selection) {
          throw new Error("Could not capture the screen in the browser");
        }
        onDone(selection);
      } catch (error) {
        setHint(error instanceof Error ? error.message : "Capture failed");
        setBusy(false);
        boxRef.current = null;
        setBox(null);
        onCancel();
      }
    },
    [onCancel, onDone],
  );

  return (
    <div
      className="select-overlay"
      onMouseDown={(event) => {
        if (busy) return;
        start.current = { x: event.clientX, y: event.clientY };
        const next = { x: event.clientX, y: event.clientY, width: 0, height: 0 };
        boxRef.current = next;
        setBox(next);
      }}
      onMouseMove={(event) => {
        if (!start.current || busy) return;
        const origin = start.current;
        const next = {
          x: Math.min(origin.x, event.clientX),
          y: Math.min(origin.y, event.clientY),
          width: Math.abs(event.clientX - origin.x),
          height: Math.abs(event.clientY - origin.y),
        };
        boxRef.current = next;
        setBox(next);
      }}
      onMouseUp={() => {
        const selected = boxRef.current;
        if (selected && !busy) void finish(selected);
        start.current = null;
        boxRef.current = null;
      }}
    >
      <div className="select-overlay__hint">{hint}</div>
      {box && (
        <div
          className="select-overlay__box"
          style={{ left: box.x, top: box.y, width: box.width, height: box.height }}
        />
      )}
      {busy && (
        <div className="select-overlay__hint" style={{ top: "auto", bottom: 24 }}>
          Sending to Dobot…
        </div>
      )}
      <button
        className="select-overlay__cancel"
        onMouseDown={(event) => event.stopPropagation()}
        onClick={onCancel}
        type="button"
      >
        Cancel (Esc)
      </button>
    </div>
  );
}
