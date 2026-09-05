/**
 * Operational overview — the screen a Ministry user opens first.
 *
 * Geography renders independently of analytics. No figure is computed here.
 * Compact executive states stay in the layout; implementation strings live in
 * tooltips and on Data Quality.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, api, type Schemas } from "../../api/client";
import { MeasureGrid } from "../../design-system/Measure";
import { ForbiddenState, UnavailableState } from "../../design-system/States";
import { PeriodControl } from "../../design-system/Surveillance";
import { formatMoment, monthPeriod, type PeriodSelection } from "../../design-system/period";
import { useAuth } from "../../auth/context";
import { GeographyCanvas } from "../map/GeographyCanvas";
import { boundsOf, confirmedByArea, decorateCollection, isInScope, overlayProps } from "../map/geography";
import { PatientTable } from "../patients/PatientSurveillanceView";
import "./command-centre.css";

type Snapshot = Schemas["OverviewSnapshot"];

function districtCountOverlay(districtId: string | null, snapshot: Schemas["LiveDashboardSnapshot"] | null | undefined): Map<string, number> {
  const confirmed = snapshot?.kpis.find((kpi) => kpi.code === "ENC_CONFIRMED_MALARIA");
  return districtId && confirmed?.status === "available" && confirmed.numerator != null
    ? new Map<string, number>([[districtId, confirmed.numerator]])
    : new Map<string, number>();
}

export function CommandCentreView() {
  const { user, can } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const [boundaryLevel, setBoundaryLevel] = useState<"district" | "subcounty">("district");
  const discoveryRequested = useRef(false);
  const synchronizationRequested = useRef<string | null>(null);
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );

  const overview = useQuery({
    queryKey: ["surveillance", "overview", range],
    queryFn: () => api.overview(range),
    retry: false,
  });

  const liveMode = user?.source_status?.mode === "live";
  const discovery = useQuery({
    queryKey: ["live", "metadata-discovery"],
    queryFn: api.latestLiveMetadataDiscovery,
    enabled: liveMode,
    retry: false,
  });
  const runDiscovery = useMutation({
    mutationFn: api.runLiveMetadataDiscovery,
    onSuccess: (result) => {
      queryClient.setQueryData(["live", "metadata-discovery"], result);
    },
  });

  useEffect(() => {
    if (
      liveMode &&
      discovery.isFetched &&
      !discovery.data &&
      !discoveryRequested.current &&
      !runDiscovery.isPending &&
      !runDiscovery.isError
    ) {
      discoveryRequested.current = true;
      runDiscovery.mutate();
    }
  }, [discovery.data, discovery.isFetched, liveMode, runDiscovery]);
  const liveDashboard = useQuery({
    queryKey: ["live", "dashboard", range],
    queryFn: () => api.latestLiveDashboard(range),
    enabled: liveMode,
    retry: false,
  });
  const synchronizeLive = useMutation({
    mutationFn: () => api.synchronizeLiveDashboard(range),
    onSuccess: (result) => {
      queryClient.setQueryData(["live", "dashboard", range], result);
      queryClient.setQueryData(["live", "dashboard", "latest"], result);
    },
  });

  useEffect(() => {
    const synchronizationKey = `${range.period_start}:${range.period_end}:${discovery.data?.generated_at ?? "none"}`;
    if (
      liveMode &&
      discovery.data &&
      liveDashboard.isFetched &&
      (!liveDashboard.data ||
        liveDashboard.data.period_start !== range.period_start ||
        liveDashboard.data.period_end !== range.period_end) &&
      synchronizationRequested.current !== synchronizationKey &&
      !synchronizeLive.isPending &&
      !synchronizeLive.isError
    ) {
      synchronizationRequested.current = synchronizationKey;
      synchronizeLive.mutate();
    }
  }, [
    discovery.data,
    liveDashboard.data,
    liveDashboard.isFetched,
    liveMode,
    range.period_end,
    range.period_start,
    synchronizeLive,
  ]);

  const mapMeta = useQuery({
    queryKey: ["map", "metadata"],
    queryFn: api.mapMetadata,
    retry: false,
  });

  const patients = useQuery({
    queryKey: ["patients", "overview", range],
    queryFn: () =>
      api.patientsOfInterest({
        period_from: range.period_start,
        period_to: range.period_end,
        limit: 5,
      }),
    enabled: liveMode && can("case:view_pseudonymous_evidence"),
    retry: false,
  });

  const districtScopes = (user?.geography_scopes ?? []).filter((scope) => scope.level === "district");
  const nationalMap = Boolean(user?.has_national_scope);
  const soleDistrict = districtScopes.length === 1 ? districtScopes[0] : undefined;
  const singleDistrictId = !nationalMap && soleDistrict ? soleDistrict.geography_unit_id : null;

  const features = useQuery({
    queryKey: nationalMap
      ? ["map", "context", "district"]
      : singleDistrictId
        ? ["map", "features", boundaryLevel, singleDistrictId]
        : ["map", "features", "district"],
    queryFn: () => {
      if (nationalMap) return api.mapContext({ level: "district" });
      if (singleDistrictId && boundaryLevel === "subcounty") {
        return api.mapFeatures({ level: boundaryLevel, within_id: singleDistrictId });
      }
      return api.mapFeatures({ level: "district" });
    },
    retry: false,
  });

  if (overview.isError) {
    const error = overview.error;
    if (error instanceof ApiError && error.isForbidden) {
      return <ForbiddenState requirement={error.requirement ?? "surveillance:view_aggregate"} />;
    }
    if (error instanceof ApiError && error.isUnavailable) {
      return (
        <UnavailableState title="Overview could not be loaded" description={error.message} />
      );
    }
  }

  const snap = overview.data;
  const subcountyOverlay = confirmedByArea(features.data, liveDashboard.data?.facilities);
  const collection = features.data
    ? decorateCollection(features.data, {
        signalPriorityByUnitId: priorityByUnit(snap),
        // The API already applies materialised-path scope. A district principal
        // contains the district UUID, not every authorised descendant UUID, so
        // a second UUID-membership check would incorrectly grey every subcounty.
        inScopeUnitIds: null,
        liveCounts: liveMode,
        confirmedByUnitId: boundaryLevel === "subcounty"
          ? subcountyOverlay.counts
          : districtCountOverlay(singleDistrictId, liveDashboard.data),
      })
    : null;

  return (
    <div className="page overview">
      <header className="overview__header">
        <div>
          <h1>{snap?.title ?? "Overview"}</h1>
          <p className="page__lede">
            {snap?.subtitle ?? "Malaria surveillance from routine health information systems"}
          </p>
        </div>
        <div className="overview__controls">
          <PeriodControl period={period} onChange={setPeriod} />
          <label className="label" htmlFor="geographic-level">
            Geographic level
          </label>
          <select
            id="geographic-level"
            className="period-control__select"
            value={snap?.requested_scope ?? "national"}
            disabled
          >
            <option value={snap?.requested_scope ?? "national"}>
              {scopeLabel(snap, user?.has_national_scope ?? false)}
            </option>
          </select>
        </div>
      </header>

      <p className={`overview__mode overview__mode--${snap?.data_mode ?? "unavailable"}`} role="status">
        {synchronizeLive.isError ? "Refresh failed — showing the last retrieved snapshot" : modeLine(snap, user, liveDashboard.data, synchronizeLive.isPending)}
      </p>

      {liveMode ? (
        <LiveDiscoveryBar
          result={discovery.data}
          loading={discovery.isPending || runDiscovery.isPending}
          error={discovery.isError || runDiscovery.isError}
          onRun={() => runDiscovery.mutate()}
          dashboard={liveDashboard.data}
          synchronizing={synchronizeLive.isPending}
          syncError={synchronizeLive.isError}
          onSynchronize={() => synchronizeLive.mutate()}
        />
      ) : null}

      {liveDashboard.error ? <UnavailableState title="Live data could not be loaded" description={liveDashboard.error.message} /> : null}
      {liveDashboard.data?.warnings.length ? <div className="notice notice--warning" role="status">{liveDashboard.data.warnings.join(". ")}</div> : null}

      <section aria-labelledby="kpi-heading">
        <h2 id="kpi-heading" className="visually-hidden">
          Key measures
        </h2>
        {overview.isLoading ? (
          <div className="measure-grid measure-grid--compact overview__kpi-skeleton" aria-busy="true">
            {Array.from({ length: 6 }, (_, index) => (
              <article key={index} className="measure measure--compact" />
            ))}
          </div>
        ) : snap ? (
          <MeasureGrid
            measures={
              liveMode && liveDashboard.data
                ? liveMeasures(liveDashboard.data, snap)
                : executiveKpis(snap.kpis.items)
            }
            compact
          />
        ) : null}
      </section>

      <div className="overview__primary">
        <section className="overview__map panel" aria-labelledby="map-heading">
          <div className="panel__header">
            <div>
              <h2 id="map-heading">
                {nationalMap
                  ? "Uganda: active surveillance signals"
                  : singleDistrictId
                    ? `${soleDistrict?.name.replace(/\s+district$/i, "")} District Map`
                    : "Authorised geography"}
              </h2>
              <p className="panel__lede">
                {nationalMap
                  ? "Districts by highest-priority active signal"
                  : "Boundaries inside the authorised scope. Surveillance values stay scoped."}
              </p>
            </div>
            {singleDistrictId ? (
              <label className="label">Boundary layer
                <select className="period-control__select" value={boundaryLevel} onChange={(event) => { setBoundaryLevel(event.target.value as "district" | "subcounty"); setSelectedUnitId(null); }}>
                  <option value="district">District</option>
                  <option value="subcounty">Subcounties</option>
                </select>
              </label>
            ) : null}
          </div>
          <div className="overview__map-canvas">
            {features.isPending ? (
              <div className="overview__compact-state" role="status">
                Loading geography
              </div>
            ) : collection ? (
              <GeographyCanvas
                collection={collection}
                bounds={boundsOf(features.data, mapMeta.data)}
                metadata={mapMeta.data}
                selectedUnitId={selectedUnitId}
                hoveredUnitId={null}
                onSelect={(unitId) => {
                  const feature = collection.features.find((item) => item.properties.unit_id === unitId);
                  if (!feature || !isInScope(feature)) return;
                  setSelectedUnitId(unitId);
                  if (!liveMode && feature.properties.level === "district") void navigate(`/workspaces/districts/${unitId}`);
                }}
                onHover={() => undefined}
                label={liveMode ? "Authorised district and subcounty boundaries from supplied GeoJSON. Blue means reported totals; grey means no linked area total." : "Uganda districts by surveillance signal."}
                forceSvg={liveMode}
                facilities={liveDashboard.data?.facilities ?? []}
              />
            ) : (
              <div className="overview__compact-state" role="status">
                {features.isError ? "Map unavailable" : "Geography not published"}
              </div>
            )}
          </div>
          {liveMode ? <div className="panel__body">
            <p>Boundaries: supplied Uganda district and subcounty GeoJSON. No facility coordinates are invented.</p>
            {boundaryLevel === "subcounty" ? <p>Facility totals are assigned only where the eRegisters ancestor label exactly matches a supplied GeoJSON subcounty. Unmatched values remain in district totals.</p> : null}
            {liveDashboard.data ? <p>{liveDashboard.data.facilities.filter((facility) => facility.latitude == null || facility.longitude == null).length} of {liveDashboard.data.facilities.length} facilities have no usable coordinate pair. Their reported figures remain included in district totals and the facility table.</p> : null}
            {boundaryLevel === "subcounty" && liveDashboard.data ? <p>{subcountyOverlay.assignedFacilities.size} of {liveDashboard.data.facilities.filter((facility) => facility.confirmed_malaria != null).length} reporting facilities were linked to a GeoJSON subcounty by verified hierarchy metadata.</p> : null}
            {collection?.features.filter((feature) => feature.properties.unit_id === selectedUnitId).map((feature) => <p key={feature.properties.unit_id}><strong>{feature.properties.name}</strong>: {typeof overlayProps(feature).confirmed_count === "number" ? `${Number(overlayProps(feature).confirmed_count).toLocaleString()} reported confirmed malaria cases in the selected period.` : "No verified area-level total available."}</p>)}
          </div> : null}
          {liveMode ? (
            <ul className="overview__legend">
              <li><span className="map-dot map-dot--reporting" /> Reporting facility</li>
              <li><span className="map-dot map-dot--stockout" /> Stock-out reported</li>
              <li><span className="swatch" style={{ backgroundColor: "#93c5e8" }} /> Reported area total (not a risk class)</li>
              <li><span className="swatch swatch--insufficient" /> No linked area total</li>
            </ul>
          ) : (
            <ul className="overview__legend">
              <li><span className="swatch swatch--urgent" /> Very high</li>
              <li><span className="swatch swatch--high" /> High</li>
              <li><span className="swatch swatch--attention" /> Moderate</li>
              <li><span className="swatch swatch--info" /> Under review</li>
              <li><span className="swatch swatch--none" /> No active signal</li>
              <li><span className="swatch swatch--insufficient" /> No / insufficient data</li>
              <li><span className="swatch swatch--outside" /> Outside authorised scope</li>
            </ul>
          )}
        </section>

        <div className="overview__side overview__side--signals">
          {liveMode && liveDashboard.data ? (
            <LiveAlertSummary snapshot={liveDashboard.data} />
          ) : (
            <BucketPanel
              title="Signals by priority"
              section={snap?.signals_by_priority}
              href="/signals"
              linkLabel="View all signals"
              empty="Not configured"
            />
          )}
          <BucketPanel
            title="Investigations"
            section={snap?.investigations_by_status}
            href="/action-centre"
            linkLabel="View all investigations"
            empty="No records"
          />
        </div>

        {liveMode && liveDashboard.data ? (
          <LiveFacilityPanel snapshot={liveDashboard.data} />
        ) : (
          <DistrictPanel section={snap?.districts_requiring_review} />
        )}
      </div>

      <div className="overview__ops">
        {liveMode && liveDashboard.data ? (
          <LiveCommodityPanel snapshot={liveDashboard.data} />
        ) : (
          <CommodityPanel section={snap?.commodity_alerts} />
        )}
        {liveMode && liveDashboard.data ? (
          <LiveDataQualityPanel snapshot={liveDashboard.data} />
        ) : (
          <BucketPanel title="Needs attention" section={snap?.needs_attention} empty="No items" />
        )}
      </div>

      <div className="overview__charts">
        {liveMode && liveDashboard.data ? (
          <>
            <LiveTrendChart title="Confirmed malaria trend" snapshot={liveDashboard.data} metric="confirmed_malaria" />
            <LiveTrendChart title="Testing and positivity" snapshot={liveDashboard.data} metric="tested_for_malaria" showRate />
          </>
        ) : (
          <>
            <ChartPlaceholder title="Confirmed malaria vs baseline" section={snap?.confirmed_malaria_trend} />
            <ChartPlaceholder title="Testing and positivity rate" section={snap?.testing_positivity} />
          </>
        )}
      </div>

      <div className="overview__bottom-grid">
        {liveMode && liveDashboard.data ? (
          <LiveIssuesTable snapshot={liveDashboard.data} />
        ) : (
          <SignalTable section={snap?.recent_signals} />
        )}
        <PatientOverviewPanel
          enabled={liveMode && can("case:view_pseudonymous_evidence")}
          loading={patients.isPending}
          unavailable={patients.error instanceof ApiError && patients.error.isUnavailable}
          patients={patients.data ?? []}
          livePatients={liveDashboard.data?.repeat_positive_patients ?? []}
        />
      </div>

      <footer className="overview__freshness">
        <span>
          Last sync:{" "}
          {liveDashboard.data?.synchronized_at ?? snap?.last_successful_synchronization
            ? formatMoment(
                liveDashboard.data?.synchronized_at ?? snap?.last_successful_synchronization ?? null,
              )
            : "Not yet run"}
        </span>
        <span>{liveMode ? `Source updated: ${liveDashboard.data?.source_updated_at ? formatMoment(liveDashboard.data.source_updated_at) : "not supplied"}` : `Last updated ${formatMoment(snap?.provenance.analytics_refreshed_at ?? null)}`}</span>
        <span className={`freshness freshness--${snap?.data_mode ?? "unavailable"}`}>
          {sourceFreshness(snap, user, liveDashboard.data)}
        </span>
      </footer>
    </div>
  );
}

function PatientOverviewPanel({
  enabled,
  loading,
  unavailable,
  patients,
  livePatients,
}: {
  enabled: boolean;
  loading: boolean;
  unavailable: boolean;
  patients: Schemas["PatientOfInterestSummary"][];
  livePatients: Schemas["LiveRepeatPositivePatient"][];
}) {
  if (!enabled) return null;
  return (
    <section className="panel" aria-labelledby="recent-patients-heading">
      <div className="panel__header">
        <h2 id="recent-patients-heading">Recent patients of interest</h2>
        <Link className="overview__panel-link" to="/patients">View all →</Link>
      </div>
      <div className="panel__body">
        {loading ? (
          <p className="overview__compact-note">Loading authorised evidence…</p>
        ) : unavailable ? (
          <p className="overview__compact-note">Patient alias key or controlled sync not ready.</p>
        ) : livePatients.length > 0 ? (
          <table className="patient-table">
            <thead>
              <tr>
                <th>Patient ID</th>
                <th>First positive</th>
                <th>Repeat positive</th>
                <th>Interval</th>
                <th>Facility</th>
              </tr>
            </thead>
            <tbody>
              {livePatients.slice(0, 5).map((patient) => (
                <tr key={patient.mars_patient_id}>
                  <td className="mono">{patient.mars_patient_id}</td>
                  <td>{patient.first_positive_on}</td>
                  <td>{patient.latest_positive_on}</td>
                  <td>{patient.interval_days} days</td>
                  <td>{patient.facility_name}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : patients.length === 0 ? (
          <p className="overview__compact-note">
            No validated patient evidence synchronised for this period.
          </p>
        ) : (
          <PatientTable patients={patients} />
        )}
      </div>
    </section>
  );
}

function LiveDiscoveryBar({
  result,
  loading,
  error,
  onRun,
  dashboard,
  synchronizing,
  syncError,
  onSynchronize,
}: {
  result: Schemas["LiveMetadataDiscoverySummary"] | null | undefined;
  loading: boolean;
  error: boolean;
  onRun: () => void;
  dashboard: Schemas["LiveDashboardSnapshot"] | null | undefined;
  synchronizing: boolean;
  syncError: boolean;
  onSynchronize: () => void;
}) {
  return (
    <section className="overview__discovery" aria-label="Live source readiness">
      <div>
        <strong>{result ? "eRegisters metadata discovered" : "Live data mapping required"}</strong>
        <span>
          {result
            ? ` DHIS2 ${result.dhis2_version ?? "version unavailable"} · ${result.programme_count} programmes · ${result.program_stage_count} stages · ${result.data_element_count} data elements · ${result.accessible_facility_count ?? "unknown"} accessible facilities.`
            : " Discover the real OPD programme, malaria variables, and authorised Tracker scope before any data-bearing request."}
        </span>
        {dashboard ? (
          <em>
            Real source snapshot: {dashboard.aggregate_value_count} HMIS values and{" "}
            {dashboard.tracker_event_count} Tracker events. No synthetic values used.
          </em>
        ) : result ? (
          <em>Metadata ready. Real data synchronization is pending.</em>
        ) : null}
        {error ? <em>Discovery could not complete; no patient data were requested.</em> : null}
        {syncError ? <em>Live synchronization failed; no synthetic fallback was used.</em> : null}
      </div>
      <div className="overview__discovery-actions">
        <button className="button button--secondary" type="button" onClick={onRun} disabled={loading}>
          {loading ? "Reading metadata…" : result ? "Refresh metadata" : "Discover source metadata"}
        </button>
        {result ? (
          <button
            className="button button--primary"
            type="button"
            onClick={onSynchronize}
            disabled={synchronizing}
          >
            {synchronizing
              ? "Synchronizing live data…"
              : dashboard
                ? "Refresh live data"
                : "Synchronize live data"}
          </button>
        ) : null}
      </div>
    </section>
  );
}

function executiveKpis(items: Schemas["SurveillanceMeasure"][]): Schemas["SurveillanceMeasure"][] {
  return items.filter((item) => item.code !== "ACTIVE_SIGNALS").slice(0, 6);
}

function liveMeasures(
  live: Schemas["LiveDashboardSnapshot"],
  snap: Snapshot,
): Schemas["SurveillanceMeasure"][] {
  return live.kpis
    .filter(
      (item) =>
        item.status === "available" &&
        !(
          item.unit === "percent" &&
          item.numerator != null &&
          item.denominator != null &&
          item.numerator > item.denominator
        ),
    )
    .map((item) => ({
    code: item.code,
    label: item.label,
    value: item.value,
    unit: item.unit,
    numerator: item.numerator,
    denominator: item.denominator,
    period: { start: live.period_start, end: live.period_end },
    geography_grain: "district",
    geography_unit_id: snap.kpis.items[0]?.geography_unit_id ?? null,
    facility_id: null,
    source: item.source,
    method_version_id: null,
    source_freshness: live.source_updated_at ?? null,
    comparison: null,
    status: item.status,
    status_detail:
      item.status === "available"
        ? "Real authorized DHIS2 source value"
        : "No reported value was returned for this period and scope",
    missing_configuration: [],
    }));
}

function LiveCommodityPanel({
  snapshot,
}: {
  snapshot: Schemas["LiveDashboardSnapshot"];
}) {
  const items = [
    ["RDT stock-out", snapshot.commodity_alerts.rdt_stock_out_facilities],
    ["AL stock-out", snapshot.commodity_alerts.al_stock_out_facilities],
    ["Artesunate stock-out", snapshot.commodity_alerts.artesunate_stock_out_facilities],
  ] as const;
  return (
    <section className="panel" aria-label="Commodity security">
      <div className="panel__header">
        <h2>Commodity security</h2>
      </div>
      <div className="panel__body">
        <ul className="bucket-list">
          {items.map(([label, count]) => (
            <li className="bucket-list__row" key={label}>
              <span className="bucket-list__label">{label}</span>
              <span className="bucket-list__track" aria-hidden="true" />
              <span className="bucket-list__count mono">{count}</span>
            </li>
          ))}
        </ul>
        <p className="overview__compact-note">Reported HMIS 105 stock-out days.</p>
      </div>
    </section>
  );
}

function LiveAlertSummary({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  const alerts = snapshot.operational_alerts ?? [];
  const actionRequired = alerts.filter(
    (item) => item.status === "action_required",
  ).length;
  const review = alerts.filter((item) => item.status === "review").length;
  return (
    <section className="panel" aria-label="Live issues requiring review">
      <div className="panel__header">
        <h2>Live issues requiring review</h2>
      </div>
      <div className="panel__body">
        <ul className="bucket-list">
          <LiveBucket label="Operational action" count={actionRequired} tone="high" />
          <LiveBucket label="Data-quality review" count={review} tone="attention" />
          <LiveBucket
            label="Repeat-positive patients"
            count={snapshot.repeat_positive_patients.length}
            tone="informational"
          />
        </ul>
        <p className="overview__compact-note">
          Observed source conditions only; no resistance conclusion is inferred.
        </p>
      </div>
    </section>
  );
}

function LiveBucket({ label, count, tone }: { label: string; count: number; tone: string }) {
  return (
    <li className="bucket-list__row">
      <span className="bucket-list__label">{label}</span>
      <span className="bucket-list__track" aria-hidden="true">
        <span
          className={`bucket-list__fill bucket-list__fill--${tone}`}
          style={{ width: count > 0 ? `${Math.min(100, 18 + count * 8)}%` : "0%" }}
        />
      </span>
      <span className="bucket-list__count mono">{count}</span>
    </li>
  );
}

function LiveFacilityPanel({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  const rows = [...snapshot.facilities]
    .sort((left, right) => {
      const leftStock =
        (left.rdt_days_out_of_stock ?? 0) +
        (left.al_days_out_of_stock ?? 0) +
        (left.artesunate_days_out_of_stock ?? 0);
      const rightStock =
        (right.rdt_days_out_of_stock ?? 0) +
        (right.al_days_out_of_stock ?? 0) +
        (right.artesunate_days_out_of_stock ?? 0);
      return rightStock - leftStock || (right.confirmed_malaria ?? 0) - (left.confirmed_malaria ?? 0);
    })
    .slice(0, 8);
  return (
    <section className="panel overview__facility-panel" aria-labelledby="facility-review-heading">
      <div className="panel__header">
        <h2 id="facility-review-heading">Facility reporting overview</h2>
        <Link className="overview__panel-link" to="/facilities">View all →</Link>
      </div>
      <div className="panel__body table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th>Facility</th>
              <th>Confirmed</th>
              <th>Tested</th>
              <th>Stock-out commodity-days (sum)</th>
              <th>Tracker</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((facility) => (
              <tr key={facility.uid}>
                <th>{facility.name}</th>
                <td>{formatCount(facility.confirmed_malaria)}</td>
                <td>{formatCount(facility.tested_for_malaria)}</td>
                <td>
                  {(facility.rdt_days_out_of_stock ?? 0) +
                    (facility.al_days_out_of_stock ?? 0) +
                    (facility.artesunate_days_out_of_stock ?? 0)}
                </td>
                <td>{facility.tracker_reported ? "Reported" : "No event returned"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LiveDataQualityPanel({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  const items = [
    ["HMIS reporting facilities", `${snapshot.aggregate_reporting_facility_count}/${snapshot.facility_count}`],
    ["Tracker reporting facilities", `${snapshot.tracker_reporting_facility_count}/${snapshot.facility_count}`],
    ["HMIS values read", snapshot.aggregate_value_count.toLocaleString()],
    ["Tracker events read", snapshot.tracker_event_count.toLocaleString()],
    ["Mapped malaria lab events", snapshot.malaria_lab_event_count.toLocaleString()],
    ["Invalid HMIS values", snapshot.invalid_aggregate_value_count.toLocaleString()],
  ];
  return (
    <section className="panel" aria-label="Live data quality">
      <div className="panel__header"><h2>Live data quality</h2></div>
      <div className="panel__body">
        <dl className="live-quality-list">
          {items.map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd className="mono">{value}</dd></div>
          ))}
        </dl>
      </div>
    </section>
  );
}

function LiveTrendChart({
  title,
  snapshot,
  metric,
  showRate = false,
}: {
  title: string;
  snapshot: Schemas["LiveDashboardSnapshot"];
  metric: "confirmed_malaria" | "tested_for_malaria";
  showRate?: boolean;
}) {
  const points = (snapshot.trend ?? []).filter((item) => item[metric] != null);
  const max = Math.max(1, ...points.map((item) => item[metric] ?? 0));
  const coordinates = points.map((item, index) => {
    const x = points.length === 1 ? 260 : 20 + (index / (points.length - 1)) * 480;
    const y = 108 - ((item[metric] ?? 0) / max) * 88;
    return { item, x, y };
  });
  return (
    <section className="panel panel--chart" aria-label={title}>
      <div className="panel__header"><h2>{title}</h2></div>
      <div className="panel__body live-chart">
        {coordinates.length === 0 ? (
          <p className="overview__compact-note">No reported monthly values in this window.</p>
        ) : (
          <>
            <svg viewBox="0 0 520 132" role="img" aria-label={`${title}, real HMIS monthly values`}>
              {[20, 42, 64, 86, 108].map((y) => <line key={y} x1="20" x2="500" y1={y} y2={y} />)}
              <polyline points={coordinates.map(({ x, y }) => `${x},${y}`).join(" ")} />
              {coordinates.map(({ item, x, y }) => (
                <g key={item.period}>
                  <circle cx={x} cy={y} r="3.5"><title>{item.period}: {formatCount(item[metric])}</title></circle>
                  <text x={x} y="126" textAnchor="middle">{monthLabel(item.period)}</text>
                </g>
              ))}
            </svg>
            <p className="overview__compact-note">
              {showRate
                ? `Latest positivity: ${formatRate(points.at(-1)?.positivity_rate)}`
                : `Latest reported count: ${formatCount(points.at(-1)?.confirmed_malaria)}`}
            </p>
          </>
        )}
      </div>
    </section>
  );
}

function LiveIssuesTable({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  const alerts = snapshot.operational_alerts ?? [];
  return (
    <section className="panel" aria-labelledby="live-issues-heading">
      <div className="panel__header"><h2 id="live-issues-heading">Current source issues</h2></div>
      <div className="panel__body">
        {alerts.length === 0 ? (
          <p className="overview__compact-note">No directly observed source issue in this period.</p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Issue</th><th>Location</th><th>Status</th><th>Evidence</th></tr></thead>
              <tbody>
                {alerts.slice(0, 8).map((item) => (
                  <tr key={item.id}>
                    <th>{item.title}</th>
                    <td>{item.facility_name}</td>
                    <td><span className={`chip chip--${item.status === "action_required" ? "priority" : "attention"}`}>{item.status.replace("_", " ")}</span></td>
                    <td>{item.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function formatCount(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString();
}

function formatRate(value: number | null | undefined): string {
  return value == null ? "not derivable" : `${value.toFixed(1)}%`;
}

function monthLabel(period: string): string {
  const month = Number(period.slice(4, 6));
  return ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month] ?? period;
}

function modeLine(
  snap: Snapshot | undefined,
  user: ReturnType<typeof useAuth>["user"],
  live: Schemas["LiveDashboardSnapshot"] | null | undefined,
  synchronizing: boolean,
): string {
  if (user?.source_status?.mode === "live") {
    if (user.source_status.authentication !== "connected") {
      return "CONNECTION ISSUE — eRegisters unavailable";
    }
    if (synchronizing) return "LIVE — synchronizing authorised eRegisters data";
    if (live?.status === "synchronized") return "LIVE — eRegisters data synchronized";
    if (live?.status === "partial") return "LIVE — partial source data synchronized";
    if (live?.status === "unavailable") {
      return "LIVE SOURCE — no records returned for the selected period";
    }
    return "CONNECTED — live data synchronization pending";
  }
  if (!snap) return "Loading source status.";
  if (snap.data_mode === "synthetic") {
    return "Development session · synthetic data · not a live Ministry feed.";
  }
  if (snap.data_mode === "live") return snap.data_mode_detail;
  return "Source connected, no synchronisation yet.";
}

function sourceFreshness(
  snap: Snapshot | undefined,
  user: ReturnType<typeof useAuth>["user"],
  live: Schemas["LiveDashboardSnapshot"] | null | undefined,
): string {
  if (user?.is_synthetic || snap?.data_mode === "synthetic") {
    return "Synthetic demonstration data";
  }
  if (live?.status === "synchronized") return "Live source synchronized";
  if (live?.status === "partial") return "Partial live source data";
  if (snap?.last_successful_synchronization) return "Synchronised";
  return "Last sync: Not yet run";
}

function scopeLabel(snap: Snapshot | undefined, national: boolean): string {
  if (!snap) return national ? "National" : "Assigned scope";
  if (snap.requested_scope === "national") return "National";
  if (snap.requested_scope === "pader") return "Pader";
  return snap.requested_scope;
}

function priorityByUnit(snap: Snapshot | undefined): Map<string, string> {
  const highest = new Map<string, string>();
  for (const signal of snap?.recent_signals.items ?? []) {
    if (!signal.geography_unit_id) continue;
    const current = highest.get(signal.geography_unit_id);
    if (!current || rank(signal.priority) < rank(current)) {
      highest.set(signal.geography_unit_id, signal.priority);
    }
  }
  return highest;
}

function rank(priority: string): number {
  return ["urgent", "high", "attention", "informational", "unclassified"].indexOf(priority);
}

function BucketPanel({
  title,
  section,
  empty,
  href,
  linkLabel,
}: {
  title: string;
  section: Schemas["BucketSection"] | undefined;
  empty: string;
  href?: string;
  linkLabel?: string;
}) {
  const items = section?.items ?? [];
  const max = Math.max(1, ...items.map((item) => item.count ?? 0));
  const blocked = !section || section.availability === "not_configured";
  return (
    <section className="panel" aria-label={title}>
      <div className="panel__header">
        <h2>{title}</h2>
        {href ? (
          <Link className="overview__panel-link" to={href}>
            {linkLabel} →
          </Link>
        ) : null}
      </div>
      <div className="panel__body">
        {blocked ? (
          <p className="overview__compact-note" title={section?.refusal_reason ?? empty}>
            {empty}
          </p>
        ) : items.length === 0 ? (
          <p className="overview__compact-note">{empty}</p>
        ) : (
          <ul className="bucket-list">
            {items.map((item) => (
              <li key={item.code} className="bucket-list__row">
                <span className="bucket-list__label">{item.label}</span>
                <span className="bucket-list__track" aria-hidden="true">
                  <span
                    className={`bucket-list__fill bucket-list__fill--${item.code}`}
                    style={{ width: `${item.count == null ? 0 : (item.count / max) * 100}%` }}
                  />
                </span>
                <span className="bucket-list__count mono">
                  {item.count === null ? "—" : item.count}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function DistrictPanel({ section }: { section: Schemas["DistrictSection"] | undefined }) {
  const items = section?.items ?? [];
  return (
    <section className="panel" aria-labelledby="district-review-heading">
      <div className="panel__header">
        <h2 id="district-review-heading">Districts requiring review</h2>
      </div>
      <div className="panel__body">
        {items.length === 0 ? (
          <p className="overview__compact-note" title={section?.refusal_reason ?? undefined}>
            No districts queued
          </p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <caption className="visually-hidden">Districts ordered by active signal count</caption>
              <thead>
                <tr>
                  <th scope="col">District</th>
                  <th scope="col">Signals</th>
                  <th scope="col">Commodity</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.geography_unit_id}>
                    <th scope="row">
                      <Link to={`/workspaces/districts/${row.geography_unit_id}`}>{row.name}</Link>
                    </th>
                    <td>{row.active_signals}</td>
                    <td>{row.commodity_alerts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function CommodityPanel({ section }: { section: Schemas["CommoditySection"] | undefined }) {
  const items = section?.items ?? [];
  return (
    <section className="panel" aria-labelledby="commodity-heading">
      <div className="panel__header">
        <h2 id="commodity-heading">Commodity security</h2>
      </div>
      <div className="panel__body">
        {items.length === 0 ? (
          <p className="overview__compact-note">No commodity alerts</p>
        ) : (
          <ul className="alert-list">
            {items.map((alert) => (
              <li key={alert.id}>
                <strong>{commodityLabel(alert)}</strong>
                <span>{commodityStatement(alert)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function ChartPlaceholder({
  title,
  section,
}: {
  title: string;
  section: Schemas["ChartSection"] | undefined;
}) {
  return (
    <section className="panel panel--chart" aria-labelledby={`${slug(title)}-heading`}>
      <div className="panel__header">
        <h2 id={`${slug(title)}-heading`}>{title}</h2>
      </div>
      <div className="panel__body">
        <div className="chart-frame" aria-hidden="true" />
        <p className="overview__compact-note" title={section?.refusal_reason ?? undefined}>
          Not configured
        </p>
      </div>
    </section>
  );
}

function SignalTable({ section }: { section: Schemas["SignalListSection"] | undefined }) {
  const items = section?.items ?? [];
  return (
    <section className="panel" aria-labelledby="recent-signals-heading">
      <div className="panel__header">
        <h2 id="recent-signals-heading">Recent high-priority signals</h2>
      </div>
      <div className="panel__body">
        {items.length === 0 ? (
          <p className="overview__compact-note" title={section?.refusal_reason ?? undefined}>
            No signals in this period
          </p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <caption className="visually-hidden">Recent active signals</caption>
              <thead>
                <tr>
                  <th scope="col">Signal</th>
                  <th scope="col">Type</th>
                  <th scope="col">Priority</th>
                  <th scope="col">Status</th>
                  <th scope="col">Detected</th>
                  <th scope="col">Open</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <th scope="row">{row.title}</th>
                    <td>{row.signal_type.replace(/_/g, " ")}</td>
                    <td>
                      <span className={`chip chip--${priorityTone(row.priority)}`}>{row.priority}</span>
                    </td>
                    <td>{row.status}</td>
                    <td className="mono">{row.generated_at.slice(0, 10)}</td>
                    <td>
                      <Link to={`/signals/${row.id}`}>View</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function commodityLabel(alert: Schemas["AnalyticalRecordSummary"]): string {
  const label = alert.details.commodity_label;
  return typeof label === "string" ? label : alert.code;
}

function commodityStatement(alert: Schemas["AnalyticalRecordSummary"]): string {
  const statement = alert.details.statement;
  return typeof statement === "string" ? statement : alert.value_status;
}

function slug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function priorityTone(priority: string): string {
  switch (priority) {
    case "urgent":
    case "high":
    case "very high":
      return "priority";
    case "attention":
    case "moderate":
      return "attention";
    default:
      return "info";
  }
}
