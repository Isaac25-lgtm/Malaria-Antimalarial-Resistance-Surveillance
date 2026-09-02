# Historical boundary versioning

How MARS keeps a boundary recut from rewriting the past, and which query answers
which question.

Read [geography-import.md](geography-import.md) for how boundaries arrive, and
[geography-map.md](geography-map.md) for how they are served.

## The defect this corrects

The importer matched units on `(level, preferred_code)`, kept the UUID — which
was right — and then **overwrote** name, parent, depth, path, boundary version,
active state and geometry on the stable row. Geometry was worse: one row per
unit, so a recut replaced the previous shape in place.

The consequence was quiet and total. After a second import:

- the earlier `BoundaryVersion` row survived, carrying its checksum and its
  audit trail, describing boundaries **nothing could reconstruct**
- an analysis run under that version could not be reproduced
- a district renamed in the recut appeared to have always had its new name
- a subcounty dropped in the recut appeared never to have existed

None of that raised an error. The blueprint requires that a later recut must not
silently rewrite historical analysis, and it was doing exactly that.

## The model

Two ideas were conflated. They are now separate tables.

### `geography_unit` — stable identity

The thing that **points are made at**: facilities, user geography scopes and
outpatient encounters all carry a `geography_unit_id`. It survives every recut:
never renumbered, never re-parented, never deleted.

Its descriptive columns — `raw_name`, `normalised_name`, `parent_id`, `depth`,
`path`, `is_active`, `boundary_version_id` — are **a cache of the currently
published revision**. They are fast to read and structurally incapable of
answering a historical question, and every one carries a database comment
saying so:

> Cache of the currently published revision's name. NOT historical: a later
> import overwrites it. Query `geography_unit_revision` for what any given
> boundary version said.

### `geography_unit_revision` — what one version said

One row per `(geography_unit_id, boundary_version_id)`, holding everything a
recut can change:

| Column | |
| --- | --- |
| `level`, `unit_kind` | The classification under this version |
| `preferred_code` | The code under this version — a recut may reassign one |
| `raw_name`, `normalised_name` | The name under this version |
| `parent_revision_id` | The parent **revision**, not the parent unit |
| `depth`, `path` | The hierarchy position under this version |
| `is_present` | Whether this version contained the unit at all |
| `effective_from`, `effective_to` | The period this version claims |

`parent_revision_id` points at a revision rather than a unit deliberately. A
recut can re-parent, and a link to the stable parent would lose which parent the
unit had *at the time* — which is the fact being preserved.

### `geography_unit_geometry` — one shape per version

Keyed on `(geography_unit_id, boundary_version_id)`. It was keyed on the unit
alone, which is why a recut overwrote the previous shape.

Every query joining geometry must constrain **both halves of the key**. Joining
on the unit alone returns one row per version the unit has ever had, and the map
draws each boundary once per historical version — a bug that looks like a
rendering glitch and is actually a missing predicate.

## Immutability

A trigger on `geography_unit_revision` rejects UPDATE, and rejects DELETE while
the owning `geography_unit` still exists, whenever the revision belongs to a
**published** boundary version.

Application discipline would not be enough here: the importer *is* the code that
was rewriting history, so the database refuses rather than trusting it not to.

Two deliberate exemptions:

- **Unpublished versions are mutable.** An import in progress writes and
  rewrites its own revisions freely; publication is what freezes them.
- **A cascade from deleting the unit is allowed.** Removing a stable unit
  entirely takes its record of what each version said with it. That is not a
  rewrite of history — the thing the history describes is gone. The trigger
  distinguishes the two by checking whether the parent row still exists.

## Which query answers which question

| Question | Read | Why |
| --- | --- | --- |
| What does the map draw now? | The cached columns, filtered to the published version | Current state, one join fewer |
| What did version X contain? | `geography_unit_revision` where `boundary_version_id = X` | The cache holds only today's answer |
| What shape did unit U have under X? | `geography_unit_geometry` on `(U, X)` | One shape per version |
| Which facility is this? | `geography_unit_id` | Stable identity, unaffected by recuts |

`GeographyMapService.historical_hierarchy(principal, boundary_version_id)` is
the supported historical read. It goes through revisions and never touches the
cached columns, because asking those about an earlier version returns **today's
answer wearing yesterday's date** — which is worse than an error, since it looks
right.

Scope is applied through the *stable* unit even for historical queries:
authorisation is granted over identity, so a district officer's grant follows
their district across a recut. That is the point of separating the two.

## Publication is atomic

A partial unique index allows exactly one published boundary version. An import
that fails validation is retained as `validation_failed` with its full report,
and **the previously published version is untouched** — so a broken recut leaves
the last good boundaries serving rather than leaving the country with no map.

## What a recut can and cannot express

In the supplied source scheme the FScode **is** the hierarchy: a subcounty's
county is `FScode[0:4]`, its district `FScode[0:3]`. A unit therefore cannot be
re-parented while keeping its code. A real recut expresses a move as a **new
code appearing and the old one disappearing** — two facts, both preserved as
revisions, rather than one unit quietly changing parents.

The model supports re-parenting within a version (`parent_revision_id` is per
version) for sources that can express it. The supplied source cannot, and the
tests say so rather than pretending otherwise.

## Verification

`tests/integration/test_geography_versioning.py`, against live PostgreSQL. Every
test has the same shape: import version A, record what it says, import a changed
version B, assert A is still exactly what it was.

| Guarantee | How it is proved |
| --- | --- |
| Version A is unchanged by version B | Full snapshot compared before and after |
| A renamed unit keeps its old name under the old version | ALPHA NORTH → ALPHA CENTRAL |
| A dropped unit keeps its path under the old version | BETA SOUTH removed in B |
| The new version records the change | ALPHA CENTRAL present under B only |
| Both versions hold their own revisions | Row counts per version |
| A unit's UUID survives a recut | Compared across versions |
| A foreign key still resolves after a recut | Unit re-read by id |
| Geometry is kept per version | Two rows, one per version |
| The two shapes actually differ | `ST_Area` compared |
| Published revisions cannot be updated | Trigger, as the table owner |
| Published revisions cannot be deleted | Trigger, as the table owner |
| A failed import does not displace the published version | Control-total failure |
| The map draws the published version | ALPHA CENTRAL, not ALPHA NORTH |
| Each unit is drawn once after a recut | Feature ids are unique |
| A historical query returns the pinned version | ALPHA NORTH under A |
| A since-dropped unit is in the historical answer | BETA SOUTH under A |
