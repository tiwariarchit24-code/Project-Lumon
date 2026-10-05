import { useState } from "react";

// Where the official logo image lives. Files in /public are served from the
// site root, so public/assets/project-lumon-logo.png becomes this URL.
const LOGO_URL = "/assets/project-lumon-logo.png";

// ---------------------------------------------------------------------------
// Project Lumon brand mark for the top bar.
//
// Uses the official logo file when it exists. The logo artwork is white on
// black; `mix-blend-mode: screen` (in the CSS) makes the black background
// blend into the dark top bar without editing the image. If the file is
// missing, a plain text placeholder is shown instead (never a fake logo).
// ---------------------------------------------------------------------------
export default function Logo() {
  const [imageMissing, setImageMissing] = useState(false);

  return (
    <div className="logo">
      {imageMissing ? (
        <div className="logo__placeholder" aria-label="Project Lumon">
          <span>PROJECT</span>
          <span className="logo__placeholder-strong">LUMON</span>
        </div>
      ) : (
        <img className="logo__image" src={LOGO_URL} alt="Project Lumon" onError={() => setImageMissing(true)} />
      )}
      <div className="logo__divider" aria-hidden="true" />
      <span className="logo__product">LUMON COMMAND</span>
    </div>
  );
}
