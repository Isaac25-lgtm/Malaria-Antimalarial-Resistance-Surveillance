/**
 * MapLibre canvas for MARS boundary geometry.
 *
 * Deliberate decisions this component encodes:
 *
 * **No external basemap.** There is no tile provider, no attribution to a third
 * party, and no request leaving the browser for anything but the MARS API. A
 * basemap would send the viewport - and so, by inference, which district an
 * officer is looking at - to whoever serves the tiles. Uganda is drawn from
 * MARS's own boundaries on a neutral ground, which is all a boundary map needs.
 *
 * **No fabricated values.** Polygons are filled with one neutral colour. There
 * is no choropleth, because MARS has no indicator to colour by yet, and a
 * gradient that means nothing is worse than no gradient at all.
 *
 * **The canvas is not the only way in.** Selection is driven from props, and
 * every selection the map can make is also available from the list beside it.
 * A `<canvas>` cannot be made properly keyboard-navigable, so the list is the
 * keyboard interface rather than an afterthought.
 */

// MapLibre 6 publishes named exports only; there is no default export to import.
import { MapLibreMap, NavigationControl } from "maplibre-gl";
import type { GeoJSONSource, MapLayerMouseEvent } from "maplibre-gl";
import type { FeatureCollection as GeoJsonFeatureCollection } from "geojson";
import { useEffect, useRef } from "react";

import type { Schemas } from "../../api/client";

import "maplibre-gl/dist/maplibre-gl.css";

type FeatureCollection = Schemas["MapFeatureCollection"];

const SOURCE_ID = "mars-boundaries";
const FILL_LAYER = "mars-boundaries-fill";
const LINE_LAYER = "mars-boundaries-line";
const SELECTED_LAYER = "mars-boundaries-selected";

/**
 * The unit id carried by the feature under the pointer, if there is one.
 *
 * MapLibre types feature properties as `any`, so this is the single place that
 * value is narrowed. Everything downstream deals in `string | null`.
 */
function unitIdOf(event: MapLayerMouseEvent): string | null {
  const raw: unknown = event.features?.[0]?.properties?.unit_id;
  return typeof raw === "string" ? raw : null;
}

/** Read a design token, so the map cannot drift from the rest of the interface. */
function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return value.trim() || fallback;
}

export interface BoundaryMapProps {
  collection: FeatureCollection | null;
  /** Extent to fit, as west, south, east, north. */
  bounds: [number, number, number, number] | null;
  selectedUnitId: string | null;
  hoveredUnitId: string | null;
  onSelect: (unitId: string) => void;
  onHover: (unitId: string | null) => void;
  /** Announced to assistive technology as the map region's name. */
  label: string;
}

export function BoundaryMap({
  collection,
  bounds,
  selectedUnitId,
  hoveredUnitId,
  onSelect,
  onHover,
  label,
}: BoundaryMapProps) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  const ready = useRef(false);

  // Handlers are held in refs so the map is built once. Rebuilding it on every
  // render would refit the viewport and throw away the user's pan and zoom.
  const selectRef = useRef(onSelect);
  const hoverRef = useRef(onHover);
  selectRef.current = onSelect;
  hoverRef.current = onHover;

  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new MapLibreMap({
      container: container.current,
      // An empty style: no sources, no sprite, no glyphs, nothing fetched.
      style: {
        version: 8,
        sources: {},
        layers: [
          {
            id: "background",
            type: "background",
            paint: { "background-color": token("--surface-sunken", "#eef1f0") },
          },
        ],
      },
      center: [32.5, 1.4],
      zoom: 5.6,
      attributionControl: false,
      // Rotation carries no meaning on an administrative map and makes the
      // north-up reading of a boundary ambiguous.
      dragRotate: false,
      pitchWithRotate: false,
      touchZoomRotate: false,
    });

    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");
    instance.on("load", () => {
      ready.current = true;
      instance.addSource(SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
        promoteId: "unit_id",
      });

      instance.addLayer({
        id: FILL_LAYER,
        type: "fill",
        source: SOURCE_ID,
        paint: {
          "fill-color": [
            "case",
            ["boolean", ["feature-state", "selected"], false],
            token("--accent", "#0b6e63"),
            ["boolean", ["feature-state", "hovered"], false],
            token("--accent-soft", "#e4f0ed"),
            token("--surface-raised", "#ffffff"),
          ],
          "fill-opacity": 1,
        },
      });

      instance.addLayer({
        id: LINE_LAYER,
        type: "line",
        source: SOURCE_ID,
        paint: {
          "line-color": token("--line-strong", "#b6c2bf"),
          "line-width": 0.7,
        },
      });

      // Selection is drawn as a heavier outline as well as a fill change, so
      // it survives greyscale and does not depend on colour alone.
      instance.addLayer({
        id: SELECTED_LAYER,
        type: "line",
        source: SOURCE_ID,
        filter: ["boolean", ["feature-state", "selected"], false],
        paint: {
          "line-color": token("--accent", "#0b6e63"),
          "line-width": 2.5,
        },
      });

      instance.on("click", FILL_LAYER, (event: MapLayerMouseEvent) => {
        const unitId = unitIdOf(event);
        if (unitId) selectRef.current(unitId);
      });

      instance.on("mousemove", FILL_LAYER, (event: MapLayerMouseEvent) => {
        instance.getCanvas().style.cursor = "pointer";
        hoverRef.current(unitIdOf(event));
      });

      instance.on("mouseleave", FILL_LAYER, () => {
        instance.getCanvas().style.cursor = "";
        hoverRef.current(null);
      });
    });

    map.current = instance;

    return () => {
      ready.current = false;
      instance.remove();
      map.current = null;
    };
  }, []);

  // Feed the source whenever the collection changes.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !collection) return;

    const apply = () => {
      const source: GeoJSONSource | undefined = instance.getSource(SOURCE_ID);
      if (!source) return;
      // The API guarantees GeoJSON geometry; the generated schema types it as a
      // permissive object, so this is the one place the two are reconciled.
      // setData resolves once the worker has parsed the data. Nothing here
      // depends on that, and a rejection would already surface as a map error
      // event, so the promise is explicitly discarded rather than awaited.
      void source.setData({
        type: "FeatureCollection",
        features: collection.features,
      } as unknown as GeoJsonFeatureCollection);
    };

    if (ready.current) apply();
    else instance.once("load", apply);
  }, [collection]);

  // Fit the viewport when the extent changes - on drill-down, not on hover.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !bounds) return;

    const fit = () => {
      instance.fitBounds(
        [
          [bounds[0], bounds[1]],
          [bounds[2], bounds[3]],
        ],
        { padding: 24, duration: 400, maxZoom: 11 },
      );
    };

    if (ready.current) fit();
    else instance.once("load", fit);
  }, [bounds]);

  // Selection and hover are feature state, not a re-render of the geometry.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current || !collection) return;

    for (const feature of collection.features) {
      const id = feature.properties.unit_id;
      instance.setFeatureState(
        { source: SOURCE_ID, id },
        {
          selected: id === selectedUnitId,
          hovered: id === hoveredUnitId,
        },
      );
    }
  }, [collection, selectedUnitId, hoveredUnitId]);

  return (
    <div
      ref={container}
      className="boundary-map"
      // The canvas conveys the same information the list beside it carries in
      // an accessible form, so it is described rather than made focusable.
      role="img"
      aria-label={label}
      data-testid="boundary-map"
    />
  );
}
