/**
 * Operational list pages that share the overview's APIs rather than inventing figures.
 */

import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api, type Schemas } from "../../api/client";
import { useAuth } from "../../auth/context";
import { EmptyState, LoadingState, UnavailableState } from "../../design-system/States";
import { useLiveDashboard } from "./useLiveDashboard";
import { PeriodControl } from "../../design-system/Surveillance";
import { monthPeriod, type PeriodSelection } from "../../design-system/period";

function usePeriod(): [PeriodSelection, (period: PeriodSelection) => void] {
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  return [period, setPeriod];
}

export function SignalsListView() {
  const [period, setPeriod] = usePeriod();
  const live = useLiveDashboard(period);
  const query = useQuery({
    queryKey: ["signals", period],
    queryFn: () =>
      api.signals({ period_from: period.start, period_to: period.end, active_only: true, limit: 50 }),
    enabled: !live.liveMode,
  });
  if (live.error || query.error) return <UnavailableState title="Signals could not be loaded" description={(live.error ?? query.error)?.message ?? "Request failed"} onRetry={live.refresh} />;
  return (
    <ListPage
      title="Signals"
      period={period}
      onPeriod={setPeriod}
      loading={query.isLoading || live.isLoading}
      empty={live.liveMode ? !(live.data?.operational_alerts ?? []).length : !query.data?.length}
      emptyTitle="No active signals"
      emptyDescription="An empty register is not a zero until the signal method is configured."
    >
      {live.liveMode && live.data ? <LiveIssueRows snapshot={live.data} /> : <table className="table">
        <thead>
          <tr>
            <th scope="col">Title</th>
            <th scope="col">Priority</th>
            <th scope="col">Type</th>
          </tr>
        </thead>
        <tbody>
          {(query.data ?? []).map((row) => (
            <tr key={row.id}>
              <th scope="row">
                <Link to={`/signals/${row.id}`}>{row.title}</Link>
              </th>
              <td>{row.priority}</td>
              <td>{row.signal_type.replace(/_/g, " ")}</td>
            </tr>
          ))}
        </tbody>
      </table>}
    </ListPage>
  );
}

export function CommoditiesView() {
  const [period, setPeriod] = usePeriod();
  const live = useLiveDashboard(period);
  const query = useQuery({
    queryKey: ["commodities", period],
    queryFn: () =>
      api.commodityAlerts({ period_from: period.start, period_to: period.end, limit: 50 }),
    enabled: !live.liveMode,
  });
  if (live.error || query.error) return <UnavailableState title="Commodities could not be loaded" description={(live.error ?? query.error)?.message ?? "Request failed"} onRetry={live.refresh} />;
  return (
    <ListPage
      title="Commodities"
      period={period}
      onPeriod={setPeriod}
      loading={query.isLoading || live.isLoading}
      empty={live.liveMode ? !(live.data?.operational_alerts ?? []).some((item) => item.kind === "commodity") : !query.data?.length}
      emptyTitle="No commodity alerts"
      emptyDescription="Commodity alerts stay separate from epidemiological signals."
    >
      {live.liveMode && live.data ? <LiveCommodityRows snapshot={live.data} /> : <ul>
        {(query.data ?? []).map((row) => (
          <li key={row.id}>
            {typeof row.details.commodity_label === "string" ? row.details.commodity_label : row.code}
            {typeof row.details.statement === "string" ? ` — ${row.details.statement}` : ""}
          </li>
        ))}
      </ul>}
    </ListPage>
  );
}

export function AnalyticsView() {
  const [period, setPeriod] = usePeriod();
  const live = useLiveDashboard(period);
  const query = useQuery({
    queryKey: ["analytics", "testing", period],
    queryFn: () =>
      api.analyticalResults("testing", { period_from: period.start, period_to: period.end, limit: 25 }),
    enabled: !live.liveMode,
  });
  if (live.error || query.error) return <UnavailableState title="Analytics could not be loaded" description={(live.error ?? query.error)?.message ?? "Request failed"} onRetry={live.refresh} />;
  return (
    <ListPage
      title="Analytics"
      period={period}
      onPeriod={setPeriod}
      loading={query.isLoading || live.isLoading}
      empty={live.liveMode ? !(live.data?.trend ?? []).length : !query.data?.length}
      emptyTitle="No governed testing results"
      emptyDescription="This page lists server-computed records. It does not calculate rates."
    >
      {live.liveMode && live.data ? <LiveAnalyticsTable snapshot={live.data} /> : <p><Link to="/command-centre">Return to overview</Link></p>}
    </ListPage>
  );
}

