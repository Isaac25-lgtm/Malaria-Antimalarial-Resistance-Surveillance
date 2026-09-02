# Identity vault and deterministic linkage

How MARS stores direct patient identifiers, how it links a person to themselves
across visits, and what it takes to turn a pseudonymous record back into a name.

Read [ADR 0006](../adr/0006-identity-separation-and-authorisation.md) first: it
records why identity is separated at all.

## Threat and boundary review

### What counts as a direct identifier

From HMIS OPD 002, the fields that identify a person on their own or in
combination with very little else:

| Source | Field | Why |
| --- | --- | --- |
| Column 2 | National ID (NIN), refugee number, passport number | A national register key. Directly identifying, and re-usable against other systems. |
| Column 3 | Surname, given name | Directly identifying. |
| Column 3 | Phone contact | Directly identifying, and a linkage key to telecoms records. |
| Column 8 | Next of kin name, phone, relationship | Directly identifying **a third party who did not attend**. |
| Column 7 | Village + parish | Not identifying alone. With age and sex at village level, frequently is. |

The first four are **direct identity**. The last is a quasi-identifier and stays
in `mars_core`, because geography is what surveillance is for — but it carries
the highest sensitivity there and is excluded from aggregate exports.

### What belongs in `mars_identity`

Every one of these is stored as **AES-256-GCM ciphertext**, encrypted in the
application before it reaches PostgreSQL:

- The identifier value and its type
- The person's name
- Their phone number
- Their date of birth, when a source supplies one

Stored in the clear, because they must be:

- The **linkage token**, which has to be indexed for equality. It is a MAC, not
  an encryption: it cannot be reversed to the identifier even with the key, and
  it is derived under a *different* key from the one protecting the ciphertext
  beside it.
- The key versions, so a row can be decrypted and re-keyed.

And nothing clinical. Not a diagnosis, not a test result, not a prescription.

**The normalised identifier is not stored at all.** It is a pure function of the
raw value and the normalisation rules, so a second copy - even encrypted - would
widen exposure for nothing. Diagnosing a mis-normalisation means re-normalising
the decrypted raw value under the old rules and the new.

The vault knows **who**. `mars_core` knows **what happened**. Neither knows the
other's half without crossing the boundary deliberately.

### What must never enter `mars_identity`

Clinical data. A vault row that carried a malaria result would mean a single
compromised query returned both a name and a diagnosis, which is precisely the
outcome the separation exists to prevent.

### What belongs in `mars_core`

`patient_reference`: an id, three dates, a count. Nothing that describes a
person. A service reading it learns that two encounters share a person and
nothing else about who that person is.

### What is stored nowhere

**Next of kin** (column 8). Not in `mars_core`, and not in the vault either.
Surveillance has no purpose for a third party's contact details, and the vault
exists to link patients to themselves, not to hold a contact book. An extract
containing next-of-kin fields has them dropped at the ingestion boundary.

### Which database role reaches identity

Two roles, and the separation is enforced by PostgreSQL rather than by
application code:

| Role | `mars_core` | `mars_identity` |
| --- | --- | --- |
| `mars_app` — the application, analytics, the API | full | **no privileges at all** |
| `mars_identity_service` — the identity service only | none | full |

`mars_app` is not merely un-granted on `mars_identity`; `USAGE` on the schema is
revoked, so a query naming a vault table fails at parse time. An SQL injection
in an ordinary endpoint cannot reach identity, because the connection it runs on
has no path to it.

The separation is a **runtime** fact, not a convention: the identity service
builds its own engine from `MARS_IDENTITY_DATABASE_URL`, with its own
credentials and its own pool. It is not the application connection with a
promise attached, and it is not `SET ROLE` on a shared connection - which
anything able to execute SQL could simply reset.

**Roles are created by provisioning, not by a migration.** Granting privileges
needs no special rights; creating a role needs `CREATEROLE`, which an ordinary
migration runner should not hold. `scripts/provision_identity_roles.sql` is the
privileged step that creates the two group roles; migration 0006 detects them
and applies the grants, and runs cleanly when they are absent. Login and
credentials are attached separately, from the deployment's secret store - the
group roles are `NOLOGIN` precisely so a credential must be attached
deliberately rather than inherited from a committed script.

### Which permission allows re-identification

`patient:reidentify`, whose minimum sensitivity is `DIRECT_IDENTITY`.

**No role is granted it.** Not the national programme, not an administrator.
It is assigned to an individual account, for a stated purpose, and is expected
to be removed afterwards. An administrator can grant it — and that grant is
itself audited.

Both axes are required. Holding the permission with a lower sensitivity ceiling
is refused, because a permission whose minimum sensitivity exceeds the caller's
ceiling is a misconfiguration, not an upgrade.

### How access is audited

