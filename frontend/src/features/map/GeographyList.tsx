/**
 * The non-map way through the same geography.
 *
 * Not a fallback that appears when the map fails: it is shown alongside the map
 * at all times and carries every action the canvas offers. A `<canvas>` cannot
 * be made properly navigable by keyboard or screen reader, so if selection were
 * only possible by clicking a polygon, the map would be the only way to use the
 * page - and for some users, no way at all.
 *
 * Selection is indicated three ways - a chip, an outline, and `aria-current` -
 * so it never depends on colour alone.
 */

import type { Schemas } from "../../api/client";

type UnitSummary = Schemas["GeographyUnitSummary"];

export interface GeographyListProps {
  units: UnitSummary[];
  selectedUnitId: string | null;
  hoveredUnitId: string | null;
  onSelect: (unitId: string) => void;
  onHover: (unitId: string | null) => void;
  /** What the units are, for the list's accessible name. E.g. "districts". */
  levelLabel: string;
  /** Shown when a unit has children the user can open. */
  canDrill: (unit: UnitSummary) => boolean;
  onDrill: (unit: UnitSummary) => void;
}

export function GeographyList({
  units,
  selectedUnitId,
  hoveredUnitId,
  onSelect,
  onHover,
  levelLabel,
  canDrill,
  onDrill,
}: GeographyListProps) {
  return (
    <ul className="geography-list" aria-label={`${levelLabel} in view`}>
      {units.map((unit) => {
        const isSelected = unit.id === selectedUnitId;
        return (
          <li key={unit.id}>
            <div
              className={
                isSelected
                  ? "geography-list__row geography-list__row--selected"
                  : unit.id === hoveredUnitId
                    ? "geography-list__row geography-list__row--hovered"
                    : "geography-list__row"
              }
            >
              <button
                type="button"
                className="geography-list__select"
                // aria-current carries the selection to assistive technology
                // without relying on the visual treatment.
                aria-current={isSelected ? "true" : undefined}
                onClick={() => onSelect(unit.id)}
                onFocus={() => onHover(unit.id)}
                onBlur={() => onHover(null)}
                onMouseEnter={() => onHover(unit.id)}
                onMouseLeave={() => onHover(null)}
              >
                <span className="geography-list__name">{unit.name}</span>
                <span className="geography-list__code mono">{unit.preferred_code}</span>
              </button>

              {canDrill(unit) ? (
                <button
                  type="button"
                  className="geography-list__drill"
                  onClick={() => onDrill(unit)}
                >
                  Open
                  <span className="visually-hidden">{` ${unit.name}`}</span>
                </button>
              ) : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
