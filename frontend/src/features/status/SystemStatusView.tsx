/**
 * System status.
 *
 * The one view in this build that shows live data, because it is the one thing
 * MARS can honestly report yet: what this deployment is, which dependencies
 * answer, and which governed methods are in force. Everything here is read from
 * the API - no figure on this page is written into the frontend.
 */

import { useQuery } from "@tanstack/react-query";

import { ApiError, api } from "../../api/client";
import { LoadingState, UnavailableState } from "../../design-system/States";
import "./status.css";

export function SystemStatusView() {
  const readiness = useQuery({
    queryKey: ["health", "ready"],
    queryFn: api.readiness,
    // Readiness is a live probe; a cached answer would be misleading.
    staleTime: 0,
    refetchInterval: 30_000,
    retry: false,
  });

  const version = useQuery({
    queryKey: ["meta", "version"],
    queryFn: api.version,
    staleTime: 60_000,
  });

  const lanes = useQuery({
    queryKey: ["meta", "evidence-lanes"],
    queryFn: api.evidenceLanes,
    staleTime: Infinity,
  });

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">Deployment</p>
          <h1>System status</h1>
          <p className="page__lede">
            What this MARS deployment is, which dependencies are answering, and which
            governed methods are currently in force.
          </p>
        </div>
      </header>

      <section className="panel" aria-labelledby="dependencies-heading">
        <div className="panel__header">
          <h2 id="dependencies-heading">Dependencies</h2>
          {readiness.data ? (
            <span className={`chip chip--${readinessTone(readiness.data.status)}`}>
              {readiness.data.status}
            </span>
          ) : null}
        </div>
        <div className="panel__body panel__body--flush">
          {readiness.isPending ? (
            <div className="panel__body">
              <LoadingState label="dependency status" rows={3} />
            </div>
          ) : readiness.isError ? (
            <div className="panel__body">
              <UnavailableState
                title="Readiness could not be determined"
                description="The MARS API did not answer the readiness probe."
                requestId={
                  readiness.error instanceof ApiError ? readiness.error.requestId : null
                }
                onRetry={() => void readiness.refetch()}
              />
            </div>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">
                  Backing services and their current state
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Service</th>
                    <th scope="col">State</th>
                    <th scope="col">Version</th>
                    <th scope="col">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {readiness.data.dependencies.map((dependency) => (
                    <tr key={dependency.name}>
                      <th scope="row" className="mono">
                        {dependency.name}
                      </th>
                      <td>
                        <span className={`chip chip--${dependencyTone(dependency.status)}`}>
                          {dependency.status.replace(/_/g, " ")}
                        </span>
                      </td>
                      <td className="mono">{dependency.version ?? "-"}</td>
                      <td>{dependency.detail ?? "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="build-heading">
        <div className="panel__header">
          <h2 id="build-heading">Build</h2>
        </div>
        <div className="panel__body">
          {version.isPending ? (
            <LoadingState label="build information" rows={2} />
          ) : version.isError ? (
            <UnavailableState
              title="Build information unavailable"
              description="The metadata endpoint did not respond."
              requestId={version.error instanceof ApiError ? version.error.requestId : null}
              onRetry={() => void version.refetch()}
            />
          ) : (
            <>
              <dl className="definition-grid">
                <div>
                  <dt>Release</dt>
                  <dd className="mono">{version.data.release_version}</dd>
                </div>
                <div>
                  <dt>Commit</dt>
                  <dd className="mono">{version.data.git_sha}</dd>
                </div>
                <div>
                  <dt>Environment</dt>
                  <dd className="mono">{version.data.environment}</dd>
                </div>
                <div>
                  <dt>API version</dt>
                  <dd className="mono">{version.data.api_version}</dd>
                </div>
                <div>
                  <dt>Display timezone</dt>
                  <dd className="mono">{version.data.display_timezone}</dd>
                </div>
                <div>
                  <dt>AI assistant</dt>
                  <dd>{version.data.ai_assistant_enabled ? "Enabled" : "Disabled"}</dd>
                </div>
              </dl>

              <div className="status__registries">
                <div>
                  <p className="label">Active method versions</p>
                  {(version.data.active_method_versions ?? []).length === 0 ? (
                    <p className="status__empty">
                      None. No analytical method has been defined, validated or
                      approved yet - the registry lands in a later phase.
                    </p>
                  ) : (
                    <ul className="status__list mono">
                      {(version.data.active_method_versions ?? []).map((method) => (
                        <li key={method}>{method}</li>
                      ))}
                    </ul>
                  )}
                </div>

                <div>
                  <p className="label">Active configuration keys</p>
                  {(version.data.active_configuration_keys ?? []).length === 0 ? (
                    <p className="status__empty">
                      None. Surveillance windows and thresholds are programme
                      decisions and are recorded here once supplied and approved.
                    </p>
                  ) : (
                    <ul className="status__list mono">
                      {(version.data.active_configuration_keys ?? []).map((key) => (
                        <li key={key}>{key}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="lanes-heading">
        <div className="panel__header">
          <h2 id="lanes-heading">Evidence lanes</h2>
          <span className="chip chip--neutral">Scientific boundary</span>
        </div>
        <div className="panel__body">
          {lanes.isPending ? (
            <LoadingState label="evidence lane definitions" rows={2} />
          ) : lanes.isError ? (
            <UnavailableState
              title="Evidence lane definitions unavailable"
              description="The metadata endpoint did not respond."
              onRetry={() => void lanes.refetch()}
            />
          ) : (
            <EvidenceLanes payload={lanes.data} />
          )}
        </div>
      </section>
    </div>
  );
}

interface Lane {
  id: string;
  label: string;
  sources: string[];
  produces: string;
  boundary: string;
  permitted_language?: string[];
}

/**
 * Render the two evidence lanes from the API.
 *
 * Rendered from the server's definition rather than written into the frontend,
 * so the dashboard, generated reports and documentation cannot disagree about
 * where the scientific boundary sits.
 */
function EvidenceLanes({ payload }: { payload: Record<string, unknown> }) {
  const lanes = (payload.lanes ?? []) as Lane[];

  return (
    <div className="status__lanes">
      {lanes.map((lane) => (
        <article key={lane.id} className="status__lane">
          <header>
            <span
              className={
                lane.id === "confirmed_evidence" ? "chip chip--priority" : "chip chip--info"
              }
            >
              {lane.id === "confirmed_evidence" ? "Lane B" : "Lane A"}
            </span>
            <h3>{lane.label}</h3>
          </header>
          <p className="status__lane-produces">{lane.produces}</p>
          <p className="label">Sources</p>
          <ul className="status__list">
            {lane.sources.map((source) => (
              <li key={source}>{source}</li>
            ))}
          </ul>
          <p className="status__lane-boundary">{lane.boundary}</p>
        </article>
      ))}
    </div>
  );
}

function readinessTone(status: string): string {
  if (status === "ready") return "ok";
  if (status === "degraded") return "attention";
  return "priority";
}

function dependencyTone(status: string): string {
  switch (status) {
    case "ok":
      return "ok";
    case "not_installed":
      return "attention";
    case "unavailable":
      return "priority";
    default:
      return "neutral";
  }
}