Every re-identification attempt writes an audit event, whether it succeeded, was
denied, or found nothing:

- the actor, the session, the request id
- the stated reason, which is **required** and non-empty
- the `patient_reference_id` that was asked about
- the outcome

The audit context contains **no identifier value and no name**. An audit trail
that recorded what was revealed would become a second, unguarded copy of the
vault. A denial is written on an independent short-lived transaction, so it
survives the rollback of the request it denied.

**The log is append-only in the database.** A trigger rejects UPDATE and DELETE
on `reidentification_event`, and the identity role holds only SELECT and INSERT.
Both matter: the privilege can be re-granted by a later migration, while the
trigger binds every writer including the table owner - and the component with
the most reason to edit this log is the one that writes to it.

A **second, coarser record** goes to the general audit trail as
`AuditAction.REIDENTIFICATION_PERFORMED`, so a reviewer looking at one actor's
activity sees the disclosure beside everything else they did. It carries the
pseudonymous reference and deliberately **not** the stated reason: `mars_audit`
is readable by roles that must not learn why a particular patient was looked
up.

### How keys are supplied and rotated

The linkage key is an HMAC secret supplied through the environment
(`MARS_IDENTITY_LINKAGE_KEY`) and held in memory as a `SecretStr`. It is:

- never written to the database
- never written to a log, an error, or an audit record
- never returned by any endpoint
- absent by default, so a deployment that forgets it cannot silently produce
  tokens under a default key

Each token records the **key version** that produced it. Rotation adds a new
version; a token derived under `v1` and one under `v2` never collide, because
the version selects a different key entirely.

**Lookup searches every configured version**, active first. A patient first seen
under `v1` is still found after rotation to `v2`, and the matched row is
re-derived under the active key in place - so a rotation completes as ingestion
runs, with no migration job and without ever creating a second identity for the
same person. Re-keying is idempotent and races are decided by the unique
constraint on `(linkage_token, linkage_key_version)`.

If the vault holds a token under a version that is **not** configured, lookup
raises rather than reporting "not found". Silently missing a retired key would
make an existing patient look new, split their clinical history in two, and
leave nothing in any log to explain it.

### What must never appear in logs or errors

Identifier values, names, phone numbers, and raw linkage material. The identity
service raises errors that name the *reference*, never the person. Structured
log events carry `patient_reference_id` and never an identifier.

## Encryption at rest

Schema separation keeps identity out of the application's reach. It does nothing
for a backup, a replica, a stolen disk or a superuser session, and a column
called `surname` holding a name in a nightly dump is the same breach whichever
schema it sits in.

**AES-256-GCM**, an AEAD: one operation gives confidentiality *and*
authentication, so a modified ciphertext fails to decrypt rather than yielding
altered plaintext. There is no separate MAC to forget to check.

The stored envelope is self-describing, so a key can be rotated without
rewriting history:

```
version_length : 1 byte
version        : ASCII, up to 16 bytes
nonce          : 12 bytes, fresh random per operation
ciphertext+tag : AES-256-GCM output
```

**Associated data binds each ciphertext to where it lives.** The AAD names the
table, the column and a value identifying the row - the patient reference for a
vault record, the linkage token for an identifier. A surname ciphertext moved
into the phone column, or copied onto another patient's row, fails to decrypt.
Encryption alone would leave that shuffle undetectable.

**A nonce is never reused.** 96 random bits per operation, so encrypting the same
name twice yields different ciphertext and the database reveals nothing by
equality. Equality matching is the linkage token's job.

**Two key families, held separately.** The encryption key lets you read stored
identifiers; the linkage key lets you *test* a guessed one. Neither substitutes
for the other, and compromising one does not yield the other.

Both come from the environment, both are versioned, and neither has a default.
A deployment missing either reports the identity component **unready** rather
than writing identifiers in plaintext.

## Deterministic linkage

### The token

```
token = HMAC-SHA256(key[version], "mars.identity.v1" | identifier_type | normalised_value)
```

Three properties this buys:

**Deterministic.** The same normalised identifier always produces the same
token, so the same person's visits group without the value being stored in
`mars_core`.

**Domain-separated.** The identifier type is inside the HMAC input, so a
national ID `CM12345` and a passport `CM12345` produce entirely different
tokens. Column 2 of OPD 002 carries three different identifier systems in one
cell with no type marker, so without domain separation a passport holder and a
citizen could be merged into one person.

**Not reversible.** The token is a MAC, not an encryption. Possessing it does
not yield the identifier, even with the key — the key only lets you *test* a
candidate. Re-identification therefore goes through the vault, which is audited,
rather than through a computation an analyst could perform on their own.

### Normalisation

Applied before the HMAC, so trivial formatting differences do not split one
person into two:

