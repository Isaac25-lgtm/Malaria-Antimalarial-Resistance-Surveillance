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

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

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
  type PeriodSelection,
} from "../../design-system/period";
import { useAuth } from "../../auth/context";
import { Breadcrumbs } from "../../design-system/Breadcrumbs";
import { LiveSnapshotStatus } from "../operations/LiveSnapshotStatus";
import { useLiveDashboard } from "../operations/useLiveDashboard";
import { useReportingPeriod } from "../operations/useReportingPeriod";
import "./workspace.css";

export function FacilityWorkspaceView() {
  const { facilityId = "" } = useParams<{ facilityId: string }>();
  const [period, setPeriod] = useReportingPeriod();
  const { user } = useAuth();
  const live = useLiveDashboard(period);
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );

  const facility = useQuery({
    queryKey: ["facility", facilityId],
    queryFn: () => api.facility(facilityId),
    enabled: Boolean(facilityId) && !live.liveMode,
    retry: false,
  });

  const summary = useQuery({
    queryKey: ["surveillance", "facility-summary", facilityId, range],
    queryFn: () => api.facilitySummary(facilityId, range),
    enabled: Boolean(facilityId) && !live.liveMode,
    retry: false,
  });

  const provenance = useQuery({
    queryKey: ["surveillance", "provenance", range],
    queryFn: () => api.surveillanceProvenance(range),
    enabled: !live.liveMode,
    retry: false,
  });

  if (live.liveMode) {
    return (
      <LiveFacilityWorkspace
        facilityId={facilityId}
        period={period}
        setPeriod={setPeriod}
        live={live}
      />
    );
  }

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

function LiveFacilityWorkspace({
  facilityId,
  period,
  setPeriod,
  live,
}: {
  facilityId: string;
  period: PeriodSelection;
  setPeriod: (period: PeriodSelection) => void;
  live: ReturnType<typeof useLiveDashboard>;
}) {
  const facility = live.data?.facilities.find((item) => item.uid === facilityId);
  const patients = (live.data?.positive_patients ?? []).filter(
    (patient) => patient.facility_name === facility?.name,
  );
  const positivity = validRate(
    facility?.confirmed_malaria,
    facility?.tested_for_malaria,
  );
  return (
    <div className="page workspace">
      <Breadcrumbs trail={[{ to: "/command-centre", label: `${live.data?.scope ?? "Authorised scope"} Overview` }, { label: facility?.name ?? "Facility" }]} />
      <header className="page__header workspace__header">
        <div>
          <p className="label">Live facility, {live.data?.scope ?? "authorised scope"}</p>
          <h1>{facility?.name ?? "Facility"}</h1>
          <p className="page__lede">Reported results for this authorised eRegisters facility only.</p>
        </div>
        <PeriodControl period={period} onChange={setPeriod} />
      </header>
      <LiveSnapshotStatus live={live} />
      {!live.isLoading && live.data && !facility ? (
        <NoDataState
          title="Facility is outside this synchronized snapshot"
          description="The requested facility UID is not present in the current authorised facility set."
          awaiting="an authorised facility in the selected reporting period"
        />
      ) : facility ? (
        <>
          <section className="panel" aria-labelledby="live-facility-measures">
            <div className="panel__header"><h2 id="live-facility-measures">Facility measures</h2></div>
            <div className="panel__body">
              <dl>
                <dt>Tested for malaria</dt><dd>{count(facility.tested_for_malaria)}</dd>
                <dt>Confirmed malaria</dt><dd>{count(facility.confirmed_malaria)}</dd>
                <dt>Positivity among reported tests</dt><dd>{positivity}</dd>
                <dt>HMIS return</dt><dd>{facility.aggregate_reported ? "Reported" : "No value returned"}</dd>
                <dt>Tracker retrieval</dt><dd>{facility.tracker_reported ? "At least one event returned" : "No event returned"}</dd>
                <dt>RDT stock-out days</dt><dd>{count(facility.rdt_days_out_of_stock)}</dd>
                <dt>AL stock-out days</dt><dd>{count(facility.al_days_out_of_stock)}</dd>
                <dt>Artesunate stock-out days</dt><dd>{count(facility.artesunate_days_out_of_stock)}</dd>
              </dl>
            </div>
          </section>
          <section className="panel" aria-labelledby="live-facility-patients">
            <div className="panel__header"><h2 id="live-facility-patients">Positive-patient evidence last recorded here</h2></div>
            <div className="panel__body">
              {patients.length ? <ul>{patients.map((patient) => <li key={patient.mars_patient_id}><Link to={`/patients/${patient.mars_patient_id}`}>{patient.mars_patient_id}</Link> — latest positive {patient.latest_positive_on}</li>)}</ul> : <p>No mapped positive-patient evidence in this snapshot names this facility as the latest positive location.</p>}
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function count(value: number | null | undefined): string {
  return value == null ? "Not reported" : value.toLocaleString("en-GB");
}

function validRate(confirmed: number | null | undefined, tested: number | null | undefined): string {
  if (confirmed == null || tested == null || tested <= 0 || confirmed > tested) {
    return "Not available";
  }
  return `${(100 * confirmed / tested).toFixed(1)}%`;
}
