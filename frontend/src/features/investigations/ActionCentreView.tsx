/**
 * The action centre — Prompt 26.
 *
 * Surveillance becomes useful at the point where a signal becomes somebody's
 * work. This screen is the queue of that work.
 *
 * There is no overdue queue, and the page says why. An overdue list needs an
 * approved SLA, and showing an empty one would tell a district officer that
 * nothing is late when the truth is that MARS has not been told what late
 * means. The distinction matters more here than anywhere else in the
 * interface, because this is the screen people work from.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { useAuth } from "../../auth/context";
import { QueryRegion } from "../../design-system/QueryRegion";
import { NoDataState } from "../../design-system/States";
import "./action-centre.css";

const QUEUES = [
  { name: "new", label: "New" },
  { name: "high_priority", label: "High priority" },
  { name: "assigned_to_me", label: "Assigned to me" },
  { name: "under_investigation", label: "Under investigation" },
  { name: "awaiting_external_result", label: "Awaiting external result" },
  { name: "resolved", label: "Resolved" },
] as const;

type QueueName = (typeof QUEUES)[number]["name"];

export function ActionCentreView() {
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const [active, setActive] = useState<QueueName>("new");
  const live = useQuery({
    queryKey: ["live", "dashboard", "latest"],
    queryFn: () => api.latestLiveDashboard(),
    enabled: liveMode,
    retry: false,
  });

  const catalogue = useQuery({
    queryKey: ["investigations", "queues"],
    queryFn: api.investigationQueues,
    retry: false,
  });

  const queue = useQuery({
    queryKey: ["investigations", "queue", active],
    queryFn: () => api.investigationQueue(active),
    retry: false,
  });

  const overdue = catalogue.data?.overdue;

  return (
    <div className="page action-centre">
      <header className="page__header">
        <div>
          <p className="label">Investigations</p>
          <h1>Action centre</h1>
          <p className="page__lede">
            Signals that have become somebody&apos;s work, and what state that work is in.
          </p>
        </div>
      </header>

      {!liveMode && overdue && !overdue.available ? (
        <NoDataState
          title="No overdue queue"
          description={overdue.detail ?? ""}
          awaiting={overdue.missing_configuration.join(", ")}
        />
      ) : null}

      {liveMode && live.data && (live.data.operational_alerts ?? []).length > 0 ? (
        <section className="panel" aria-labelledby="untriaged-live-heading">
          <div className="panel__header"><h2 id="untriaged-live-heading">Untriaged live source issues</h2></div>
          <div className="panel__body table-scroll">
            <table className="table">
              <thead><tr><th>Issue</th><th>Location</th><th>State</th><th>Evidence</th></tr></thead>
              <tbody>{(live.data.operational_alerts ?? []).map((item) => (
                <tr key={item.id}><th>{item.title}</th><td>{item.facility_name}</td><td>Awaiting triage</td><td>{item.detail}</td></tr>
              ))}</tbody>
            </table>
          </div>
        </section>
      ) : null}

      <nav className="action-centre__tabs" aria-label="Investigation queues">
        <ul>
          {QUEUES.map((item) => (
            <li key={item.name}>
              <button
                type="button"
                className="action-centre__tab"
                aria-current={active === item.name ? "true" : undefined}
                onClick={() => {
                  setActive(item.name);
                }}
              >
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <section className="panel" aria-live="polite">
        <div className="panel__header">
          <h2>{QUEUES.find((item) => item.name === active)?.label}</h2>
        </div>
        <div className="panel__body panel__body--flush">
          <QueryRegion
            query={queue}
            loadingLabel="Loading queue"
            emptyTitle="This queue is empty"
            emptyDescription="No investigation in your scope is in this state. That is a statement about the queue, not about whether anything needs looking at."
          >
            {(rows) => (
              <div className="table-scroll">
                <table className="table">
                  <caption className="visually-hidden">Investigations in this queue</caption>
                  <thead>
                    <tr>
                      <th scope="col">Investigation</th>
                      <th scope="col">Status</th>
                      <th scope="col">Priority</th>
                      <th scope="col">Period</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.id}>
                        <th scope="row">
                          <Link to={`/signals/${row.signal_id}`}>
                            {row.id.slice(0, 8)}
                          </Link>
                        </th>
                        <td>{row.investigation_status.replace(/_/g, " ")}</td>
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
  );
}

function priorityTone(priority: string): string {
  switch (priority) {
    case "urgent":
      return "priority";
    case "high":
      return "attention";
    case "attention":
      return "info";
    default:
      return "unavailable";
  }
}
