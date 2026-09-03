/**
 * The national MARS command centre — Prompt 23.
 *
 * The screen a Ministry of Health user opens first, which makes it the screen
 * most likely to be believed without being questioned. Three rules follow from
 * that, and they shape everything below.
 *
 * **No figure is computed here.** Every value arrives from
 * `/surveillance/...` or `/analytics/...` as a record carrying its own period,
 * scope, source, method version and availability status. There is no KPI
 * formula in this file to disagree with the server.
 *
 * **An absent figure never renders as a zero.** A fresh deployment has no
 * approved indicator versions, so the honest national screen says "not
 * configured" seven times and names what is missing. A country of zeroes would
 * be a lie told in a very convincing typeface.
 *
 * **Commodity alerts sit apart from signals.** A stock-out needs a district
 * pharmacist; an epidemiological signal needs an investigation. Putting them
 * in one list is how the first quietly becomes the second.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { MeasureGrid } from "../../design-system/Measure";
import {
  EmptyState,
  LoadingState,
  NoDataState,
  UnavailableState,
} from "../../design-system/States";
import {
  InterpretationBoundary,
  PeriodControl,
  ProvenanceBar,
} from "../../design-system/Surveillance";
import {
  formatPeriod,
  monthPeriod,
  type PeriodSelection,
} from "../../design-system/period";
import "./command-centre.css";

export function CommandCentreView() {
  // Default to the last complete month. The current month is always partial,
  // and a partial month read as a whole one looks like a collapse in cases.
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );

  const summary = useQuery({
    queryKey: ["surveillance", "national-summary", range],
    queryFn: () => api.nationalSummary(range),
    retry: false,
  });

  const provenance = useQuery({
    queryKey: ["surveillance", "provenance", range],
    queryFn: () => api.surveillanceProvenance(range),
    retry: false,
  });

  const districts = useQuery({
    queryKey: ["surveillance", "priority-districts", range],
    queryFn: () => api.priorityDistricts({ ...range, limit: 10 }),
    retry: false,
  });

  const signals = useQuery({
    queryKey: ["signals", "active", range],
    queryFn: () =>
      api.signals({
        period_from: period.start,
        period_to: period.end,
        active_only: true,
        limit: 10,
      }),
    retry: false,
  });

  const alerts = useQuery({
    queryKey: ["analytics", "commodity-alerts", range],
    queryFn: () =>
      api.commodityAlerts({
        period_from: period.start,
        period_to: period.end,
        limit: 10,
      }),
    retry: false,
  });

  return (
    <div className="page command-centre">
      <header className="page__header command-centre__header">
        <div>
          <p className="label">National surveillance</p>
          <h1>Command centre</h1>
          <p className="page__lede">
            Governed malaria surveillance for {formatPeriod(period)}. Every figure on this
            page is computed by MARS against an approved definition and carries its own
            period, scope and provenance.
          </p>
        </div>
        <div className="command-centre__actions">
          <PeriodControl period={period} onChange={setPeriod} />
          <ReportLink period={period} />
        </div>
      </header>

      <ProvenanceBar provenance={provenance.data} />

      <InterpretationBoundary statement={provenance.data?.interpretation_boundary} />

      {/* -- KPI strip ------------------------------------------------- */}
      <section aria-labelledby="kpi-heading">
        <h2 id="kpi-heading" className="section-heading">
          Governed measures
        </h2>
        <QueryRegion
          query={summary}
          loadingLabel="Loading governed measures"
          emptyTitle="No measures are registered"
          emptyDescription="The indicator catalogue has not been seeded for this deployment."
        >
          {(measures) => <MeasureGrid measures={measures} />}
        </QueryRegion>
      </section>

      <div className="command-centre__columns">
        {/* -- Priority districts ---------------------------------------- */}
        <section className="panel" aria-labelledby="districts-heading">
          <div className="panel__header">
            <h2 id="districts-heading">Priority districts</h2>
          </div>
          <div className="panel__body panel__body--flush">
            <QueryRegion
              query={districts}
              loadingLabel="Loading districts"
              emptyTitle="No district has an active signal"
              emptyDescription="No governed signal is active for this period within your scope."
            >
              {(rows) => (
                <>
                  <div className="table-scroll">
                    <table className="table">
                      <caption className="visually-hidden">
                        Districts ordered by active signal count for {formatPeriod(period)}
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">District</th>
                          <th scope="col" className="numeric">
                            Active signals
                          </th>
                          <th scope="col" className="numeric">
                            Commodity alerts
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row) => (
                          <tr key={row.geography_unit_id}>
                            <th scope="row">
                              <Link to={`/districts/${row.geography_unit_id}`}>{row.name}</Link>
                              {row.preferred_code ? (
                                <span className="table__meta mono"> {row.preferred_code}</span>
                              ) : null}
                            </th>
                            <td className="numeric">{row.active_signals}</td>
                            <td className="numeric">{row.commodity_alerts}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="panel__footnote">{rows[0]?.ordering_detail}</p>
                </>
              )}
            </QueryRegion>
          </div>
        </section>

        {/* -- Priority signals ------------------------------------------ */}
        <section className="panel" aria-labelledby="signals-heading">
          <div className="panel__header">
            <h2 id="signals-heading">Active signals</h2>
          </div>
          <div className="panel__body panel__body--flush">
            <QueryRegion
              query={signals}
              loadingLabel="Loading signals"
              emptyTitle="No active signals"
              emptyDescription="No governed signal is active for this period within your scope. A signal rule must be approved before any can be generated."
            >
              {(rows) => (
                <div className="table-scroll">
                  <table className="table">
                    <caption className="visually-hidden">
                      Active surveillance signals for {formatPeriod(period)}
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Signal</th>
                        <th scope="col">Priority</th>
                        <th scope="col">Period</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={row.id}>
                          <th scope="row">
                            <Link to={`/signals/${row.id}`}>{row.title}</Link>
                          </th>
                          <td>
                            <span className={`chip chip--${priorityTone(row.priority)}`}>
                              {row.priority.replace(/_/g, " ")}
                            </span>
                          </td>
                          <td className="mono">{row.period_start}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </QueryRegion>
          </div>
        </section>
      </div>

      {/* -- Commodity alerts -------------------------------------------- */}
      <section className="panel" aria-labelledby="commodity-heading">
        <div className="panel__header">
          <h2 id="commodity-heading">Commodity operational alerts</h2>
          <span className="chip chip--info">Supply chain</span>
        </div>
        <div className="panel__body">
          <p className="panel__lede">
            Reported stock conditions. These are operational facts about a store and are
            deliberately kept apart from epidemiological signals: a stock-out needs a
            district pharmacist, not an outbreak investigation.
          </p>
          <QueryRegion
            query={alerts}
            loadingLabel="Loading commodity alerts"
            emptyTitle="No commodity alerts"
            emptyDescription="No facility reported a stock condition meeting an alert definition for this period."
          >
            {(rows) => (
              <div className="table-scroll">
                <table className="table">
                  <caption className="visually-hidden">
                    Commodity operational alerts for {formatPeriod(period)}
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Alert</th>
                      <th scope="col">Period</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.id}>
                        <th scope="row">{row.code.replace(/_/g, " ")}</th>
                        <td className="mono">{row.period_start}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </QueryRegion>
        </div>
      </section>

      {/* -- Investigation summary (Prompt 26) ---------------------------- */}
      <section className="panel" aria-labelledby="investigation-heading">
        <div className="panel__header">
          <h2 id="investigation-heading">Investigations</h2>
        </div>
        <div className="panel__body">
          <NoDataState
            title="Investigation workflow is not part of this build"
            description="Signal triage, assignment and closure arrive with the investigation module. Until then MARS shows no queue rather than an empty one, because an empty queue would suggest there is nothing to investigate."
            awaiting="investigation module"
          />
        </div>
      </section>
    </div>
  );
}

/**
 * A link to the governed national brief.
 *
 * A plain anchor to the server route rather than a client-side assembly: the
 * export is authorised, scoped and audited on the server, and a file built in
 * the browser would carry none of that. The permission is checked there too -
 * hiding a button is not access control.
 */
function ReportLink({ period }: { period: PeriodSelection }) {
  const href =
    `/api/v1/reports/national_brief/export.csv` +
    `?period_start=${period.start}&period_end=${period.end}`;
  return (
    <a className="button" href={href} download>
      Download national brief (CSV)
    </a>
  );
}

/** Priority to a status tone. Colour never carries the meaning alone. */
function priorityTone(priority: string): string {
  switch (priority) {
    case "high":
      return "priority";
    case "medium":
      return "attention";
    case "low":
      return "info";
    default:
      return "unavailable";
  }
}

interface QueryLike<T> {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: T[] | undefined;
}

interface QueryRegionProps<T> {
  query: QueryLike<T>;
  loadingLabel: string;
  emptyTitle: string;
  emptyDescription: string;
  children: (rows: T[]) => React.ReactNode;
}

/**
 * The five states every data region must be able to be in.
 *
 * Loading, forbidden, unavailable, empty and populated are genuinely different
 * answers and are rendered differently. Collapsing "you may not see this" and
 * "there is nothing here" into one blank panel is how a scoped-out district
 * becomes an apparently healthy one.
 */
function QueryRegion<T>({
  query,
  loadingLabel,
  emptyTitle,
  emptyDescription,
  children,
}: QueryRegionProps<T>) {
  if (query.isPending) {
    return <LoadingState label={loadingLabel} />;
  }

  if (query.isError) {
    const error = query.error;
    if (error instanceof ApiError && error.isForbidden) {
      return (
        <NoDataState
          title="Outside your authorised scope"
          description="Your account is not authorised for this information. This is a statement about permissions, not about malaria."
          awaiting={error.requirement ?? undefined}
        />
      );
    }
    return (
      <UnavailableState
        title="This section could not be loaded"
        description={
          error instanceof ApiError
            ? error.message
            : "The server did not answer. The figures are not zero; they are unknown."
        }
      />
    );
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />;
  }

  return <>{children(rows)}</>;
}
