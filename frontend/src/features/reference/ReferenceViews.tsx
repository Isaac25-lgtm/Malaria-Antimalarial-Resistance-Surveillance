/**
 * Reference-data workspaces: geography, organisation units and facilities.
 *
 * All three are empty in this build, and each says so specifically. The
 * distinction matters: "no geography has been imported" is a different fact
 * from "no facility master has been supplied", and a reader deciding what to
 * chase next needs to know which one they are looking at.
 */

import { useQuery } from "@tanstack/react-query";

import { ApiError, api } from "../../api/client";
import { useAuth } from "../../auth/context";
import {
  ForbiddenState,
  LoadingState,
  NoDataState,
  UnavailableState,
} from "../../design-system/States";
import "../status/status.css";

/** Map an API failure onto the state that actually describes it. */
function renderError(error: unknown, onRetry: () => void) {
  if (error instanceof ApiError && error.isForbidden) {
    return (
      <ForbiddenState
        requirement={error.requirement ?? "additional access"}
        description={error.problem?.detail ?? undefined}
      />
    );
  }
  return (
    <UnavailableState
      title="This view could not be loaded"
      description={
        error instanceof ApiError
          ? (error.problem?.detail ?? error.message)
          : "An unexpected error occurred."
      }
      requestId={error instanceof ApiError ? error.requestId : null}
      onRetry={onRetry}
    />
  );
}