export function DataQualityView() {
  const [period, setPeriod] = usePeriod();
  const live = useLiveDashboard(period);
  const query = useQuery({
    queryKey: ["provenance", period],
    queryFn: () => api.surveillanceProvenance({ period_start: period.start, period_end: period.end }),
    enabled: !live.liveMode,
  });
  if (live.error || query.error) return <UnavailableState title="Data quality could not be loaded" description={(live.error ?? query.error)?.message ?? "Request failed"} onRetry={live.refresh} />;
  return (
    <div className="page">
      <header className="page__header">
        <h1>Data quality</h1>
        <PeriodControl period={period} onChange={setPeriod} />
      </header>
      {query.isLoading || live.isLoading ? <LoadingState label="Loading provenance" /> : null}
      {live.liveMode && live.data ? (
        <LiveQualityDetails snapshot={live.data} />
      ) : query.data ? (
        <dl>
          <dt>Indicators approved</dt>
          <dd>{query.data.indicators_approved}</dd>
          <dt>Analytically configured</dt>
          <dd>{query.data.analytically_configured ? "yes" : "no"}</dd>
          <dt>Configuration</dt>
          <dd>{query.data.configuration_detail}</dd>
        </dl>
      ) : null}
    </div>
  );
}

function LiveIssueRows({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  return <table className="table"><thead><tr><th>Issue</th><th>Location</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{(snapshot.operational_alerts ?? []).map((item) => <tr key={item.id}><th>{item.title}</th><td>{item.facility_name}</td><td>{item.status.replace("_", " ")}</td><td>{item.detail}</td></tr>)}</tbody></table>;
}

function LiveCommodityRows({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  const rows = (snapshot.operational_alerts ?? []).filter((item) => item.kind === "commodity");
  return <table className="table"><thead><tr><th>Commodity condition</th><th>Facility</th><th>Reported evidence</th></tr></thead><tbody>{rows.map((item) => <tr key={item.id}><th>{item.title}</th><td>{item.facility_name}</td><td>{item.detail}</td></tr>)}</tbody></table>;
}

function LiveAnalyticsTable({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  return <table className="table"><thead><tr><th>Month</th><th>Encounters</th><th>Suspected</th><th>Tested</th><th>Confirmed</th><th>Positivity</th></tr></thead><tbody>{(snapshot.trend ?? []).map((row) => <tr key={row.period}><th>{row.period}</th><td>{number(row.encounters)}</td><td>{number(row.suspected_malaria)}</td><td>{number(row.tested_for_malaria)}</td><td>{number(row.confirmed_malaria)}</td><td>{row.positivity_rate == null ? "—" : `${row.positivity_rate.toFixed(1)}%`}</td></tr>)}</tbody></table>;
}

function LiveQualityDetails({ snapshot }: { snapshot: Schemas["LiveDashboardSnapshot"] }) {
  return <dl><dt>HMIS reporting facilities</dt><dd>{snapshot.aggregate_reporting_facility_count} of {snapshot.facility_count}</dd><dt>Tracker reporting facilities</dt><dd>{snapshot.tracker_reporting_facility_count} of {snapshot.facility_count}</dd><dt>HMIS values read</dt><dd>{snapshot.aggregate_value_count.toLocaleString()}</dd><dt>Tracker events read</dt><dd>{snapshot.tracker_event_count.toLocaleString()}</dd><dt>Mapped malaria tests</dt><dd>{snapshot.malaria_lab_event_count.toLocaleString()}</dd><dt>Mapped positive malaria tests</dt><dd>{(snapshot.positive_malaria_event_count ?? 0).toLocaleString()}</dd><dt>Invalid aggregate values</dt><dd>{snapshot.invalid_aggregate_value_count}</dd></dl>;
}

function number(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString();
}

export function ReportsView() {
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const [period, setPeriod] = usePeriod();
  const range = useMemo(() => period, [period]);
  const href =
    (liveMode ? `/api/v1/live/dashboard/export.csv` : `/api/v1/reports/${user?.has_national_scope ? "national_brief" : "district_brief"}/export.csv`) +
    `?period_start=${range.start}&period_end=${range.end}`;
  return (
    <div className="page">
      <header className="page__header">
        <h1>Reports</h1>
        <PeriodControl period={period} onChange={setPeriod} />
      </header>
      <p>
        Exports are authorised, scoped and audited on the server. A file built in the
        browser would carry none of that.
      </p>
      <a className="button" href={href} download>
        Download {user?.has_national_scope ? "national" : "district"} brief (CSV)
      </a>
    </div>
  );
}

export function AdministrationView() {
  return (
    <div className="page">
      <header className="page__header">
        <h1>Administration</h1>
      </header>
      <ul>
        <li>
          <Link to="/governance">Governance registries</Link>
        </li>
        <li>
          <Link to="/status">System status</Link>
        </li>
        <li>
          <Link to="/profile">Your access</Link>
        </li>
      </ul>
    </div>
  );
}

function ListPage({
  title,
  period,
  onPeriod,
  loading,
  empty,
  emptyTitle,
  emptyDescription,
  children,
}: {
  title: string;
  period: PeriodSelection;
  onPeriod: (period: PeriodSelection) => void;
  loading: boolean;
  empty: boolean;
  emptyTitle: string;
  emptyDescription: string;
  children: ReactNode;
}) {
  return (
    <div className="page">
      <header className="page__header">
        <h1>{title}</h1>
        <PeriodControl period={period} onChange={onPeriod} />
      </header>
      {loading ? <LoadingState label={`Loading ${title.toLowerCase()}`} /> : null}
      {!loading && empty ? (
        <EmptyState title={emptyTitle} description={emptyDescription} />
      ) : (
        children
      )}
    </div>
  );
}
