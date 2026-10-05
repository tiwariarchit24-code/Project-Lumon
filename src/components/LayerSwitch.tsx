// ---------------------------------------------------------------------------
// The ON/OFF switch for one map layer.
//
// ON:  filled accent track, knob on the right, the word "ON".
// OFF: empty dark track, grey knob on the left, the word "OFF".
// The text means the state is readable without relying on colour or on
// the knob position. It is a real switch for screen readers (role="switch",
// aria-checked) and works with Tab + Space/Enter like any button.
//
// Props:
//  - name:     layer name, used in the tooltip and accessible label
//  - on:       whether the layer is currently drawn
//  - onToggle: flip it
// ---------------------------------------------------------------------------
type LayerSwitchProps = { name: string; on: boolean; onToggle: () => void };

export default function LayerSwitch({ name, on, onToggle }: LayerSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={`${name} layer`}
      title={on ? `${name} is ON — click to hide it` : `${name} is OFF — click to show it`}
      className={on ? "switch switch--on" : "switch"}
      onClick={(e) => {
        e.stopPropagation(); // never also select the row
        onToggle();
      }}
    >
      <span className="switch__knob" aria-hidden="true" />
      <span className="switch__text" aria-hidden="true">{on ? "ON" : "OFF"}</span>
    </button>
  );
}
