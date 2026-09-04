/**
 * The national map: Uganda drawn from MARS's own PostGIS boundaries.
 *
 * What this view shows and, as importantly, what it does not:
 *
 * * Administrative boundaries, at the level the caller is authorised to see.
 * * The boundary version every polygon came from, always on screen. A map that
 *   does not say which boundaries it is drawing invites two versions to be
 *   compared as though they were one.
 * * No epidemiological value of any kind. There is no indicator to show yet,
 *   and a plausible-looking colour scale over invented numbers is the single
 *   most misleading thing this page could contain.
 *
 * Drill-down follows the caller's scope: the view opens on their root, and only
 * offers to descend where they are authorised. Nothing outside scope is
 * requested, so nothing outside scope can be drawn.
 */

import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";

import { ApiError, api, type Schemas } from "../../api/client";
import {
  ForbiddenState,
  LoadingState,
  NoDataState,
  UnavailableState,
} from "../../design-system/States";
import { GeographyCanvas } from "./GeographyCanvas";
import { GeographyList } from "./GeographyList";
import { decorateCollection, isInScope } from "./geography";
import "./map.css";

type UnitSummary = Schemas["GeographyUnitSummary"];
type MapMetadata = Schemas["MapMetadataResponse"];

/**
 * One step of the drill-down.
 *
 * The level being *drawn* is not the level being *stood on*: standing on the
 * country draws districts. Subcounties are reached with ``within`` rather than
 * a parent filter, because they hang off counties - a fact of the Ugandan
 * hierarchy, not of this component.
 */
interface DrillStep {
  unitId: string | null;
  unitName: string;
  drawLevel: "district" | "subcounty";
  filter: "none" | "within";
}

/**
 * The root step, before the caller's own geography is known.
 *
 * unitName is a placeholder: "national" is the top of the *caller's* scope,
 * which for a district account is their district, not Uganda. The real name
 * arrives with the map metadata and replaces this - hardcoding "Uganda" would
 * mislabel every delegated account's own view.
 */
const ROOT_STEP: DrillStep = {
  unitId: null,
  unitName: "National",
  drawLevel: "district",
  filter: "none",
};

