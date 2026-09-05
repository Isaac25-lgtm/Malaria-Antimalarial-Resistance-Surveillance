/**
 * SVG administrative map from the same GeoJSON the MapLibre canvas draws.
 *
 * Used when WebGL is missing or MapLibre fails to initialise. Colour is a
 * fill class plus a word in the legend; out-of-scope districts are not links.
 */

import type { MapCollection } from "./geography";
import { FILL_COLOURS, boundsOf, featurePathD, fillClassOf, isInScope, overlayProps, project } from "./geography";
import type { Schemas } from "../../api/client";

const WIDTH = 640;
const HEIGHT = 720;

interface SvgBoundaryMapProps {
  collection: MapCollection;
  bounds: [number, number, number, number] | null;
  metadata?: Schemas["MapMetadataResponse"];
  selectedUnitId: string | null;
  onSelect: (unitId: string) => void;
  label: string;
  facilities?: Schemas["LiveDashboardFacility"][];
}

export function SvgBoundaryMap({
  collection,
  bounds,
  metadata,
  selectedUnitId,
  onSelect,
  label,
  facilities = [],
}: SvgBoundaryMapProps) {
  const extent = bounds ?? boundsOf(collection, metadata) ?? [29.5, -1.5, 35.0, 4.2];

  return (
    <svg
      className="boundary-map boundary-map--svg"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={label}
      data-testid="boundary-svg"
      data-feature-count={collection.features.length}
    >
      <rect width={WIDTH} height={HEIGHT} fill="#eef1f4" />
      {collection.features.map((feature) => {
        const d = featurePathD(feature, extent, WIDTH, HEIGHT);
        if (!d) return null;
        const unitId = feature.properties.unit_id;
        const fillClass = fillClassOf(feature);
        const scoped = isInScope(feature);
        const selected = unitId === selectedUnitId;
        const confirmed = scoped ? overlayProps(feature).confirmed_count : undefined;
        const description = `${feature.properties.name}${typeof confirmed === "number" ? `: ${confirmed.toLocaleString()} reported confirmed malaria cases` : ""}`;
        return (
          <path
            key={unitId}
            d={d}
            fill={typeof confirmed === "number" ? "#93c5e8" : FILL_COLOURS[fillClass] ?? FILL_COLOURS.none}
            stroke={selected ? "#0b6e63" : "#b6c2bf"}
            strokeWidth={selected ? 2.2 : 0.7}
            data-unit-id={unitId}
            data-in-scope={scoped ? "true" : "false"}
            data-fill-class={fillClass}
            aria-label={description}
            tabIndex={scoped ? 0 : -1}
            role={scoped ? "button" : "presentation"}
            onClick={() => {
              if (scoped) onSelect(unitId);
            }}
            onKeyDown={(event) => {
              if (!scoped) return;
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(unitId);
              }
            }}
          ><title>{description}</title></path>
        );
      })}
      {facilities.map((facility) => {
        if (facility.latitude == null || facility.longitude == null) return null;
        if (!Number.isFinite(facility.latitude) || !Number.isFinite(facility.longitude)) return null;
        if (facility.longitude < extent[0] || facility.longitude > extent[2] || facility.latitude < extent[1] || facility.latitude > extent[3]) return null;
        const [x, y] = project(facility.longitude, facility.latitude, extent, WIDTH, HEIGHT);
        const stockOut =
          (facility.rdt_days_out_of_stock ?? 0) > 0 ||
          (facility.al_days_out_of_stock ?? 0) > 0 ||
          (facility.artesunate_days_out_of_stock ?? 0) > 0;
        return (
          <g key={facility.uid} aria-label={facility.name}>
            <circle
              cx={x}
              cy={y}
              r={stockOut ? 6 : 4.5}
              fill={stockOut ? "#dc2626" : "#1976b9"}
              stroke="#ffffff"
              strokeWidth={1.8}
            >
              <title>
                {facility.name}
                {stockOut ? " — stock-out reported" : " — reporting facility"}
              </title>
            </circle>
          </g>
        );
      })}
    </svg>
  );
}
