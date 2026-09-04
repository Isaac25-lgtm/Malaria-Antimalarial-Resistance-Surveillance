/**
 * Operational overview — the screen a Ministry user opens first.
 *
 * Geography renders independently of analytics. No figure is computed here.
 * Compact executive states stay in the layout; implementation strings live in
 * tooltips and on Data Quality.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, api, type Schemas } from "../../api/client";
import { MeasureGrid } from "../../design-system/Measure";
import { ForbiddenState, UnavailableState } from "../../design-system/States";
import { PeriodControl } from "../../design-system/Surveillance";
import { formatMoment, monthPeriod, type PeriodSelection } from "../../design-system/period";
import { useAuth } from "../../auth/context";
import { GeographyCanvas } from "../map/GeographyCanvas";
import { boundsOf, decorateCollection, isInScope } from "../map/geography";
import "./command-centre.css";

type Snapshot = Schemas["OverviewSnapshot"];

export function CommandCentreView() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );

  const overview = useQuery({
    queryKey: ["surveillance", "overview", range],
    queryFn: () => api.overview(range),
    retry: false,
  });

  const mapMeta = useQuery({
    queryKey: ["map", "metadata"],
    queryFn: api.mapMetadata,
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
        ? ["map", "features", "subcounty", singleDistrictId]
        : ["map", "features", "district"],
    queryFn: () => {
      if (nationalMap) return api.mapContext({ level: "district" });
      if (singleDistrictId) {
        return api.mapFeatures({ level: "subcounty", within_id: singleDistrictId });
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
  const inScopeUnitIds = user?.has_national_scope
    ? null
    : new Set((user?.geography_scopes ?? []).map((scope) => scope.geography_unit_id));
  const collection = features.data
    ? decorateCollection(features.data, {
        signalPriorityByUnitId: priorityByUnit(snap),
        inScopeUnitIds,
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
        {modeLine(snap, user)}
      </p>

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
          <MeasureGrid measures={executiveKpis(snap.kpis.items)} compact />
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
                    ? "District and subcounty geography"
                    : "Authorised geography"}
              </h2>
              <p className="panel__lede">
                {nationalMap
                  ? "Districts by highest-priority active signal"
                  : "Boundaries inside the authorised scope. Surveillance values stay scoped."}
              </p>
            </div>
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
                  void navigate(`/workspaces/districts/${unitId}`);
                }}
                onHover={() => undefined}
                label="Uganda districts. Colour is a signal overlay on administrative geography."
              />
            ) : (
              <div className="overview__compact-state" role="status">
                {features.isError ? "Map unavailable" : "Geography not published"}
              </div>
            )}
          </div>
          <ul className="overview__legend">
            <li><span className="swatch swatch--urgent" /> Very high</li>
            <li><span className="swatch swatch--high" /> High</li>
            <li><span className="swatch swatch--attention" /> Moderate</li>
            <li><span className="swatch swatch--info" /> Under review</li>
            <li><span className="swatch swatch--none" /> No active signal</li>
            <li><span className="swatch swatch--insufficient" /> No / insufficient data</li>
            <li><span className="swatch swatch--outside" /> Outside authorised scope</li>
          </ul>
        </section>

        <div className="overview__side overview__side--signals">
          <BucketPanel
            title="Signals by priority"
            section={snap?.signals_by_priority}
            href="/signals"
            linkLabel="View all signals"
            empty="Not configured"
          />
          <BucketPanel
            title="Investigations"
            section={snap?.investigations_by_status}
            href="/action-centre"
            linkLabel="View all investigations"
            empty="No records"
          />
        </div>

        <DistrictPanel section={snap?.districts_requiring_review} />
      </div>

      <div className="overview__ops">
        <CommodityPanel section={snap?.commodity_alerts} />
        <BucketPanel
          title="Needs attention"
          section={snap?.needs_attention}
          empty="No items"
        />
      </div>

      <div className="overview__charts">
        <ChartPlaceholder title="Confirmed malaria vs baseline" section={snap?.confirmed_malaria_trend} />
        <ChartPlaceholder title="Testing and positivity rate" section={snap?.testing_positivity} />
      </div>

      <SignalTable section={snap?.recent_signals} />

      <footer className="overview__freshness">
        <span>
          Last sync:{" "}
          {snap?.last_successful_synchronization
            ? formatMoment(snap.last_successful_synchronization)
            : "Not yet run"}
        </span>
        <span>Last updated {formatMoment(snap?.provenance.analytics_refreshed_at ?? null)}</span>
        <span className={`freshness freshness--${snap?.data_mode ?? "unavailable"}`}>
          {sourceFreshness(snap, user)}
        </span>
      </footer>
    </div>
  );
}

function executiveKpis(items: Schemas["SurveillanceMeasure"][]): Schemas["SurveillanceMeasure"][] {
  return items.filter((item) => item.code !== "ACTIVE_SIGNALS").slice(0, 6);
}

function modeLine(
  snap: Snapshot | undefined,
  user: ReturnType<typeof useAuth>["user"],
): string {
  if (user?.source_status?.mode === "live") {
    if (user.source_status.authentication !== "connected") {
      return "CONNECTION ISSUE — eRegisters unavailable";
    }
    if (user.source_status.mapping === "pending") {
      return "LIVE — authentication succeeded; malaria mapping pending";
    }
    return "LIVE — eRegisters connected";
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
): string {
  if (user?.is_synthetic || snap?.data_mode === "synthetic") {
    return "Synthetic demonstration data";
  }
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