export function NationalMapView() {
  const [trail, setTrail] = useState<DrillStep[]>([ROOT_STEP]);
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const [hoveredUnitId, setHoveredUnitId] = useState<string | null>(null);

  // The trail always holds at least the root, but an index read is not a proof
  // of that, so the root is the fallback rather than a non-null assertion.
  const step = trail[trail.length - 1] ?? ROOT_STEP;

  const metadata = useQuery<MapMetadata>({
    queryKey: ["map", "metadata"],
    queryFn: api.mapMetadata,
    retry: false,
  });

  const useContextLayer = step.drawLevel === "district" && step.filter === "none";

  const features = useQuery({
    queryKey: ["map", useContextLayer ? "context" : "features", step.drawLevel, step.unitId, step.filter],
    queryFn: () =>
      useContextLayer
        ? api.mapContext({ level: step.drawLevel })
        : api.mapFeatures({
            level: step.drawLevel,
            ...(step.filter === "within" && step.unitId ? { within_id: step.unitId } : {}),
          }),
    enabled: metadata.data?.is_available === true,
    retry: false,
  });

  const bounds = useMemo<[number, number, number, number] | null>(() => {
    const box = features.data?.bbox;
    if (box) {
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
    const initial = metadata.data?.initial_bounds;
    if (initial) {
      return [initial.min_lon, initial.min_lat, initial.max_lon, initial.max_lat];
    }
    return null;
  }, [features.data?.bbox, metadata.data?.initial_bounds]);

  /** The drawn features, as list rows. One source of truth for both views. */
  const collection = useMemo(
    () =>
      features.data
        ? decorateCollection(features.data, {
            signalPriorityByUnitId: new Map(),
            inScopeUnitIds: null,
          })
        : null,
    [features.data],
  );

  const units = useMemo<UnitSummary[]>(() => {
    if (!collection) return [];
    return collection.features.map((feature) => ({
      id: feature.properties.unit_id,
      level: feature.properties.level,
      unit_kind: "unspecified",
      preferred_code: feature.properties.code,
      name: feature.properties.name,
      normalised_name: feature.properties.name.toLowerCase(),
      parent_id: feature.properties.parent_id,
      depth: 0,
      path: feature.properties.path,
      is_active: feature.properties.is_active,
      effective_from: null,
      effective_to: null,
    }));
  }, [collection]);

  const selectedFeature = collection?.features.find(
    (feature) => feature.properties.unit_id === selectedUnitId,
  );
  const selectedUnit =
    selectedFeature && isInScope(selectedFeature)
      ? (units.find((unit) => unit.id === selectedUnitId) ?? null)
      : null;

  const drillInto = useCallback((unit: UnitSummary) => {
    setTrail((current) => [
      ...current,
      {
        unitId: unit.id,
        unitName: unit.name,
        drawLevel: "subcounty",
        filter: "within",
      },
    ]);
    setSelectedUnitId(null);
    setHoveredUnitId(null);
  }, []);

  const goToStep = useCallback((index: number) => {
    setTrail((current) => current.slice(0, index + 1));
    setSelectedUnitId(null);
    setHoveredUnitId(null);
  }, []);

  // -- States ------------------------------------------------------------
  if (metadata.isPending) {
    return (
      <MapPage>
        <LoadingState label="the national map" rows={5} />
      </MapPage>
    );
  }

  if (metadata.isError) {
    return (
      <MapPage>
        {renderQueryError(metadata.error, () => void metadata.refetch())}
      </MapPage>
    );
  }

  if (!metadata.data.is_available) {
    return (
      <MapPage>
        <NoDataState
          title="No boundaries have been loaded"
          description={
            "No boundary version is published, so there is no geography to draw. " +
            "This is the state of a new deployment before the geography importer has run."
          }
          awaiting="a published boundary version"
        />
      </MapPage>
    );
  }

  const versionLabel = metadata.data.boundary_version_code ?? "unknown";
  const rootName = metadata.data.initial_unit_name ?? ROOT_STEP.unitName;

  /** The label for a step, with the root resolved to the caller's own geography. */
  const nameOf = (entry: DrillStep, index: number) =>
    index === 0 ? rootName : entry.unitName;

  const currentName = nameOf(step, trail.length - 1);

  return (
    <MapPage version={versionLabel} importedAt={metadata.data.imported_at}>
      <nav className="map-breadcrumbs" aria-label="Geography drill-down">
        <ol>
          {trail.map((entry, index) => (
            <li key={`${entry.unitId ?? "root"}-${index}`}>
              {index === trail.length - 1 ? (
                <span aria-current="location">{nameOf(entry, index)}</span>
              ) : (
                <button type="button" className="link-button" onClick={() => goToStep(index)}>
                  {nameOf(entry, index)}
                </button>
              )}
            </li>
          ))}
        </ol>
      </nav>

      <div className="map-layout">
        <section className="panel map-layout__canvas" aria-labelledby="map-heading">
          <div className="panel__header">
            <h2 id="map-heading">Boundary map</h2>
            <p className="panel__meta">
              {features.data
                ? `${features.data.features.length} drawn · simplified geometry`
                : null}
            </p>
          </div>

          <div className="panel__body panel__body--flush">
            {features.isPending ? (
              <div className="panel__body">
                <LoadingState label="boundary geometry" rows={4} />
              </div>
            ) : features.isError ? (
              <div className="panel__body">
                {renderQueryError(features.error, () => void features.refetch())}
              </div>
            ) : features.data.features.length === 0 ? (
              <div className="panel__body">
                <NoDataState
                  title="Nothing to draw here"
                  description={
                    `No ${step.drawLevel} boundaries are available within ` +
                    `${currentName} for your assigned geography.`
                  }
                />
              </div>
            ) : (
              <GeographyCanvas
                  collection={collection}
                  bounds={bounds}
                  metadata={metadata.data}
                  selectedUnitId={selectedUnitId}
                  hoveredUnitId={hoveredUnitId}
                  onSelect={(unitId) => {
                    const feature = collection?.features.find(
                      (item) => item.properties.unit_id === unitId,
                    );
                    if (!feature || !isInScope(feature)) return;
                    setSelectedUnitId(unitId);
                  }}
                  onHover={setHoveredUnitId}
                  label={
                    `Map of ${features.data.features.length} ${step.drawLevel} boundaries ` +
                    `within ${currentName}. The list beside this map carries the same ` +
                    `areas and is keyboard navigable.`
                  }
                />
            )}
          </div>
        </section>

        <section className="panel map-layout__list" aria-labelledby="list-heading">
          <div className="panel__header">
            <h2 id="list-heading">
              {step.drawLevel === "district"
                ? "Districts"
                : `Subcounties in ${currentName}`}
            </h2>
          </div>

          <div className="panel__body panel__body--flush">
            {features.data && features.data.features.length > 0 ? (
              <GeographyList
                units={units}
                selectedUnitId={selectedUnitId}
                hoveredUnitId={hoveredUnitId}
                onSelect={(unitId) => {
                  const feature = collection?.features.find(
                    (item) => item.properties.unit_id === unitId,
                  );
                  if (!feature || !isInScope(feature)) return;
                  setSelectedUnitId(unitId);
                }}
                onHover={setHoveredUnitId}
                levelLabel={step.drawLevel === "district" ? "Districts" : "Subcounties"}
                canDrill={(unit) => {
                  if (step.drawLevel !== "district") return false;
                  const feature = collection?.features.find(
                    (item) => item.properties.unit_id === unit.id,
                  );
                  return Boolean(feature && isInScope(feature));
                }}
                onDrill={drillInto}
              />
            ) : null}
          </div>
        </section>
      </div>

      {selectedUnit ? (
        <section className="panel" aria-labelledby="selected-heading" aria-live="polite">
          <div className="panel__header">
            <h2 id="selected-heading">Selected area</h2>
          </div>
          <div className="panel__body">
            <dl className="definition-grid">
              <div>
                <dt>Name</dt>
                <dd>{selectedUnit.name}</dd>
              </div>
              <div>
                <dt>Code</dt>
                <dd className="mono">{selectedUnit.preferred_code}</dd>
              </div>
              <div>
                <dt>Level</dt>
                <dd>{selectedUnit.level}</dd>
              </div>
              <div>
                <dt>Hierarchy path</dt>
                <dd className="mono">{selectedUnit.path}</dd>
              </div>
            </dl>
            <p className="page__note">
              Boundary reference only. No surveillance indicator is available for this
              area yet.
            </p>
          </div>
        </section>
      ) : null}
    </MapPage>
  );
}

interface MapPageProps {
  children: React.ReactNode;
  version?: string;
  importedAt?: string | null;
}

function MapPage({ children, version, importedAt }: MapPageProps) {
  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">National view</p>
          <h1>Uganda administrative boundaries</h1>
          <p className="page__lede">
            Drawn from the boundary version loaded into MARS. Administrative reference
            only - no surveillance indicator is shown.
          </p>
        </div>
        {version ? (
          <div className="page__header-meta">
            <span className="chip chip--info">Boundary version</span>
            <span className="mono">{version}</span>
            {importedAt ? (
              <span className="page__meta-detail">
                {`Imported ${new Date(importedAt).toLocaleDateString()}`}
              </span>
            ) : null}
          </div>
        ) : null}
      </header>
      {children}
    </div>
  );
}

/** Turn a query failure into the state that actually describes it. */
function renderQueryError(error: unknown, retry: () => void) {
  if (error instanceof ApiError) {
    if (error.isForbidden) {
      return (
        <ForbiddenState
          requirement={error.requirement ?? "geography:view"}
          description="Your account does not hold the access needed to view geography."
        />
      );
    }
    if (error.status === 413) {
      return (
        <NoDataState
          title="That area is too large to draw at once"
          description={
            error.problem?.detail ??
            "Narrow the view by opening a single district before drawing subcounties."
          }
        />
      );
    }
    return (
      <UnavailableState
        title="Boundary data could not be loaded"
        description={error.message}
        requestId={error.requestId}
        onRetry={retry}
      />
    );
  }
  return (
    <UnavailableState
      title="Boundary data could not be loaded"
      description="An unexpected error prevented the map from loading."
      onRetry={retry}
    />
  );
}
