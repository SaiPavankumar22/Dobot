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
        const image = await native.captureRegion(region);
        if (!image) {
          setHint("Capture failed");
          setBusy(false);
          return;
        }
        await native.finishSelection({
          image,
          region,
          application: "",
          window_title: "",
        });
        return;
      }
      const selection = await browserCapture(region);
      if (!selection) {
        setHint("Could not capture the screen in the browser");
        setBusy(false);
        return;
      }
      onDone(selection);
    },
    [onDone],
  );

  return (
    <div
      className="select-overlay"
      onMouseDown={(event) => {
        start.current = { x: event.clientX, y: event.clientY };
        setBox({ x: event.clientX, y: event.clientY, width: 0, height: 0 });
      }}
      onMouseMove={(event) => {
        if (!start.current) return;
        const origin = start.current;
        setBox({
          x: Math.min(origin.x, event.clientX),
          y: Math.min(origin.y, event.clientY),
          width: Math.abs(event.clientX - origin.x),
          height: Math.abs(event.clientY - origin.y),
        });
      }}
      onMouseUp={() => {
        if (box) void finish(box);
        start.current = null;
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
    </div>
  );
}
