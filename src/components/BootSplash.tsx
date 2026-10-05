import { useEffect, useState } from "react";

// ---------------------------------------------------------------------------
// The Project Lumon start-up sequence (~900 ms), drawn OVER the app.
//
// The workstation (map, data requests) is mounted underneath at the same
// time, so start-up never waits for this animation. The overlay ignores the
// mouse and removes itself when done.
//
// It builds the real logo (public/assets/project-lumon-logo.png); nothing
// is redesigned. Coordinates below are in the logo image's own pixel frame
// (1622 x 969), measured from the asset:
//   outer wireframe ellipse  centre (806, 467), radii 766 x 392
//   the globe in the "O"     centre (1055, 500), radius ~100 (contains India)
//
// Timeline (CSS keyframes in styles/boot.css):
//   0-380 ms    wireframe lines and points draw on a black background
//   240-520 ms  the globe with India resolves inside the "O"
//   500-580 ms  a short, restrained white flash
//   520-800 ms  the full logo (PROJECT above LUMON) resolves outward
//   760-950 ms  the overlay fades, revealing LUMON COMMAND
//
// prefers-reduced-motion: no construction; the logo is shown briefly and
// the overlay is gone after ~150 ms.
// ---------------------------------------------------------------------------

const LOGO_URL = "/assets/project-lumon-logo.png";
const FULL_MS = 950;
const REDUCED_MS = 150;

// Faint construction points scattered along the wireframe (a fixed set, so
// every start looks the same).
const POINTS: [number, number][] = [
  [806, 75], [806, 859], [40, 467], [1572, 467], [330, 170], [1282, 170], [330, 764], [1282, 764],
  [560, 222], [1052, 222], [560, 712], [1052, 712], [806, 222], [806, 712],
];

export default function BootSplash() {
  const [done, setDone] = useState(false);
  const reduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  useEffect(() => {
    const timer = window.setTimeout(() => setDone(true), reduced ? REDUCED_MS : FULL_MS);
    return () => window.clearTimeout(timer);
  }, [reduced]);

  if (done) return null;

  return (
    <div className={reduced ? "boot boot--reduced" : "boot"} aria-hidden="true">
      <div className="boot__stage">
        {/* Wireframe construction lines in the logo's own coordinate frame. */}
        <svg className="boot__wire" viewBox="0 0 1622 969" preserveAspectRatio="xMidYMid meet">
          <ellipse className="boot__line boot__line--1" cx="806" cy="467" rx="766" ry="392" pathLength="1" />
          <ellipse className="boot__line boot__line--2" cx="806" cy="467" rx="470" ry="392" pathLength="1" />
          <ellipse className="boot__line boot__line--3" cx="806" cy="467" rx="215" ry="392" pathLength="1" />
          <line className="boot__line boot__line--4" x1="190" y1="222" x2="1422" y2="222" pathLength="1" />
          <line className="boot__line boot__line--4" x1="190" y1="712" x2="1422" y2="712" pathLength="1" />
          <circle className="boot__line boot__line--o" cx="1055" cy="500" r="100" pathLength="1" />
          {POINTS.map(([x, y], i) => (
            <circle key={i} className="boot__point" cx={x} cy={y} r="5" style={{ animationDelay: `${40 + i * 14}ms` }} />
          ))}
        </svg>
        {/* The real logo, revealed first through a small circle around the
            India globe in the "O", then fully. */}
        <img className="boot__logo" src={LOGO_URL} alt="" decoding="async" />
        <div className="boot__flash" />
      </div>
    </div>
  );
}
