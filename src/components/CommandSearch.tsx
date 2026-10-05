import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { getJson, postJson, query } from "../api";
import type { QueryPlan } from "../types";

// ---------------------------------------------------------------------------
// The command / search field in the top bar.
//
// While typing, it shows matching places from the local gazetteer (cities,
// states, districts). Pressing Enter sends the sentence to the backend's
// deterministic parser, which returns an editable QUERY PLAN; the plan opens
// in the Search drawer where the analyst can check and edit it before
// running. Nothing is sent to any online service.
// ---------------------------------------------------------------------------
type Place = { name: string; kind: string; lon: number; lat: number; bbox?: number[] | null; state?: string | null };

type CommandSearchProps = {
  apiOnline: boolean;
  onPlace: (place: Place) => void;
  onPlan: (plan: QueryPlan) => void;
};

// Example questions shown when the field is empty. They are suggestions only.
const EXAMPLES = [
  "Show newly constructed areas near rivers",
  "Show recent earthquakes near Delhi",
  "Show aircraft near Mumbai",
  "Show power plants in Gujarat",
  "Show water expansion since January",
  "Find vessels near the western coast",
];

export default function CommandSearch({ apiOnline, onPlace, onPlan }: CommandSearchProps) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [places, setPlaces] = useState<Place[]>([]);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Keyboard shortcut: "/" focuses the search field from anywhere,
  // unless the analyst is already typing in another field.
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (event.key === "/" && target.tagName !== "INPUT" && target.tagName !== "TEXTAREA") {
        event.preventDefault();
        inputRef.current?.focus();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  // Look up places as the analyst types (short pause first, so we do not
  // send a request for every single key press).
  useEffect(() => {
    if (text.trim().length < 3 || !apiOnline) {
      setPlaces([]);
      return;
    }
    const timer = window.setTimeout(() => {
      getJson<Place[]>(`/api/places/search${query({ q: text.trim(), limit: 5 })}`)
        .then(setPlaces)
        .catch(() => setPlaces([]));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [text, apiOnline]);

  // Enter: parse the sentence into a plan.
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!text.trim()) return;
    try {
      const plan = await postJson<QueryPlan>("/api/query/parse", { text: text.trim() });
      setError(null);
      onPlan(plan);
      inputRef.current?.blur();
    } catch {
      setError("Query engine unavailable: Lumon API unreachable.");
    }
  }

  function choosePlace(place: Place) {
    onPlace(place);
    setText(place.name);
    inputRef.current?.blur();
  }

  return (
    <form className={focused ? "search search--focused" : "search"} role="search" onSubmit={submit}>
      <label htmlFor="command-search" className="visually-hidden">Search places or ask a question</label>
      <span className="search__prompt" aria-hidden="true">&gt;</span>
      <input
        id="command-search"
        ref={inputRef}
        className="search__input"
        type="text"
        autoComplete="off"
        spellCheck={false}
        placeholder="Search places, events, changes, sites… or ask a question"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          setError(null);
        }}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
      />
      <kbd className="search__shortcut" aria-hidden="true">/</kbd>

      {focused && (
        <div className="search__dropdown">
          {error && <div className="search__notice">{error}</div>}

          {places.length > 0 && (
            <>
              <div className="search__dropdown-header"><span>PLACES</span><span>LOCAL GAZETTEER</span></div>
              <ul className="search__list">
                {places.map((place) => (
                  <li key={`${place.kind}-${place.name}`}>
                    <button type="button" className="search__item" onMouseDown={(event) => { event.preventDefault(); choosePlace(place); }}>
                      <span className="search__item-kind">{place.kind.toUpperCase()}</span>
                      <span>{place.name}</span>
                      {place.state && <span className="search__item-note">{place.state}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}

          {text.trim() && (
            <button type="button" className="search__item search__item--run" onMouseDown={(event) => { event.preventDefault(); submit(event as unknown as FormEvent); }}>
              <span className="search__item-kind">PLAN</span>
              <span>Interpret “{text.trim()}” as a question</span>
              <kbd className="search__item-note">ENTER</kbd>
            </button>
          )}

          {!text.trim() && (
            <>
              <div className="search__dropdown-header"><span>EXAMPLE QUESTIONS</span><span>DETERMINISTIC PARSER</span></div>
              <ul className="search__list">
                {EXAMPLES.map((example) => (
                  <li key={example}>
                    <button type="button" className="search__item" onMouseDown={(event) => { event.preventDefault(); setText(example); }}>
                      <span className="search__item-kind">›</span>
                      <span>{example}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
          <div className="search__dropdown-footer">
            Questions become an editable plan before anything runs. No language model is used.
          </div>
        </div>
      )}
    </form>
  );
}
