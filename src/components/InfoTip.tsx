import { useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// A small, unobtrusive ⓘ control that explains a complex section.
//
// Hover or keyboard focus shows a compact note; a click pins it open until
// the next click, Escape, or a click elsewhere. Used only for sections that
// are not self-explanatory (layers, telemetry, imagery, change detection,
// search, evidence, confidence, provenance, review, audit...). Simple
// buttons use plain tooltips instead.
//
// Props (all short strings):
//  - title: what the note is about
//  - what:  what it does
//  - data:  what data it uses
//  - state: the current data state (computed by the caller from live data)
//  - limits: important limitations, if any
//
// The note is positioned with `position: fixed` next to the icon, so the
// scrolling panels it sits in cannot cut it off.
// ---------------------------------------------------------------------------
export type InfoContent = {
  title: string;
  what: string;
  data?: string;
  state?: string;
  limits?: string;
};

const NOTE_WIDTH = 280;

export default function InfoTip({ title, what, data, state, limits }: InfoContent) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const buttonRef = useRef<HTMLButtonElement>(null);

  // Place the note below the icon, kept inside the window.
  function place() {
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.min(Math.max(8, rect.left - NOTE_WIDTH / 2), window.innerWidth - NOTE_WIDTH - 8);
    const below = rect.bottom + 6;
    const top = below + 180 > window.innerHeight ? Math.max(8, rect.top - 186) : below;
    setPosition({ left, top });
  }

  function show() {
    place();
    setOpen(true);
  }

  // A pinned note closes on Escape or on any click outside the icon.
  useEffect(() => {
    if (!pinned) return;
    function close(event: Event) {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      if (event.target instanceof Node && buttonRef.current?.contains(event.target)) return;
      setPinned(false);
      setOpen(false);
    }
    window.addEventListener("keydown", close);
    window.addEventListener("mousedown", close);
    return () => {
      window.removeEventListener("keydown", close);
      window.removeEventListener("mousedown", close);
    };
  }, [pinned]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={open ? "info-tip info-tip--open" : "info-tip"}
        aria-label={`About: ${title}`}
        aria-expanded={open}
        onMouseEnter={show}
        onMouseLeave={() => !pinned && setOpen(false)}
        onFocus={show}
        onBlur={() => !pinned && setOpen(false)}
        onClick={(event) => {
          event.stopPropagation();
          if (pinned) {
            setPinned(false);
            setOpen(false);
          } else {
            show();
            setPinned(true);
          }
        }}
      >
        i
      </button>
      {open && (
        <div className="info-note" role="tooltip" style={{ left: position.left, top: position.top, width: NOTE_WIDTH }}>
          <div className="info-note__title">{title}</div>
          <p className="info-note__text">{what}</p>
          {data && <p className="info-note__row"><span>DATA</span>{data}</p>}
          {state && <p className="info-note__row"><span>NOW</span>{state}</p>}
          {limits && <p className="info-note__row info-note__row--limits"><span>LIMITS</span>{limits}</p>}
        </div>
      )}
    </>
  );
}
