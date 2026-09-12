import type { Schemas } from "../../api/client";
import { rows, strings, text } from "./types";

interface Props {
  page: Schemas["DuplicatePageView"] | undefined;
  onPage: (cursor: string | null) => void;
}

export function DuplicateReviewTable({ page, onPage }: Props) {
  const items = page?.items ?? [];
  return (
    <section className="panel" aria-labelledby="duplicate-heading">
      <div className="panel__header">
        <h3 id="duplicate-heading">Possible duplicate positive encounters</h3>
        <span className="chip">{page?.total ?? 0} groups</span>
      </div>
      <div className="panel__body">
        {items.length === 0 ? <p>No possible duplicate group was identified in this run.</p> : (
          <div className="recurrence-duplicates">{items.map((item, index) => {
            const members = rows(item.members);
            return <details key={text(item.group_id, String(index))}>
              <summary>
                <span className="mono">{text(item.patient_alias, "Unlinked")}</span>
                <span>{text(item.day)}</span>
                <span>{text(item.kind)} / {text(item.confidence)}</span>
                <span>{members.length} records</span>
              </summary>
              <p>{strings(item.reasons).join("; ") || "Duplicate rule match"}</p>
              <div className="table-scroll"><table className="table">
                <thead><tr><th>Source reference</th><th>Facility</th><th>Time</th><th>Tests</th><th>Role</th></tr></thead>
                <tbody>{members.map((member, memberIndex) => <tr key={text(member.reference, String(memberIndex))}>
                  <th className="mono">{text(member.reference)}</th>
                  <td>{text(member.facility_name)}</td>
                  <td>{text(member.occurred_at, text(member.encounter_date))}</td>
                  <td>{rows(member.tests).map((test) => `${text(test.method)}: ${text(test.result)} [${text(test.source_event)}]`).join("; ") || "Not recorded"}</td>
                  <td>{member.is_representative ? "Retained representative" : "Possible duplicate"}</td>
                </tr>)}</tbody>
              </table></div>
            </details>;
          })}</div>
        )}
        <div className="recurrence-pagination">
          <button className="button" disabled={!page?.previous_cursor}
            onClick={() => onPage(page?.previous_cursor ?? null)}>Previous</button>
          <span>{page?.total ?? 0} groups</span>
          <button className="button" disabled={!page?.next_cursor}
            onClick={() => onPage(page?.next_cursor ?? null)}>Next</button>
        </div>
      </div>
    </section>
  );
}
