/**
 * Shared geography helpers for the MapLibre canvas and the SVG fallback.
 *
 * The administrative layer is independent of analytics. Overlay classes are
 * applied only when a real in-scope value exists. Out-of-scope districts keep
 * geometry and never receive a count or signal class.
 */

import type { Schemas } from "../../api/client";

export type MapCollection = Schemas["MapFeatureCollection"];
export type FillClass =
  | "none"
  | "nodata"
  | "outside"
  | "urgent"
  | "high"
  | "attention"
  | "informational";

export const FILL_COLOURS: Record<FillClass, string> = {
  none: "#ffffff",
  nodata: "#c5cdd0",
  outside: "#d7dde0",
  urgent: "#96242a",
  high: "#c05621",
  attention: "#c9a227",
  informational: "#7eb6d9",
};

const PRIORITY_RANK = ["urgent", "high", "attention", "informational", "unclassified"];

export function canUseWebGL(): boolean {
  if (typeof document === "undefined") return false;
  if (import.meta.env.MODE === "test") return false;
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

export function rankPriority(priority: string): number {
  const index = PRIORITY_RANK.indexOf(priority);
  return index === -1 ? PRIORITY_RANK.length : index;
}

export function boundsOf(
  collection: MapCollection | undefined,
  metadata: Schemas["MapMetadataResponse"] | undefined,
): [number, number, number, number] | null {
  const box = collection?.bbox;
  if (box && box.length === 4) {
    const [west, south, east, north] = box;
    if (
      west !== undefined &&
      south !== undefined &&
      east !== undefined &&
      north !== undefined
    ) {
      return [west, south, east, north];
    }
  }
  const initial = metadata?.initial_bounds;
  if (!initial) return null;
  return [initial.min_lon, initial.min_lat, initial.max_lon, initial.max_lat];
}

export function decorateCollection(
  collection: MapCollection,
  options: {
    signalPriorityByUnitId: Map<string, string>;
    /** Null means national (every district is in scope unless the feature says otherwise). */
    inScopeUnitIds: Set<string> | null;
  },
): MapCollection {
  const scope = options.inScopeUnitIds;
  return {
    ...collection,
    features: collection.features.map((feature) => {
      const unitId = feature.properties.unit_id;
      const outside = !isInScope(feature, scope);
      const priority = outside ? undefined : options.signalPriorityByUnitId.get(unitId);
      const fillClass: FillClass = outside
        ? "outside"
        : priority && PRIORITY_RANK.includes(priority)
          ? (priority as FillClass)
          : "none";
      return {
        ...feature,
        id: unitId,
        properties: {
          ...feature.properties,
          fill_class: fillClass,
          in_scope: !outside,
        } as unknown as MapCollection["features"][number]["properties"],
      };
    }),
  };
}

export function overlayProps(feature: MapCollection["features"][number]): Record<string, unknown> {
  return { ...feature.properties };
}

export function fillClassOf(feature: MapCollection["features"][number]): FillClass {
  const raw = overlayProps(feature).fill_class;
  if (
    raw === "none" ||
    raw === "nodata" ||
    raw === "outside" ||
    raw === "urgent" ||
    raw === "high" ||
    raw === "attention" ||
    raw === "informational"
  ) {
    return raw;
  }
  return "none";
}

export function isInScope(
  feature: MapCollection["features"][number],
  inScopeUnitIds?: Set<string> | null,
): boolean {
  const raw = overlayProps(feature).in_scope;
  if (raw === false || raw === "false") return false;
  if (inScopeUnitIds != null) return inScopeUnitIds.has(feature.properties.unit_id);
  return true;
}

type Ring = number[][];

function asRings(geometry: unknown): Ring[] {
  if (!geometry || typeof geometry !== "object") return [];
  const record = geometry as { type?: string; coordinates?: unknown };
  if (record.type === "Polygon" && Array.isArray(record.coordinates)) {
    return (record.coordinates as Ring[]).filter((ring) => ring.length >= 4);
  }
  if (record.type === "MultiPolygon" && Array.isArray(record.coordinates)) {
    const rings: Ring[] = [];
    for (const polygon of record.coordinates as Ring[][]) {
      for (const ring of polygon) {
        if (ring.length >= 4) rings.push(ring);
      }
    }
    return rings;
  }
  return [];
}

export function project(
  lon: number,
  lat: number,
  bounds: [number, number, number, number],
  width: number,
  height: number,
): [number, number] {
  const [west, south, east, north] = bounds;
  const dx = east - west || 1;
  const dy = north - south || 1;
  const pad = 8;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  const x = pad + ((lon - west) / dx) * innerW;
  const y = pad + ((north - lat) / dy) * innerH;
  return [x, y];
}

export function featurePathD(
  feature: MapCollection["features"][number],
  bounds: [number, number, number, number],
  width: number,
  height: number,
): string {
  const parts: string[] = [];
  for (const ring of asRings(feature.geometry)) {
    ring.forEach((pair, index) => {
      const lon = pair[0];
      const lat = pair[1];
      if (lon === undefined || lat === undefined) return;
      const [x, y] = project(lon, lat, bounds, width, height);
      parts.push(`${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`);
    });
    parts.push("Z");
  }
  return parts.join(" ");
}
