import type { NavGroup } from "../types";

// ---------------------------------------------------------------------------
// The navigation panel on the left edge.
//
// Two modes:
//   expanded  (~248 px) readable labels grouped under COMMAND, OSINT, ...
//   collapsed (~48 px)  compact two-letter codes; the label is the tooltip
// The toggle at the bottom switches between them. Clicking an entry opens
// its drawer section over the map; clicking the active entry closes it.
//
// Props:
//  - groups:   navigation groups (data/navigation.ts)
//  - activeId: the open section, or null when the drawer is closed
//  - expanded: which mode to show
//  - onSelect: called with the clicked section id
//  - onToggleExpanded: switch between expanded and collapsed
// ---------------------------------------------------------------------------
type LeftNavProps = {
  groups: NavGroup[];
  activeId: string | null;
  expanded: boolean;
  onSelect: (id: string) => void;
  onToggleExpanded: () => void;
};

export default function LeftNav({ groups, activeId, expanded, onSelect, onToggleExpanded }: LeftNavProps) {
  return (
    <nav className={expanded ? "rail rail--expanded" : "rail"} aria-label="Main navigation">
      <div className="rail__scroll">
        {groups.map((group) => (
          <div className="rail__group" key={group.title}>
            <div className="rail__group-title" aria-hidden={!expanded}>{expanded ? group.title : group.title.slice(0, 4)}</div>
            {group.items.map((item) => {
              const active = item.id === activeId;
              return (
                <button
                  key={item.id}
                  type="button"
                  className={active ? "rail__item rail__item--active" : "rail__item"}
                  onClick={() => onSelect(item.id)}
                  aria-pressed={active}
                  aria-label={item.label}
                  title={expanded ? undefined : item.label}
                >
                  <span className="rail__code" aria-hidden="true">{item.code}</span>
                  {expanded && <span className="rail__label">{item.label}</span>}
                </button>
              );
            })}
          </div>
        ))}
      </div>

      <button
        type="button"
        className="rail__toggle"
        onClick={onToggleExpanded}
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse navigation" : "Expand navigation"}
        title={expanded ? "Collapse navigation" : "Expand navigation"}
      >
        <span aria-hidden="true">{expanded ? "«" : "»"}</span>
        {expanded && <span className="rail__toggle-label">COLLAPSE</span>}
      </button>
    </nav>
  );
}
