/**
 * Geography canvas: MapLibre when WebGL works, SVG from the same GeoJSON otherwise.
 *
 * SVG stays visible until MapLibre has applied the FeatureCollection. A lazy
 * MapLibre mount must never replace a drawn map with an empty canvas.
 */

import { Suspense, lazy, useState } from "react";

import { SvgBoundaryMap } from "./SvgBoundaryMap";
import { canUseWebGL, type MapCollection } from "./geography";
import type { Schemas } from "../../api/client";
import "./map.css";

const BoundaryMap = lazy(() =>
  import("./BoundaryMap").then((module) => ({ default: module.BoundaryMap })),
);

interface GeographyCanvasProps {
  collection: MapCollection | null;
  bounds: [number, number, number, number] | null;
  metadata?: Schemas["MapMetadataResponse"];
  selectedUnitId: string | null;
  hoveredUnitId: string | null;
  onSelect: (unitId: string) => void;
  onHover: (unitId: string | null) => void;
  label: string;
  forceSvg?: boolean;
  facilities?: Schemas["LiveDashboardFacility"][];
}

export function GeographyCanvas({
  collection,
  bounds,
  metadata,
  selectedUnitId,
  hoveredUnitId,
  onSelect,
  onHover,
  label,
  forceSvg = false,
  facilities = [],
}: GeographyCanvasProps) {
  const [engine, setEngine] = useState<"maplibre" | "svg">(() =>
    forceSvg || !canUseWebGL() ? "svg" : "maplibre",
  );
  const [mapReady, setMapReady] = useState(false);

  if (!collection || collection.features.length === 0) {
    return (
      <div className="boundary-map boundary-map--empty" data-testid="boundary-pending" role="status">
        Loading geography
      </div>
    );
  }

  const svg = (
    <SvgBoundaryMap
      collection={collection}
      bounds={bounds}
      metadata={metadata}
      selectedUnitId={selectedUnitId}
      onSelect={onSelect}
      label={label}
      facilities={facilities}
    />
  );

  if (engine === "svg") {
    return svg;
  }

  return (
    <div className="geography-canvas" data-map-engine="maplibre" data-map-ready={mapReady ? "true" : "false"}>
      {!mapReady ? svg : null}
      <div className={mapReady ? "geography-canvas__map is-ready" : "geography-canvas__map"}>
        <Suspense fallback={null}>
          <BoundaryMap
            collection={collection}
            bounds={bounds}
            selectedUnitId={selectedUnitId}
            hoveredUnitId={hoveredUnitId}
            onSelect={onSelect}
            onHover={onHover}
            label={label}
            onReady={() => setMapReady(true)}
            onEngineFailure={() => {
              setMapReady(false);
              setEngine("svg");
            }}
          />
        </Suspense>
      </div>
    </div>
  );
}
