/**
 * Geography rendering: GeoJSON stays visible without analytics, and out-of-scope
 * districts never carry a surveillance class.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { GeographyCanvas } from "../src/features/map/GeographyCanvas";
import { SvgBoundaryMap } from "../src/features/map/SvgBoundaryMap";
import {
  decorateCollection,
  featurePathD,
  fillClassOf,
  overlayProps,
  type MapCollection,
} from "../src/features/map/geography";

const PADER = "11111111-0000-4000-8000-000000000312";
const GULU = "11111111-0000-4000-8000-000000000304";

function feature(unitId: string, name: string): MapCollection["features"][number] {
  return {
    type: "Feature",
    id: unitId,
    geometry: {
      type: "MultiPolygon",
      coordinates: [[[[30, 0], [31, 0], [31, 1], [30, 1], [30, 0]]]],
    },
    properties: {
      unit_id: unitId,
      level: "district",
      code: name,
      name,
      parent_id: null,
      path: `UG/${name}`,
      area_sq_km: 1,
      is_active: true,
    },
  };
}

function collection(): MapCollection {
  return {
    type: "FeatureCollection",
    features: [feature(GULU, "GULU"), feature(PADER, "PADER")],
    bbox: [30, 0, 31, 1],
    mars: {
      boundary_version_id: null,
      boundary_version_code: null,
      level: "district",
      parent_id: null,
      within_id: null,
      geometry_resolution: "simplified",
      feature_count: 2,
      matched_count: 2,
      truncated: false,
    },
  };
}

describe("decorateCollection", () => {
  it("keeps every district when analytics are unconfigured", () => {
    const decorated = decorateCollection(collection(), {
      signalPriorityByUnitId: new Map(),
      inScopeUnitIds: null,
    });
    expect(decorated.features).toHaveLength(2);
    expect(decorated.features.every((item) => fillClassOf(item) === "none")).toBe(true);
  });

  it("marks out-of-scope districts without attaching a signal class", () => {
    const decorated = decorateCollection(collection(), {
      signalPriorityByUnitId: new Map([[GULU, "urgent"]]),
      inScopeUnitIds: new Set([PADER]),
    });
    const gulu = decorated.features.find((item) => item.properties.unit_id === GULU);
    const pader = decorated.features.find((item) => item.properties.unit_id === PADER);
    expect(fillClassOf(gulu!)).toBe("outside");
    expect(gulu?.properties && overlayProps(gulu).in_scope).toBe(false);
    expect(fillClassOf(pader!)).toBe("none");
    expect(pader?.properties && overlayProps(pader).in_scope).toBe(true);
  });

  it("does not colour an out-of-scope district even when a signal exists for it", () => {
    const decorated = decorateCollection(collection(), {
      signalPriorityByUnitId: new Map([[GULU, "urgent"]]),
      inScopeUnitIds: new Set([PADER]),
    });
    const gulu = decorated.features.find((item) => item.properties.unit_id === GULU);
    expect(overlayProps(gulu!).fill_class).toBe("outside");
    expect(overlayProps(gulu!).urgent).toBeUndefined();
  });

  it("honours the context-layer in_scope flag under national client scope", () => {
    const raw = collection();
    const first = raw.features[0];
    if (!first) throw new Error("expected a Gulu feature");
    first.properties = { ...first.properties, in_scope: false };
    const decorated = decorateCollection(raw, {
      signalPriorityByUnitId: new Map([[GULU, "urgent"]]),
      inScopeUnitIds: null,
    });
    expect(fillClassOf(decorated.features[0]!)).toBe("outside");
  });

  it("builds a path for Pader geometry", () => {
    const pader = collection().features[1];
    expect(pader).toBeDefined();
    const d = featurePathD(pader!, [30, 0, 31, 1], 100, 100);
    expect(d.startsWith("M")).toBe(true);
    expect(d.includes("Z")).toBe(true);
  });
});

describe("SVG fallback", () => {
  it("renders a path per district from valid GeoJSON", () => {
    const decorated = decorateCollection(collection(), {
      signalPriorityByUnitId: new Map(),
      inScopeUnitIds: null,
    });
    render(
      <SvgBoundaryMap
        collection={decorated}
        bounds={[30, 0, 31, 1]}
        selectedUnitId={null}
        onSelect={() => undefined}
        label="Uganda districts"
      />,
    );
    const svg = screen.getByTestId("boundary-svg");
    expect(svg).toHaveAttribute("data-feature-count", "2");
    expect(svg.querySelectorAll("path")).toHaveLength(2);
  });

  it("does not select an out-of-scope district", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const decorated = decorateCollection(collection(), {
      signalPriorityByUnitId: new Map(),
      inScopeUnitIds: new Set([PADER]),
    });
    render(
      <SvgBoundaryMap
        collection={decorated}
        bounds={[30, 0, 31, 1]}
        selectedUnitId={null}
        onSelect={onSelect}
        label="Pader scope"
      />,
    );
    const gulu = document.querySelector(`[data-unit-id="${GULU}"]`);
    expect(gulu).toHaveAttribute("data-in-scope", "false");
    if (gulu) await user.click(gulu);
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe("GeographyCanvas", () => {
  it("uses the SVG engine when WebGL is unavailable", () => {
    const decorated = decorateCollection(collection(), {
      signalPriorityByUnitId: new Map(),
      inScopeUnitIds: null,
    });
    render(
      <GeographyCanvas
        collection={decorated}
        bounds={[30, 0, 31, 1]}
        selectedUnitId={null}
        hoveredUnitId={null}
        onSelect={() => undefined}
        onHover={() => undefined}
        label="Uganda districts"
        forceSvg
      />,
    );
    expect(screen.getByTestId("boundary-svg")).toBeInTheDocument();
  });
});
