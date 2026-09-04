/**
 * MapLibre canvas for MARS boundary geometry.
 *
 * No external tiles. Geometry is MARS's own GeoJSON. The source is created with
 * whatever collection is already available, then updated when both the style
 * and the data are ready. A missed setData is retried; errors are reported.
 */

import { MapLibreMap, NavigationControl } from "maplibre-gl";
import type { GeoJSONSource, MapLayerMouseEvent } from "maplibre-gl";
import type { FeatureCollection as GeoJsonFeatureCollection } from "geojson";
import { useEffect, useRef } from "react";

import { FILL_COLOURS, type MapCollection } from "./geography";

import "maplibre-gl/dist/maplibre-gl.css";

const SOURCE_ID = "mars-boundaries";
const FILL_LAYER = "mars-boundaries-fill";
const LINE_LAYER = "mars-boundaries-line";
const SELECTED_LAYER = "mars-boundaries-selected";

function unitIdOf(event: MapLayerMouseEvent): string | null {
  const properties: unknown = event.features?.[0]?.properties;
  if (!properties || typeof properties !== "object" || !("unit_id" in properties)) return null;
  const raw = (properties as { unit_id?: unknown }).unit_id;
  return typeof raw === "string" ? raw : null;
}

function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return value.trim() || fallback;
}

function asGeoJSON(collection: MapCollection): GeoJsonFeatureCollection {
  return {
    type: "FeatureCollection",
    features: collection.features.map((feature) => ({
      type: "Feature" as const,
      id: feature.properties.unit_id,
      properties: feature.properties,
      geometry: feature.geometry as unknown as GeoJsonFeatureCollection["features"][number]["geometry"],
    })),
  };
}

export interface BoundaryMapProps {
  collection: MapCollection | null;
  bounds: [number, number, number, number] | null;
  selectedUnitId: string | null;
  hoveredUnitId: string | null;
  onSelect: (unitId: string) => void;
  onHover: (unitId: string | null) => void;
  label: string;
  onReady?: () => void;
  onEngineFailure?: (detail: string) => void;
}

export function BoundaryMap({
  collection,
  bounds,
  selectedUnitId,
  hoveredUnitId,
  onSelect,
  onHover,
  label,
  onReady,
  onEngineFailure,
}: BoundaryMapProps) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  const ready = useRef(false);
  const collectionRef = useRef(collection);
  const boundsRef = useRef(bounds);
  collectionRef.current = collection;
  boundsRef.current = bounds;

  const selectRef = useRef(onSelect);
  const hoverRef = useRef(onHover);
  const failRef = useRef(onEngineFailure);
  const readyRef = useRef(onReady);
  selectRef.current = onSelect;
  hoverRef.current = onHover;
  failRef.current = onEngineFailure;
  readyRef.current = onReady;

  useEffect(() => {
    if (!container.current || map.current) return;
    const host = container.current;
    let instance: MapLibreMap;

    try {
      instance = new MapLibreMap({
        container: host,
        style: {
          version: 8,
          sources: {},
          layers: [
            {
              id: "background",
              type: "background",
              paint: { "background-color": token("--surface-sunken", "#eef1f4") },
            },
          ],
        },
        center: [32.5, 1.4],
        zoom: 5.6,
        attributionControl: false,
        dragRotate: false,
        pitchWithRotate: false,
        touchZoomRotate: false,
      });
    } catch (error) {
      failRef.current?.(error instanceof Error ? error.message : "MapLibre failed to start");
      return;
    }

    instance.on("error", (event) => {
      const message =
        "error" in event && event.error instanceof Error
          ? event.error.message
          : "MapLibre reported an error";
      if (!ready.current) {
        failRef.current?.(message);
      }
    });
    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");

    const installLayers = () => {
      if (instance.getSource(SOURCE_ID)) return;
      const initial = collectionRef.current;
      instance.addSource(SOURCE_ID, {
        type: "geojson",
        data: initial
          ? asGeoJSON(initial)
          : { type: "FeatureCollection", features: [] },
        promoteId: "unit_id",
      });
      instance.addLayer({
        id: FILL_LAYER,
        type: "fill",
        source: SOURCE_ID,
        paint: {
          "fill-color": [
            "match",
            ["coalesce", ["get", "fill_class"], "none"],
            "urgent",
            FILL_COLOURS.urgent,
            "high",
            FILL_COLOURS.high,
            "attention",
            FILL_COLOURS.attention,
            "informational",
            FILL_COLOURS.informational,
            "outside",
            FILL_COLOURS.outside,
            "nodata",
            FILL_COLOURS.nodata,
            FILL_COLOURS.none,
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
      instance.addLayer({
        id: SELECTED_LAYER,
        type: "line",
        source: SOURCE_ID,
        paint: {
          "line-color": token("--accent", "#0b6e63"),
          "line-width": [
            "case",
            ["boolean", ["feature-state", "selected"], false],
            2.4,
            ["boolean", ["feature-state", "hovered"], false],
            1.6,
            0,
          ],
        },
      });
      instance.on("click", FILL_LAYER, (event: MapLayerMouseEvent) => {
        const unitId = unitIdOf(event);
        const raw: unknown = event.features?.[0]?.properties
          ? (event.features[0].properties as Record<string, unknown>).in_scope
          : undefined;
        if (unitId && raw !== false && raw !== "false") selectRef.current(unitId);
      });
      instance.on("mousemove", FILL_LAYER, (event: MapLayerMouseEvent) => {
        instance.getCanvas().style.cursor = "pointer";
        hoverRef.current(unitIdOf(event));
      });
      instance.on("mouseleave", FILL_LAYER, () => {
        instance.getCanvas().style.cursor = "";
        hoverRef.current(null);
      });
      ready.current = true;
      applyCollection(instance, collectionRef.current, readyRef.current, failRef.current);
      applyBounds(instance, boundsRef.current);
      instance.resize();
    };

    if (instance.loaded()) installLayers();
    else instance.once("load", installLayers);

    const observer = new ResizeObserver(() => {
      instance.resize();
    });
    observer.observe(host);

    map.current = instance;

    return () => {
      ready.current = false;
      observer.disconnect();
      instance.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const apply = () => applyCollection(instance, collection, readyRef.current, failRef.current);
    if (ready.current) apply();
    else instance.once("load", apply);
  }, [collection]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !bounds) return;
    const fit = () => applyBounds(instance, bounds);
    if (ready.current) fit();
    else instance.once("load", fit);
  }, [bounds]);

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
      role="img"
      aria-label={label}
      data-testid="boundary-map"
      data-feature-count={collection?.features.length ?? 0}
    />
  );
}

function applyCollection(
  instance: MapLibreMap,
  collection: MapCollection | null,
  onReady?: () => void,
  onFailure?: (detail: string) => void,
): void {
  if (!collection) return;
  try {
    const source: GeoJSONSource | undefined = instance.getSource(SOURCE_ID);
    if (!source) {
      instance.once("idle", () => applyCollection(instance, collection, onReady, onFailure));
      return;
    }
    void source.setData(asGeoJSON(collection));
    instance.once("idle", () => {
      instance.resize();
      onReady?.();
    });
  } catch (error) {
    onFailure?.(error instanceof Error ? error.message : "MapLibre setData failed");
  }
}

function applyBounds(
  instance: MapLibreMap,
  bounds: [number, number, number, number] | null,
): void {
  if (!bounds) return;
  instance.resize();
  instance.fitBounds(
    [
      [bounds[0], bounds[1]],
      [bounds[2], bounds[3]],
    ],
    { padding: 24, duration: 0, maxZoom: 11 },
  );
}
