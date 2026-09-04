/**
 * Operational list pages that share the overview's APIs rather than inventing figures.
 */

import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../api/client";
import { EmptyState, LoadingState } from "../../design-system/States";
import { PeriodControl } from "../../design-system/Surveillance";
import { monthPeriod, type PeriodSelection } from "../../design-system/period";

function usePeriod(): [PeriodSelection, (period: PeriodSelection) => void] {
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  return [period, setPeriod];
}

export function SignalsListView() {
  const [period, setPeriod] = usePeriod();
  const query = useQuery({
    queryKey: ["signals", period],
    queryFn: () =>
      api.signals({ period_from: period.start, period_to: period.end, active_only: true, limit: 50 }),
  });
  return (
    <ListPage
      title="Signals"
      period={period}
      onPeriod={setPeriod}
      loading={query.isLoading}
      empty={!query.data?.length}
      emptyTitle="No active signals"
      emptyDescription="An empty register is not a zero until the signal method is configured."
    >
      <table className="table">
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
      </table>
    </ListPage>
  );
}

export function CommoditiesView() {
  const [period, setPeriod] = usePeriod();
  const query = useQuery({
    queryKey: ["commodities", period],
    queryFn: () =>
      api.commodityAlerts({ period_from: period.start, period_to: period.end, limit: 50 }),
  });
  return (
    <ListPage
      title="Commodities"
      period={period}
      onPeriod={setPeriod}
      loading={query.isLoading}
      empty={!query.data?.length}
      emptyTitle="No commodity alerts"
      emptyDescription="Commodity alerts stay separate from epidemiological signals."
    >
      <ul>
        {(query.data ?? []).map((row) => (
          <li key={row.id}>
            {typeof row.details.commodity_label === "string" ? row.details.commodity_label : row.code}
            {typeof row.details.statement === "string" ? ` — ${row.details.statement}` : ""}
          </li>
        ))}
      </ul>
    </ListPage>
  );
}

export function AnalyticsView() {
  const [period, setPeriod] = usePeriod();
  const query = useQuery({
    queryKey: ["analytics", "testing", period],
    queryFn: () =>
      api.analyticalResults("testing", { period_from: period.start, period_to: period.end, limit: 25 }),
  });
  return (
    <ListPage
      title="Analytics"
      period={period}
      onPeriod={setPeriod}
      loading={query.isLoading}
      empty={!query.data?.length}
      emptyTitle="No governed testing results"
      emptyDescription="This page lists server-computed records. It does not calculate rates."
    >
      <p>
        <Link to="/command-centre">Return to overview</Link>
      </p>
    </ListPage>
  );
}

export function DataQualityView() {
  const [period, setPeriod] = usePeriod();
  const query = useQuery({
    queryKey: ["provenance", period],
    queryFn: () => api.surveillanceProvenance({ period_start: period.start, period_end: period.end }),
  });
  return (
    <div className="page">
      <header className="page__header">
        <h1>Data quality</h1>
        <PeriodControl period={period} onChange={setPeriod} />
      </header>
      {query.isLoading ? <LoadingState label="Loading provenance" /> : null}
      {query.data ? (
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

export function ReportsView() {
  const [period, setPeriod] = usePeriod();
  const range = useMemo(() => period, [period]);
  const href =
    `/api/v1/reports/national_brief/export.csv` +
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
        Download national brief (CSV)
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
