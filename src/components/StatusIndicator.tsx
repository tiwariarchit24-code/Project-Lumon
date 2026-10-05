import type { SystemStatus } from "../types";

// ---------------------------------------------------------------------------
// One small status readout: a coloured dot, a label and a value, e.g.
//   ● MODE  AIR-GAPPED
// Used in the top bar and (compact, single line) in the bottom status bar.
// The dot colour comes from the tone: ok (green), neutral (grey),
// pending (amber), error (red). A short CSS transition makes state changes
// noticeable without flashing.
// ---------------------------------------------------------------------------
type StatusIndicatorProps = {
  status: SystemStatus;
  compact?: boolean;
};

export default function StatusIndicator({ status, compact = false }: StatusIndicatorProps) {
  return (
    <div className={compact ? "status status--compact" : "status"} title={`${status.label}: ${status.value}`}>
      <span className={`status-dot status-dot--${status.tone}`} aria-hidden="true" />
      <span className="status__label">{status.label}</span>
      <span className="status__value">{status.value}</span>
    </div>
  );
}
