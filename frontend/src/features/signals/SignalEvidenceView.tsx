/**
 * Signal evidence workspace — Prompt 25.
 *
 * The page that has to prove MARS is explainable. Everything a reader needs to
 * disagree with a signal is on it: what was observed, what was expected, which
 * evidence supported the flag and which argued against it, what was missing,
 * which governed rule and which method version produced it, and the exact
 * fingerprints that make the result reproducible.
 *
 * Counter-evidence gets equal billing with supporting evidence. A screen that
 * shows only what agrees with the flag is an advocacy document, not an
 * analytical one - and the counter-evidence is usually the more useful half
 * when a district officer decides whether to send someone.
 *
 * The explanation is rendered from the deterministic explanation object. There
 * is no generated prose anywhere on this page.
 */

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { api } from "../../api/client";
import { Breadcrumbs } from "../../design-system/Breadcrumbs";
import { LoadingState, NoDataState, UnavailableState } from "../../design-system/States";
import { ApiError } from "../../api/client";
import "./signal.css";

export function SignalEvidenceView() {
  const { signalId = "" } = useParams<{ signalId: string }>();

  const signal = useQuery({
    queryKey: ["signal", signalId],
    queryFn: () => api.signal(signalId),
    enabled: Boolean(signalId),
    retry: false,
  });

  const explanation = useQuery({
    queryKey: ["signal", signalId, "explanation"],
    queryFn: () => api.signalExplanation(signalId),
    enabled: Boolean(signalId),
    retry: false,
  });

  if (signal.isPending) {
    return (
      <div className="page">
        <LoadingState label="signal evidence" />
      </div>
    );
  }

  if (signal.isError) {
    const error = signal.error;
    const forbidden = error instanceof ApiError && (error.isForbidden || error.status === 404);
    return (
      <div className="page">
        {forbidden ? (
          <NoDataState
            title="Signal not found, or outside your authorised scope"
            description="MARS does not distinguish between the two in its answer. Telling a caller that a signal exists but is not theirs to read would itself disclose that something was flagged there."
          />
        ) : (
          <UnavailableState
            title="The signal could not be loaded"
            description="The server did not answer. This is not a statement that the signal was withdrawn."
          />
        )}
      </div>
    );
  }

  const record = signal.data;

  return (
    <div className="page signal">
      <Breadcrumbs
        trail={[
          { to: "/command-centre", label: "National" },
          { label: record.title },
        ]}
      />

      {/* -- Hero ------------------------------------------------------- */}
      <header className="page__header signal__hero">
        <div>
          <p className="label">Surveillance signal</p>
          <h1>{record.title}</h1>
          <p className="page__lede">{record.statement}</p>
        </div>
        <div className="signal__badges">
          <span className={`chip chip--${priorityTone(record.priority)}`}>
            {record.priority.replace(/_/g, " ")}
          </span>
          <span className="chip chip--neutral">{record.status}</span>
        </div>
      </header>

      {/* The permanent boundary, from the signal's own uncertainty list. */}
      {record.uncertainty.length > 0 ? (
        <aside className="boundary" aria-label="Interpretation boundary">
          {record.uncertainty.map((line) => (
            <p className="boundary__text" key={line}>
              {line}
            </p>
          ))}
        </aside>
      ) : null}

      {/* -- Identity and scope ------------------------------------------ */}
      <section className="panel" aria-labelledby="identity-heading">
        <div className="panel__header">
          <h2 id="identity-heading">Signal</h2>
        </div>
        <div className="panel__body">
          <dl className="definition-grid">
            <div>
              <dt>Signal identifier</dt>
              <dd className="mono">{record.id}</dd>
            </div>
            <div>
              <dt>Type</dt>
              <dd>{record.signal_type.replace(/_/g, " ")}</dd>
            </div>
            <div>
              <dt>Period</dt>
              <dd className="mono">
                {record.period_start} to {record.period_end}
              </dd>
            </div>
            <div>
              <dt>Score</dt>
              <dd>{record.score ?? "—"}</dd>
            </div>
            <div>
              <dt>Governed rule</dt>
              <dd className="mono">{record.rule_code}</dd>
            </div>
            <div>
              <dt>Method version</dt>
              <dd className="mono">{record.method_version_id}</dd>
            </div>
            <div>
              <dt>Source cutoff</dt>
              <dd className="mono">{record.source_cutoff}</dd>
            </div>
            <div>
              <dt>Input fingerprint</dt>
              <dd className="mono signal__fingerprint">{record.input_fingerprint}</dd>
            </div>
          </dl>
        </div>
      </section>

      {/* -- Explanation -------------------------------------------------- */}
      <section className="panel" aria-labelledby="explanation-heading">
        <div className="panel__header">
          <h2 id="explanation-heading">Why this was flagged</h2>
          <span className="chip chip--info">Deterministic</span>
        </div>
        <div className="panel__body">
          {explanation.isPending ? (
            <LoadingState label="explanation" />
          ) : explanation.isError ? (
            <NoDataState
              title="No explanation has been generated"
              description="The explanation engine has not run for this signal. MARS shows nothing rather than composing a description of its own."
              awaiting="explanation build"
            />
          ) : (
            <Explanation record={explanation.data} />
          )}
        </div>
      </section>

      {/* -- Evidence ----------------------------------------------------- */}
      <section className="panel" aria-labelledby="evidence-heading">
        <div className="panel__header">
          <h2 id="evidence-heading">Evidence</h2>
        </div>
        <div className="panel__body panel__body--flush">
          <p className="panel__lede signal__lede">
            Supporting and counter-evidence together. A screen showing only what agrees with
            the flag would be an advocacy document; the counter-evidence is usually the more
            useful half when deciding whether to send someone.
          </p>
          {record.evidence && record.evidence.length > 0 ? (
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">
                  Evidence contributing to this signal
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Role</th>
                    <th scope="col">Kind</th>
                    <th scope="col">Summary</th>
                    <th scope="col" className="numeric">
                      Contribution
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {record.evidence.map((item) => (
                    <tr key={`${item.source_table}-${item.source_record_id}-${item.role}`}>
                      <td>
                        <span className={`chip chip--${roleTone(item.role)}`}>
                          {item.role}
                        </span>
                      </td>
                      <td>{item.kind.replace(/_/g, " ")}</td>
                      <th scope="row" className="signal__summary">
                        {item.summary}
                      </th>
                      <td className="numeric">{item.contribution ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="panel__body">
              <NoDataState
                title="No evidence rows are attached"
                description="This signal record carries no evidence. That is a fault in the generation run, not a finding about the area."
              />
            </div>
          )}
        </div>
      </section>

      {/* -- Data quality -------------------------------------------------- */}
      <section className="panel" aria-labelledby="quality-heading">
        <div className="panel__header">
          <h2 id="quality-heading">Data quality</h2>
        </div>
        <div className="panel__body">
          <p className="signal__counts">
            {record.evidence_count} supporting, {record.counter_evidence_count} counter.
          </p>
          <pre className="signal__json">{JSON.stringify(record.data_quality, null, 2)}</pre>
        </div>
      </section>
    </div>
  );
}

function Explanation({
  record,
}: {
  record: {
    why_flagged: string;
    method_steps: Record<string, unknown>[];
    missing_information: string[];
    recommended_actions: Record<string, string>[];
    interpretation_limit: string;
    generator_version: string;
    input_fingerprint: string;
  };
}) {
  return (
    <div className="signal__explanation">
      <p className="signal__why">{record.why_flagged}</p>

      <h3 className="signal__subheading">How MARS analysed this</h3>
      <ol className="signal__steps">
        {record.method_steps.map((step, index) => (
          <li key={index}>{describeStep(step)}</li>
        ))}
      </ol>

      <h3 className="signal__subheading">What is missing</h3>
      <ul className="signal__missing">
        {record.missing_information.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>

      {record.recommended_actions.length > 0 ? (
        <>
          <h3 className="signal__subheading">Recommended next actions</h3>
          <ul className="signal__actions">
            {record.recommended_actions.map((action) => (
              <li key={action.code ?? JSON.stringify(action)}>
                {action.label ?? action.code}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <p className="signal__limit">{record.interpretation_limit}</p>
      <p className="signal__provenance mono">
        generator {record.generator_version} · {record.input_fingerprint}
      </p>
    </div>
  );
}

/**
 * One method step as a sentence.
 *
 * The explanation engine writes these; this only chooses which field to read.
 * A step whose shape is unrecognised is shown as JSON rather than swallowed -
 * silently dropping a step would make the analysis look simpler than it was.
 */
function describeStep(step: Record<string, unknown>): string {
  const description = step.description;
  if (typeof description === "string") return description;
  const label = step.step;
  if (typeof label === "string") return label;
  return JSON.stringify(step);
}

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

function roleTone(role: string): string {
  switch (role) {
    case "supporting":
      return "attention";
    case "counter":
      return "info";
    default:
      return "neutral";
  }
}
