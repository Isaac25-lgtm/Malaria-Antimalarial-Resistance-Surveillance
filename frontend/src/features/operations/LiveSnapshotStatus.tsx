import { formatMoment } from "../../design-system/period";
import type { useLiveDashboard } from "./useLiveDashboard";

export function LiveSnapshotStatus({ live }: { live: ReturnType<typeof useLiveDashboard> }) {
  if (!live.liveMode) return null;
  const snapshot = live.data;
  return <div className="notice" role="status">
    {live.refreshError ? <p>{live.refreshError.message}</p> : null}
    {live.job ? <p>Refresh: {live.job.status}. Retrieval steps: {live.job.completed_steps}/{live.job.total_steps}.
      {live.synchronization.isPending ? " Retrieval continues when you leave this page." : ""}</p> : null}
    {snapshot ? <p>Snapshot: {snapshot.period_start} to {snapshot.period_end}, retrieved {formatMoment(snapshot.synchronized_at)}.
      {" "}HMIS reporting: {snapshot.aggregate_reporting_facility_count}/{snapshot.facility_count} facilities.
      {" "}Tracker retrieved: {snapshot.tracker_retrieved_facility_count}/{snapshot.facility_count}; reporting: {snapshot.tracker_reporting_facility_count}/{snapshot.facility_count}.
      {!snapshot.retrieval_complete ? " Retrieval coverage is incomplete." : ""}
      {snapshot.status === "partial" ? " Some measures need review; see Data Quality." : ""}
    </p> : !live.isLoading ? <p>No saved snapshot for this period. Open Overview to discover your scope and synchronize.</p> : null}
  </div>;
}