| Type | Normalisation |
| --- | --- |
| National ID / refugee / passport | Upper-cased; every character that is not a letter or digit removed |
| Phone | Validated Ugandan number. Exactly one prefix is removed - the `256` country code **or** the `0` trunk digit, never both and never repeatedly - and the remaining national number must be 9 digits beginning 2, 3, 4 or 7. Canonical form carries the country code. |

Normalisation is **lossy on purpose**, and the raw value is kept (encrypted) so
a mis-normalisation can be diagnosed without re-collecting.

Anything that does not validate is **left unlinked** rather than coerced. That
matters more than it looks: an earlier implementation stripped every leading
zero and accepted whatever remained, so a five-digit fragment became a valid
linkage key and two unrelated patients whose records happened to hold the same
fragment were merged into one clinical history.

### What linkage does not claim

A shared token means two records carried the same identifier. It does not
prove they are the same person: identifiers are mistyped, shared, and reused.
`linkage_confidence` records how the link was made, and a link is a
**deterministic match on a stated identifier**, never an inference.

MARS performs no probabilistic linkage. Fuzzy matching on names and dates of
birth produces false merges, and a false merge in a surveillance system attaches
one person's clinical history to another.

## Re-identification

`IdentityService.reidentify()` is the only path, and it requires, in order:

1. `patient:reidentify`
2. `DIRECT_IDENTITY` sensitivity
3. A non-empty stated reason
4. A `patient_reference_id` that resolves

**Steps 1, 2 and 4 raise the same error.** `IdentityUnavailableError`, status
404, one fixed message, in all three cases. A refused caller cannot tell "you
may not" from "there is nobody", and so cannot walk a list of references to
learn which are real without ever being granted a disclosure.

404 rather than 403 is deliberate: a 403 would itself be an answer, confirming
the reference is real and that only authorisation stands in the way. The cost is
real and worth stating - a caller who genuinely should have been granted access
gets no hint that a permission is what they are missing. That is why the audit
trail keeps the outcomes distinct: the person reviewing access sees exactly why
each attempt failed, even though the caller cannot.

**Step 3 stays a validation error.** A missing reason describes the caller's own
request - a field they control - and says nothing about whether any patient
exists, so returning a specific error there leaks nothing and tells them how to
fix the call.

**No identity is queried until all the gates pass.** The order matters as much as
the outcome: querying first would make the *timing* of a refusal depend on
whether the reference existed, which is the same disclosure by a slower route.
Timing is not otherwise equalised - the paths are short and structurally similar,
but MARS does not claim constant-time behaviour here.

### Non-enumerable by construction

- Lookup is by `patient_reference_id` (a UUID), never by an index or a sequence
- There is no list endpoint, no search, and no pagination over identities
- One reference per request; no bulk form exists
- A caller who is denied and a caller who asked about an unknown reference get
  the same status and the same message

### No bulk path

No ordinary endpoint returns identity. The encounter API returns
`patient_reference_id` and nothing else, and a test asserts that no response
model in the API contract contains a name, a phone number or an identifier
field.

## Verification

The guarantees above are asserted by tests rather than described:

| Guarantee | How it is proved |
| --- | --- |
| Same identifier → same token | Golden vectors, recomputed by hand |
| Different types → different tokens | Same value under two types |
| Different key versions → different tokens | Two keys, one value |
| Encryption round-trips | Encrypt then decrypt, every field |
| Same plaintext → different ciphertext | 200 encryptions, all distinct |
| Tampering fails closed | Bit flip at every region of the envelope |
| Wrong key fails closed | Decrypt under a second key |
| Ciphertext is bound to its place | Decrypt under another column, row and table |
| Retired key still decrypts | Rotate, then read a `v1` row |
| No plaintext in any column | Dump both vault tables as text and search |
| v1 patient found after rotation | Link under `v1`, rotate, link again |
| Rotation creates no duplicate | Assert one `identity_record` after re-link |
| Missing retired key is an error | Rotate without configuring `v1` |
| Concurrent linkage → one identity | Two sessions, same identifier |
| No raw identifier in `mars_core` | Column scan across every core table |
| Logs redact | Capture structured log output, assert absence |
| Errors redact | Assert the message names neither value nor reference |
| Audit omits values | Scan every column of the audit row |
| General audit event is written | Assert the action and object recorded |
| Denied and unknown are identical | Compare type, message, status and code |
| No query before authorisation | A session that raises if touched |
| Denials are durable | Audited on an independent transaction |
| Audit is append-only | UPDATE and DELETE as the restricted role **and** as the owner |
| App role cannot reach identity | Query as `mars_app`, expect a parse failure |
| Identity role cannot read clinical data | Query `mars_core` as the identity role |
| Runtime connects as the right role | `pg_has_role(current_user, …)` on each engine |
| Downgrade does not broaden access | Default privileges after downgrade |
