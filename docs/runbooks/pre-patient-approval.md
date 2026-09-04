# Pre-patient approval checklist

Metadata-only DHIS2 discovery is complete when its sanitized JSON and Markdown
reports exist. **Stop here.** Do not proceed without a named written approval.

Do not:

- retrieve a tracked entity;
- retrieve an enrollment;
- retrieve an event;
- retrieve a relationship;
- execute patient-level analytics;
- begin patient synchronization.

Patient synchronization, if later approved, requires explicit approval of:

- Pader organisation-unit UID;
- programme UID;
- program-stage UIDs;
- facility scope;
- date bounds;
- maximum records;
- minimum variables;
- stable patient-reference semantics;
- pseudonymization;
- key custody;
- cross-facility linkage;
- update/deletion behaviour;
- retention;
- suppression;
- accountable programme/data owner;
- reconciliation target;
- dedicated read-only credential.

The first approved pull must use:

- `mars_live`, never `mars_local`;
- no copy from synthetic `mars_local`;
- demo mode off;
- server-side DHIS2 secrets only;
- Pader source scope unless a national read-only service account is issued;
- immutable source provenance;
- a bounded initial window;
- separate high-water marks for tracked entities, enrollments, events and
  relationships;
- `updatedAfter`;
- a fixed UTC upper boundary;
- an overlap window;
- deterministic pagination;
- idempotent upserts;
- update handling;
- deletion/tombstone handling;
- quarantine;
- reconciliation against aggregate HMIS;
- no probabilistic person matching.

Do not display real-data mode until a synchronisation of that kind has
completed.
