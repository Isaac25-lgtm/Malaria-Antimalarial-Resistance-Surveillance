/**
 * District surveillance workspace — Prompt 24.
 *
 * The operational view for a district health team, and the first stop when
 * drilling down from the national map.
 *
 * The facility contribution table is the part that earns its place. A district
 * total that falls because a large facility stopped reporting looks exactly
 * like a district total that falls because transmission fell, and the only way
 * to tell them apart is to see who reported. Facilities that reported nothing
 * are listed with a stated absence rather than dropped, because dropping them
 * is what hides the explanation.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api } from "../../api/client";
import { MeasureGrid } from "../../design-system/Measure";
import {
  InterpretationBoundary,
  PeriodControl,
  ProvenanceBar,
} from "../../design-system/Surveillance";
import { Breadcrumbs } from "../../design-system/Breadcrumbs";
import { QueryRegion } from "../../design-system/QueryRegion";
import {
  formatPeriod,
  monthPeriod,
  type PeriodSelection,
} from "../../design-system/period";
import "./workspace.css";

export function DistrictWorkspaceView() {
  const { unitId = "" } = useParams<{ unitId: string }>();
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );

  const unit = useQuery({
    queryKey: ["geography", "unit", unitId],
    queryFn: () => api.geographyUnit(unitId),
    enabled: Boolean(unitId),
    retry: false,
  });

  const summary = useQuery({
    queryKey: ["surveillance", "district-summary", unitId, range],
    queryFn: () => api.districtSummary(unitId, range),
    enabled: Boolean(unitId),
    retry: false,
  });

  const provenance = useQuery({
    queryKey: ["surveillance", "provenance", range],
    queryFn: () => api.surveillanceProvenance(range),
    retry: false,
  });

  const facilities = useQuery({
    queryKey: ["surveillance", "district-facilities", unitId, range],
    queryFn: () => api.districtFacilities(unitId, { ...range, limit: 100 }),
    enabled: Boolean(unitId),
    retry: false,
  });

  const signals = useQuery({
    queryKey: ["signals", "district", unitId, range],
    queryFn: () =>
      api.signals({
        period_from: period.start,
        period_to: period.end,
        active_only: true,
        limit: 25,
      }),
    retry: false,
  });

  const districtName = unit.data?.name ?? "District";

  return (
    <div className="page workspace">
      <Breadcrumbs
        trail={[
          { to: "/command-centre", label: "National" },
          { label: districtName },
        ]}
      />

      <header className="page__header workspace__header">
        <div>
          <p className="label">District workspace</p>
          <h1>{districtName}</h1>
          <p className="page__lede">
            Governed surveillance for {formatPeriod(period)}. Figures are computed for this
            district by MARS; nothing on this page is derived in the browser.
          </p>
        </div>
        <PeriodControl period={period} onChange={setPeriod} />
      </header>

      <ProvenanceBar provenance={provenance.data} />
      <InterpretationBoundary statement={provenance.data?.interpretation_boundary} />

      <section aria-labelledby="district-kpi-heading">
        <h2 id="district-kpi-heading" className="section-heading">
          District measures
        </h2>
        <QueryRegion
          query={summary}
          loadingLabel="Loading district measures"
          emptyTitle="No measures are registered"
          emptyDescription="The indicator catalogue has not been seeded for this deployment."
        >
          {(measures) => <MeasureGrid measures={measures} />}
        </QueryRegion>
      </section>

      <section className="panel" aria-labelledby="contribution-heading">
        <div className="panel__header">
          <h2 id="contribution-heading">Facility contribution</h2>
        </div>
        <div className="panel__body panel__body--flush">
          <p className="panel__lede workspace__lede">
            Which facilities stand behind this district&apos;s figures. A facility that
            reported nothing is listed as such: a district total that falls because a large
            facility stopped reporting looks identical to one that falls because
            transmission fell, and this table is how the two are told apart.
          </p>
          <QueryRegion
            query={facilities}
            loadingLabel="Loading facilities"
            emptyTitle="No facilities are registered for this district"
            emptyDescription="The facility master has no active facility in this geography."
          >
            {(rows) => (
              <div className="table-scroll">
                <table className="table">
                  <caption className="visually-hidden">
                    Facility contribution for {districtName}, {formatPeriod(period)}
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Facility</th>
                      <th scope="col" className="numeric">
                        Attendances
                      </th>
                      <th scope="col">Reporting</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.facility_id}>
                        <th scope="row">
                          <Link to={`/workspaces/facilities/${row.facility_id}`}>
                            {row.name}
                          </Link>
                          {row.code ? (
                            <span className="table__meta mono"> {row.code}</span>
                          ) : null}
                        </th>
                        <td className="numeric">
                          {row.value === null ? "—" : row.value.toLocaleString("en-GB")}
                        </td>
                        <td>
                          {row.status === "available" ? (
                            <span className="chip chip--ok">Reported</span>
                          ) : (
                            <span className="chip chip--unavailable">No return</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </QueryRegion>
        </div>
      </section>

      <section className="panel" aria-labelledby="district-signals-heading">
        <div className="panel__header">
          <h2 id="district-signals-heading">Active signals</h2>
        </div>
        <div className="panel__body panel__body--flush">
          <QueryRegion
            query={signals}
            loadingLabel="Loading signals"
            emptyTitle="No active signals"
            emptyDescription="No governed signal is active for this district and period."
          >
            {(rows) => (
              <div className="table-scroll">
                <table className="table">
                  <caption className="visually-hidden">
                    Active signals for {districtName}
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
                        <td>{row.priority.replace(/_/g, " ")}</td>
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
  );
}