export function GeographyView() {
  const overview = useQuery({
    queryKey: ["geography", "overview"],
    queryFn: api.geographyOverview,
    retry: false,
  });

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">Reference data</p>
          <h1>Administrative geography</h1>
          <p className="page__lede">
            The hierarchy MARS analyses against: country, region, district, county and
            subcounty, with parish and village supported but not supplied.
          </p>
        </div>
      </header>

      <section className="panel" aria-labelledby="levels-heading">
        <div className="panel__header">
          <h2 id="levels-heading">Hierarchy levels</h2>
        </div>
        <div className="panel__body panel__body--flush">
          {overview.isPending ? (
            <div className="panel__body">
              <LoadingState label="the geography hierarchy" rows={4} />
            </div>
          ) : overview.isError ? (
            <div className="panel__body">
              {renderError(overview.error, () => void overview.refetch())}
            </div>
          ) : (
            <>
              <div className="table-scroll">
                <table className="table">
                  <caption className="visually-hidden">
                    Geography levels and the number of units loaded at each
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Level</th>
                      <th scope="col">Units loaded</th>
                      <th scope="col">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.data.levels.map((level) => (
                      <tr key={level.level}>
                        <th scope="row">{level.level}</th>
                        <td className="numeric">{level.count}</td>
                        <td>
                          {level.count === 0 ? (
                            <span className="chip chip--unavailable">Not loaded</span>
                          ) : (
                            <span className="chip chip--ok">Loaded</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="panel__body">
                <div className="notice notice--info">
                  <div>
                    <div className="notice__title">Why parish and village read zero</div>
                    <div>{overview.data.note}</div>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="boundary-heading">
        <div className="panel__header">
          <h2 id="boundary-heading">Boundary versions</h2>
        </div>
        <div className="panel__body">
          {overview.data && overview.data.boundary_versions.length === 0 ? (
            <NoDataState
              title="No boundary version has been registered"
              description={
                "The supplied Uganda boundary files are held outside Git with a tracked " +
                "checksum manifest. Run the geography importer to publish a version."
              }
              awaiting="a geography import"
            />
          ) : null}
        </div>
      </section>
    </div>
  );
}

export function OrganisationView() {
  const units = useQuery({
    queryKey: ["organisation", "units"],
    queryFn: () => api.organisationUnits({ limit: 50 }),
    retry: false,
  });

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">Reference data</p>
          <h1>Organisation units</h1>
          <p className="page__lede">
            The health-sector management hierarchy. Modelled separately from
            administrative geography: a Health Sub-District is a health-sector unit and
            is not assumed to coincide with a county.
          </p>
        </div>
      </header>

      <section className="panel">
        <div className="panel__header">
          <h2>Units</h2>
        </div>
        <div className="panel__body">
          {units.isPending ? (
            <LoadingState label="organisation units" rows={3} />
          ) : units.isError ? (
            renderError(units.error, () => void units.refetch())
          ) : units.data.items.length === 0 ? (
            <NoDataState
              title="No organisation units have been defined"
              description={
                "The schema supports national, regional referral, district health office, " +
                "health sub-district and facility units. None has been created, because the " +
                "Ministry's Health Sub-District list has not been supplied."
              }
              awaiting="the MoH organisation unit and HSD list"
            />
          ) : null}
        </div>
      </section>
    </div>
  );
}

export function FacilitiesView() {
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const live = useQuery({
    queryKey: ["live", "dashboard", "latest"],
    queryFn: () => api.latestLiveDashboard(),
    enabled: liveMode,
    retry: false,
  });
  const facilities = useQuery({
    queryKey: ["facilities"],
    queryFn: () => api.facilities({ limit: 50 }),
    enabled: !liveMode,
    retry: false,
  });

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">Reference data</p>
          <h1>Facilities</h1>
          <p className="page__lede">
            Facilities visible within your geography and facility scope. Coordinates are
            shown only when validated - MARS never places a facility approximately.
          </p>
        </div>
      </header>

      <section className="panel">
        <div className="panel__header">
          <h2>Facility list</h2>
        </div>
        <div className="panel__body">
          {liveMode && live.isPending ? (
            <LoadingState label="live facilities" rows={4} />
          ) : liveMode && live.data ? (
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">Authorised eRegisters facilities</caption>
                <thead><tr><th>Facility</th><th>Confirmed malaria</th><th>Tested</th><th>HMIS</th><th>Tracker</th><th>Map point</th></tr></thead>
                <tbody>{live.data.facilities.map((facility) => (
                  <tr key={facility.uid}>
                    <th>{facility.name}</th>
                    <td>{facility.confirmed_malaria?.toLocaleString() ?? "—"}</td>
                    <td>{facility.tested_for_malaria?.toLocaleString() ?? "—"}</td>
                    <td>{facility.aggregate_reported ? "Reported" : "No value returned"}</td>
                    <td>{facility.tracker_reported ? "Reported" : "No event returned"}</td>
                    <td>{facility.latitude != null && facility.longitude != null ? "Available" : "Not published"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : facilities.isPending ? (
            <LoadingState label="facilities" rows={4} />
          ) : facilities.isError ? (
            renderError(facilities.error, () => void facilities.refetch())
          ) : facilities.data.items.length === 0 ? (
            <NoDataState
              title="No facilities are available to you"
              description={
                "This is either because no facility master has been loaded, or because " +
                "none falls within your assigned scope. No official facility list or set " +
                "of coordinates has been supplied to the project."
              }
              awaiting="the national facility master and validated coordinates"
            />
          ) : (
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">Facilities within your scope</caption>
                <thead>
                  <tr>
                    <th scope="col">Code</th>
                    <th scope="col">Name</th>
                    <th scope="col">Level</th>
                    <th scope="col">Coordinates</th>
                    <th scope="col">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {facilities.data.items.map((facility) => (
                    <tr key={facility.id}>
                      <th scope="row" className="mono">
                        {facility.code}
                      </th>
                      <td>{facility.name}</td>
                      <td className="mono">{facility.facility_level}</td>
                      <td>
                        {facility.has_coordinates ? (
                          <span className="chip chip--ok">Validated</span>
                        ) : (
                          <span className="chip chip--unavailable">None</span>
                        )}
                      </td>
                      <td>
                        {facility.is_synthetic ? (
                          <span className="chip chip--attention">Synthetic</span>
                        ) : (
                          <span className="chip chip--neutral">Official</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

export function GovernanceView() {
  const keys = useQuery({
    queryKey: ["governance", "configuration-keys"],
    queryFn: api.configurationKeys,
    retry: false,
  });
  const methods = useQuery({
    queryKey: ["governance", "methods"],
    queryFn: api.methods,
    retry: false,
  });

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">Governance</p>
          <h1>Configuration and methods</h1>
          <p className="page__lede">
            Surveillance windows, thresholds and analytical methods are programme
            decisions under change control. MARS records which version was in force when
            a result was produced; it does not invent one.
          </p>
        </div>
      </header>

      <section className="panel">
        <div className="panel__header">
          <h2>Configuration keys</h2>
        </div>
        <div className="panel__body">
          {keys.isPending ? (
            <LoadingState label="configuration keys" rows={3} />
          ) : keys.isError ? (
            renderError(keys.error, () => void keys.refetch())
          ) : keys.data.length === 0 ? (
            <NoDataState
              title="No configuration keys are registered"
              description={
                "No surveillance window, minimum count or signal weight has been " +
                "recorded. These require malaria programme approval and have not yet " +
                "been supplied."
              }
              awaiting="programme-approved surveillance parameters"
            />
          ) : null}
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2>Method registry</h2>
        </div>
        <div className="panel__body">
          {methods.isPending ? (
            <LoadingState label="the method registry" rows={3} />
          ) : methods.isError ? (
            renderError(methods.error, () => void methods.refetch())
          ) : methods.data.length === 0 ? (
            <NoDataState
              title="No analytical method has been registered"
              description={
                "An empty registry is the accurate state: no indicator, episode rule, " +
                "baseline or signal rule has been defined, let alone validated or approved."
              }
              awaiting="the indicator and analytics phases"
            />
          ) : null}
        </div>
      </section>
    </div>
  );
}
