/**
 * Facility surveillance workspace — Prompt 24.
 *
 * Where aggregate patterns meet the facility that produced them.
 *
 * The rule this screen exists to respect: **a facility workspace shows the
 * facility's own results and nothing it merely sits inside.** Every measure
 * here is computed against this facility's rows. There is no district total on
 * this page, because a district total displayed under a facility heading is
 * the scope inheritance the API has twice been corrected to remove.
 *
 * Pseudonymous case evidence appears only where the account carries the
 * sensitivity tier for it; the panel says so rather than rendering empty.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { api } from "../../api/client";
import { MeasureGrid } from "../../design-system/Measure";
import { QueryRegion } from "../../design-system/QueryRegion";
import { NoDataState } from "../../design-system/States";
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
import { useAuth } from "../../auth/context";
import { Breadcrumbs } from "../../design-system/Breadcrumbs";
import "./workspace.css";

export function FacilityWorkspaceView() {
  const { facilityId = "" } = useParams<{ facilityId: string }>();
  const [period, setPeriod] = useState<PeriodSelection>(() => monthPeriod(-1));
  const { user } = useAuth();
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );

  const facility = useQuery({
    queryKey: ["facility", facilityId],
    queryFn: () => api.facility(facilityId),
    enabled: Boolean(facilityId),
    retry: false,
  });

  const summary = useQuery({
    queryKey: ["surveillance", "facility-summary", facilityId, range],
    queryFn: () => api.facilitySummary(facilityId, range),
    enabled: Boolean(facilityId),
    retry: false,
  });

  const provenance = useQuery({
    queryKey: ["surveillance", "provenance", range],
    queryFn: () => api.surveillanceProvenance(range),
    retry: false,
  });

  const facilityName = facility.data?.name ?? "Facility";
  const districtId = facility.data?.district_geography_unit_id ?? null;
  const mayReadCaseEvidence = user?.permissions.includes("case_evidence:view") ?? false;

  return (
    <div className="page workspace">
      <Breadcrumbs
        trail={[
          { to: "/command-centre", label: "National" },
          ...(districtId
            ? [{ to: `/workspaces/districts/${districtId}`, label: "District" }]
            : []),
          { label: facilityName },
        ]}
      />

      <header className="page__header workspace__header">
        <div>
          <p className="label">Facility workspace</p>
          <h1>{facilityName}</h1>
          <p className="page__lede">
            Governed surveillance for {formatPeriod(period)}, computed from this
            facility&apos;s own results. No district figure appears on this page.
          </p>
        </div>
        <PeriodControl period={period} onChange={setPeriod} />
      </header>

      <ProvenanceBar provenance={provenance.data} />
      <InterpretationBoundary statement={provenance.data?.interpretation_boundary} />

      <section aria-labelledby="facility-kpi-heading">
        <h2 id="facility-kpi-heading" className="section-heading">
          Facility measures
        </h2>
        <QueryRegion
          query={summary}
          loadingLabel="Loading facility measures"
          emptyTitle="No measures are registered"
          emptyDescription="The indicator catalogue has not been seeded for this deployment."
        >
          {(measures) => <MeasureGrid measures={measures} />}
        </QueryRegion>
      </section>

      <section className="panel" aria-labelledby="case-evidence-heading">
        <div className="panel__header">
          <h2 id="case-evidence-heading">Pseudonymous case evidence</h2>
        </div>
        <div className="panel__body">
          {mayReadCaseEvidence ? (
            <NoDataState
              title="Case evidence arrives with the signal workspace"
              description="Flagged-case evidence is rendered on the signal detail screen, where it can be shown beside the analysis that flagged it. MARS patient numbers are used throughout; no direct identifier reaches this interface."
              awaiting="signal evidence workspace"
            />
          ) : (
            <NoDataState
              title="Your account is not authorised for case evidence"
              description="Pseudonymous case evidence requires the case-evidence permission and the pseudonymous-case sensitivity tier. This is a statement about permissions, not about this facility."
              awaiting="case_evidence:view"
            />
          )}
        </div>
      </section>
    </div>
  );
}
