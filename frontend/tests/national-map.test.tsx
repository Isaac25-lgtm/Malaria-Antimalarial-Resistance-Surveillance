/**
 * National map behaviour.
 *
 * MapLibre needs a WebGL context, which jsdom does not provide, so the canvas
 * component is replaced with a stub that records the props it was given. That
 * is not a compromise: what matters here is what the view *asks the API for*,
 * what it *hands the map*, and whether the page remains usable without the map
 * at all. The canvas drawing itself is MapLibre's responsibility, not MARS's.
 *
 * The accessibility assertions are the point of the file as much as the routing
 * ones. If the only way to select a district were to click a polygon, this page
 * would be unusable by keyboard and invisible to a screen reader.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ClientModule from "../src/api/client";
import { ApiError, type Schemas } from "../src/api/client";

// -- The map canvas stub -----------------------------------------------------
const mapProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));

vi.mock("../src/features/map/BoundaryMap", () => ({
  BoundaryMap: (props: Record<string, unknown>) => {
    mapProps.current = props;
    return <div data-testid="boundary-map" aria-label={String(props.label)} role="img" />;
  },
}));

const mapMetadata = vi.hoisted(() => vi.fn());
const mapFeatures = vi.hoisted(() => vi.fn());

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof ClientModule>();
  return {
    ...actual,
    api: { ...actual.api, mapMetadata, mapFeatures },
  };
});

const { NationalMapView } = await import("../src/features/map/NationalMapView");

// -- Fixtures ----------------------------------------------------------------
const DISTRICT_A = "11111111-0000-4000-8000-000000000001";
const DISTRICT_B = "11111111-0000-4000-8000-000000000002";

function metadata(
  overrides: Partial<Schemas["MapMetadataResponse"]> = {},
): Schemas["MapMetadataResponse"] {
  return {
    is_available: true,
    boundary_version_id: "22222222-0000-4000-8000-000000000001",
    boundary_version_code: "UG-ADMIN-20260902T080732-c5250327",
    boundary_version_label: "Uganda administrative boundaries",
    source_name: "UBOS",
    source_checksum: "c5250327",
    imported_at: "2026-09-02T08:07:32Z",
    initial_bounds: { min_lon: 29.5, min_lat: -1.5, max_lon: 35.0, max_lat: 4.2 },
    initial_unit_id: "33333333-0000-4000-8000-000000000001",
    initial_unit_name: "Uganda",
    initial_unit_level: "country",
    levels: [],
    geometry_resolution: "simplified",
    max_features: 400,
    generated_at: "2026-09-02T09:00:00Z",
    ...overrides,
  };
}

function feature(unitId: string, name: string, code: string, level = "district") {
  return {
    type: "Feature",
    id: unitId,
    geometry: {
      type: "MultiPolygon",
      coordinates: [[[[30, 0], [31, 0], [31, 1], [30, 1], [30, 0]]]],
    },
    properties: {
      unit_id: unitId,
      level,
      code,
      name,
      parent_id: null,
      path: `UG/${code}`,
      area_sq_km: 1234.5,
      is_active: true,
    },
  };
}

function collection(features: ReturnType<typeof feature>[], level = "district") {
  return {
    type: "FeatureCollection",
    features,
    bbox: [30, 0, 32, 1],
    mars: {
      boundary_version_id: "22222222-0000-4000-8000-000000000001",
      boundary_version_code: "UG-ADMIN-20260902T080732-c5250327",
      level,
      parent_id: null,
      within_id: null,
      geometry_resolution: "simplified",
      feature_count: features.length,
      matched_count: features.length,
      truncated: false,
    },
  } as unknown as Schemas["MapFeatureCollection"];
}

function renderView() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NationalMapView />
    </QueryClientProvider>,
  );
}

const twoDistricts = () =>
  collection([feature(DISTRICT_A, "GULU", "304"), feature(DISTRICT_B, "PADER", "312")]);

beforeEach(() => {
  mapProps.current = null;
  mapMetadata.mockReset();
  mapFeatures.mockReset();
});

// ---------------------------------------------------------------------------
describe("loading, empty and error states", () => {
  it("announces that it is loading rather than showing a blank page", () => {
    mapMetadata.mockReturnValue(new Promise(() => {}));
    mapFeatures.mockReturnValue(new Promise(() => {}));
    renderView();
    expect(screen.getByText(/loading the national map/i)).toBeInTheDocument();
  });

  it("says no boundaries are loaded when nothing is published", async () => {
    mapMetadata.mockResolvedValue(metadata({ is_available: false }));
    renderView();
    expect(await screen.findByText(/no boundaries have been loaded/i)).toBeInTheDocument();
  });

  it("does not request geometry when no boundary version is published", async () => {
    mapMetadata.mockResolvedValue(metadata({ is_available: false }));
    renderView();
    await screen.findByText(/no boundaries have been loaded/i);
    expect(mapFeatures).not.toHaveBeenCalled();
  });

  it("distinguishes an unavailable dependency from an empty map", async () => {
    mapMetadata.mockRejectedValue(
      new ApiError(503, null, "the database is unavailable"),
    );
    renderView();
    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
  });

  it("explains a refusal by naming the missing grant, not the resource", async () => {
    mapMetadata.mockRejectedValue(
      new ApiError(
        403,
        {
          type: "about:blank",
          title: "Forbidden",
          status: 403,
          code: "permission_denied",
          detail: "This action requires: geography:view",
        },
        "forbidden",
      ),
    );
    renderView();
    expect(await screen.findByText(/do not have access/i)).toBeInTheDocument();
    expect(screen.getByText(/geography:view/)).toBeInTheDocument();
  });

  it("reports an over-large request as guidance rather than a failure", async () => {
    mapMetadata.mockResolvedValue(metadata());
    mapFeatures.mockRejectedValue(
      new ApiError(
        413,
        {
          type: "about:blank",
          title: "Too large",
          status: 413,
          code: "geography_request_too_broad",
          detail: "2190 subcounty features match this request, above the 400 ceiling.",
        },
        "too large",
      ),
    );
    renderView();
    expect(await screen.findByText(/too large to draw at once/i)).toBeInTheDocument();
  });

  it("reports an authorised but empty area as having nothing to draw", async () => {
    mapMetadata.mockResolvedValue(metadata());
    mapFeatures.mockResolvedValue(collection([]));
    renderView();
    expect(await screen.findByText(/nothing to draw here/i)).toBeInTheDocument();
  });
});

describe("national rendering", () => {
  beforeEach(() => {
    mapMetadata.mockResolvedValue(metadata());
    mapFeatures.mockResolvedValue(twoDistricts());
  });

  it("requests the district layer for the national view", async () => {
    renderView();
    await screen.findByTestId("boundary-map");
    expect(mapFeatures).toHaveBeenCalledWith({ level: "district" });
  });

  it("renders the map once geometry arrives", async () => {
    renderView();
    expect(await screen.findByTestId("boundary-map")).toBeInTheDocument();
  });

  it("labels the root with the caller's own geography, not the country", async () => {
    // A district account's "national" view is their district. Hardcoding
    // "Uganda" here would mislabel every delegated account's own map.
    mapMetadata.mockResolvedValue(
      metadata({ initial_unit_name: "GULU", initial_unit_level: "district" }),
    );
    renderView();
    const trail = await screen.findByRole("navigation", { name: /drill-down/i });
    expect(within(trail).getByText("GULU")).toBeInTheDocument();
    expect(within(trail).queryByText("Uganda")).not.toBeInTheDocument();
  });

  it("shows the boundary version on screen", async () => {
    renderView();
    expect(
      await screen.findByText("UG-ADMIN-20260902T080732-c5250327"),
    ).toBeInTheDocument();
  });

  it("hands the map the collection bounding box to fit", async () => {
    renderView();
    await screen.findByTestId("boundary-map");
    expect(mapProps.current?.bounds).toEqual([30, 0, 32, 1]);
  });

  it("states that the geometry drawn is simplified", async () => {
    renderView();
    expect(await screen.findByText(/simplified geometry/i)).toBeInTheDocument();
  });

  it("shows no epidemiological value anywhere on the page", async () => {
    const { container } = renderView();
    await screen.findByTestId("boundary-map");
    const text = container.textContent ?? "";
    for (const word of ["cases", "incidence", "positivity", "resistance", "failure rate"]) {
      expect(text.toLowerCase()).not.toContain(word);
    }
  });
});

describe("selection without the map", () => {
  beforeEach(() => {
    mapMetadata.mockResolvedValue(metadata());
    mapFeatures.mockResolvedValue(twoDistricts());
  });

  it("lists every drawn district as a real control", async () => {
    renderView();
    const list = await screen.findByRole("list", { name: /districts in view/i });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  });

  it("selects a district from the keyboard", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.tab();
    await user.tab();
    const gulu = screen.getByRole("button", { name: /^GULU/ });
    act(() => gulu.focus());
    await user.keyboard("{Enter}");

    const panel = await screen.findByRole("region", { name: /selected area/i });
    expect(within(panel).getByText("GULU")).toBeInTheDocument();
    expect(within(panel).getByText("304")).toBeInTheDocument();
  });

  it("marks the selected district with aria-current, not colour alone", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getByRole("button", { name: /^PADER/ }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^PADER/ })).toHaveAttribute(
        "aria-current",
        "true",
      );
    });
  });

  it("passes the selection through to the map", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getByRole("button", { name: /^GULU/ }));
    await waitFor(() => {
      expect(mapProps.current?.selectedUnitId).toBe(DISTRICT_A);
    });
  });
});

describe("drill-down", () => {
  beforeEach(() => {
    mapMetadata.mockResolvedValue(metadata());
    mapFeatures.mockImplementation((query: { level: string; within_id?: string }) => {
      if (query.level === "district") return Promise.resolve(twoDistricts());
      return Promise.resolve(
        collection(
          [
            feature("44444444-0000-4000-8000-000000000001", "BARDEGE", "30401", "subcounty"),
          ],
          "subcounty",
        ),
      );
    });
  });

  it("opens a district and requests its subcounties by subtree", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getAllByRole("button", { name: /^Open/ })[0]!);

    await waitFor(() => {
      expect(mapFeatures).toHaveBeenCalledWith({
        level: "subcounty",
        within_id: DISTRICT_A,
      });
    });
  });

  it("shows the drilled district in the breadcrumb trail", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getAllByRole("button", { name: /^Open/ })[0]!);

    const trail = await screen.findByRole("navigation", { name: /drill-down/i });
    await waitFor(() => {
      expect(within(trail).getByText("GULU")).toBeInTheDocument();
    });
  });

  it("returns to the national view from the breadcrumb", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getAllByRole("button", { name: /^Open/ })[0]!);
    const trail = await screen.findByRole("navigation", { name: /drill-down/i });
    await within(trail).findByText("GULU");

    await user.click(within(trail).getByRole("button", { name: "Uganda" }));

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Boundary map", level: 2 })).toBeInTheDocument();
    });
  });

  it("offers no further drill from a subcounty", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getAllByRole("button", { name: /^Open/ })[0]!);
    await screen.findByText("BARDEGE");

    expect(screen.queryAllByRole("button", { name: /^Open/ })).toHaveLength(0);
  });
});

describe("accessibility", () => {
  beforeEach(() => {
    mapMetadata.mockResolvedValue(metadata());
    mapFeatures.mockResolvedValue(twoDistricts());
  });

  it("gives the map region a description naming its keyboard alternative", async () => {
    renderView();
    const map = await screen.findByTestId("boundary-map");
    const label = map.getAttribute("aria-label") ?? "";
    expect(label).toMatch(/keyboard/i);
    expect(label).toMatch(/district/i);
  });

  it("names the drill-down trail as a navigation landmark", async () => {
    renderView();
    expect(
      await screen.findByRole("navigation", { name: /drill-down/i }),
    ).toBeInTheDocument();
  });

  it("gives the geography list an accessible name", async () => {
    renderView();
    expect(
      await screen.findByRole("list", { name: /districts in view/i }),
    ).toBeInTheDocument();
  });

  it("labels every selection control with the area it selects", async () => {
    renderView();
    await screen.findByTestId("boundary-map");
    for (const name of ["GULU", "PADER"]) {
      expect(screen.getByRole("button", { name: new RegExp(`^${name}`) })).toBeInTheDocument();
    }
  });

  it("announces the selected area to assistive technology when it changes", async () => {
    const user = userEvent.setup();
    const { container } = renderView();
    await screen.findByTestId("boundary-map");

    await user.click(screen.getByRole("button", { name: /^GULU/ }));
    await waitFor(() => {
      expect(container.querySelector("[aria-live='polite']")).toBeInTheDocument();
    });
  });

  it("keeps every heading in a single ordered outline", async () => {
    renderView();
    await screen.findByTestId("boundary-map");
    const levels = screen
      .getAllByRole("heading")
      .map((heading) => Number(heading.tagName.slice(1)));
    expect(levels[0]).toBe(1);
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i]! - levels[i - 1]!).toBeLessThanOrEqual(1);
    }
  });
});
