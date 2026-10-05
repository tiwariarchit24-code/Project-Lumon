import { useState } from "react";
import type { FormEvent } from "react";

// ---------------------------------------------------------------------------
// Floating map control rail on the right edge of the map.
//
// Buttons: zoom in/out, compass (reset north), reset view to India,
// flat/globe projection, measure distance, reference grid on/off, and a
// "go to coordinates" box (the offline replacement for a "locate me"
// button, which would need an online location service).
// All actions are callbacks; the map itself lives in MapWorkspace.
// ---------------------------------------------------------------------------
type MapControlsProps = {
  projection: "mercator" | "globe";
  bearing: number;
  measuring: boolean;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetNorth: () => void;
  onResetView: () => void;
  onToggleProjection: () => void;
  onToggleMeasure: () => void;
  onToggleGrid: (visible: boolean) => void;
  onGoTo: (lon: number, lat: number) => void;
};

export default function MapControls(props: MapControlsProps) {
  const [gridVisible, setGridVisible] = useState(true);
  const [goToOpen, setGoToOpen] = useState(false);
  const [goToText, setGoToText] = useState("");
  const [goToError, setGoToError] = useState<string | null>(null);

  // Accepts "lat, lon" in decimal degrees, e.g. "18.99, 73.07".
  function submitGoTo(event: FormEvent) {
    event.preventDefault();
    const parts = goToText.split(/[ ,]+/).map(Number);
    if (parts.length !== 2 || parts.some(Number.isNaN) || Math.abs(parts[0]) > 90 || Math.abs(parts[1]) > 180) {
      setGoToError("Enter: lat, lon");
      return;
    }
    setGoToError(null);
    props.onGoTo(parts[1], parts[0]);
  }

  return (
    <div className="map-rail" role="toolbar" aria-label="Map controls">
      <div className="map-rail__group">
        <button type="button" className="map-rail__button" onClick={props.onZoomIn} aria-label="Zoom in" title="Zoom in">+</button>
        <button type="button" className="map-rail__button" onClick={props.onZoomOut} aria-label="Zoom out" title="Zoom out">−</button>
        <button type="button" className="map-rail__button" onClick={props.onResetNorth} aria-label="Reset bearing to north" title="Reset north">
          {/* The arrow turns with the map so it always points north. */}
          <span className="map-rail__compass" style={{ transform: `rotate(${-props.bearing}deg)` }} aria-hidden="true">▲</span>
        </button>
        <button type="button" className="map-rail__button map-rail__button--text" onClick={props.onResetView} aria-label="Reset view to India" title="Reset view to India">IN</button>
      </div>

      <div className="map-rail__group">
        <button
          type="button"
          className={props.projection === "globe" ? "map-rail__button map-rail__button--text map-rail__button--active" : "map-rail__button map-rail__button--text"}
          onClick={props.onToggleProjection}
          aria-pressed={props.projection === "globe"}
          title={props.projection === "globe" ? "Switch to flat map" : "Switch to globe"}
        >
          {props.projection === "globe" ? "3D" : "2D"}
        </button>
        <button
          type="button"
          className={props.measuring ? "map-rail__button map-rail__button--text map-rail__button--active" : "map-rail__button map-rail__button--text"}
          onClick={props.onToggleMeasure}
          aria-pressed={props.measuring}
          title="Measure distance"
        >
          MS
        </button>
        <button
          type="button"
          className={gridVisible ? "map-rail__button map-rail__button--text map-rail__button--active" : "map-rail__button map-rail__button--text"}
          onClick={() => {
            props.onToggleGrid(!gridVisible);
            setGridVisible(!gridVisible);
          }}
          aria-pressed={gridVisible}
          title="Reference grid"
        >
          GR
        </button>
        <button
          type="button"
          className={goToOpen ? "map-rail__button map-rail__button--text map-rail__button--active" : "map-rail__button map-rail__button--text"}
          onClick={() => setGoToOpen(!goToOpen)}
          aria-expanded={goToOpen}
          title="Go to coordinates"
        >
          GO
        </button>
      </div>

      {goToOpen && (
        <form className="map-goto" onSubmit={submitGoTo}>
          <label htmlFor="goto-input" className="map-goto__label">GO TO LAT, LON</label>
          <input id="goto-input" className="map-goto__input" value={goToText} onChange={(e) => setGoToText(e.target.value)} placeholder="18.99, 73.07" autoFocus />
          {goToError && <span className="map-goto__error">{goToError}</span>}
        </form>
      )}
    </div>
  );
}
