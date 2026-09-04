/**
 * Remotely authorized live workspace while local mapping or data sync is pending.
 *
 * Does not query local surveillance KPIs. Does not invent figures.
 */

import { Navigate, useParams } from "react-router-dom";

import { useAuth } from "../../auth/context";
import { isDhis2Uid } from "../../auth/landing";
import "./live-remote-workspace.css";

export function LiveRemoteWorkspaceView() {
  const { user } = useAuth();
  const params = useParams<{ dhis2Uid?: string }>();

  if (!user) return <Navigate to="/sign-in" replace />;

  const workspace = user.workspace;
  if (!workspace || workspace.authorization_status !== "resolved") {
    return <Navigate to="/no-authorised-scope" replace />;
  }

  if (params.dhis2Uid) {
    if (!isDhis2Uid(params.dhis2Uid)) {
      return <Navigate to={user.landing_path || "/no-authorised-scope"} replace />;
    }
    if (workspace.external_uid && params.dhis2Uid !== workspace.external_uid) {
      return <Navigate to={user.landing_path || "/"} replace />;
    }
  }

  const place = workspace.name?.trim() || "Authorized";
  const placeKind = workspaceKindLabel(workspace.scope_type);
  const title = `${place} Live Pilot`;
  const mappingPending = user.mapping?.status !== "resolved";
  const syncPending = user.data_readiness?.aggregate_sync !== "ready";

  return (
    <div className="page overview live-workspace">
      <header className="overview__header">
        <div>
          <h1>{title}</h1>
          <p className="page__lede">
            {user.display_name}
            {place ? ` · ${place} ${placeKind}` : ""}
          </p>
        </div>
        <p className="live-workspace__env" role="status">
          LIVE
        </p>
      </header>

      <p className="overview__mode overview__mode--live" role="status">
        {statusLine(mappingPending, syncPending)}
      </p>

      <section className="live-workspace__status" aria-label="Authorization and mapping status">
        <StatusRow
          label={`${place} authorization confirmed`}
          detail="Remote eRegisters data-view scope is resolved."
        />
        <StatusRow
          label={mappingPending ? "Geography mapping pending" : "Geography mapping resolved"}
          detail={
            mappingPending
              ? "No confirmed DHIS2 to MARS geography crosswalk. Local surveillance queries are withheld."
              : "Local geography is mapped. Live aggregate data still requires an approved synchronization."
          }
        />
        <StatusRow
          label="Malaria metadata mapping pending"
          detail="Data elements, datasets and category combinations have not been approved."
        />
        <StatusRow
          label="Live data synchronization pending"
          detail="Authentication is not data synchronization. Last sync: Not yet run."
        />
      </section>

      <section className="panel live-workspace__map" aria-label="Geography">
        <header className="panel__header">
          <h2>Map</h2>
          <p className="panel__lede">Public boundaries render only after a confirmed local mapping.</p>
        </header>
        <div className="live-workspace__map-pending">
          {place} boundary mapping pending
        </div>
      </section>
    </div>
  );
}

function workspaceKindLabel(scopeType: string): string {
  if (scopeType === "district") return "District";
  if (scopeType === "facility") return "Facility";
  if (scopeType === "national") return "National";
  if (scopeType === "multi_district") return "Authorized scope";
  return "";
}

function statusLine(mappingPending: boolean, syncPending: boolean): string {
  if (mappingPending) return "AUTHORIZED — MAPPING PENDING";
  if (syncPending) return "AUTHORIZED — DATA SYNC PENDING";
  return "AUTHORIZED — LIVE DATA AVAILABLE";
}

function StatusRow({ label, detail }: { label: string; detail: string }) {
  return (
    <article className="live-workspace__row">
      <h3>{label}</h3>
      <p>{detail}</p>
    </article>
  );
}
