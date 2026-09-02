# Geographic API and the national map

How boundary geometry reaches a browser, what it is allowed to carry, and why
the delivery strategy is what it is.

Read [geography-import.md](geography-import.md) first: it records how the
geometry got into PostGIS in the first place.

## The delivery decision

Prompt 6 offered three options — pre-simplified GeoJSON, zoom-aware
simplification, or vector tiles. The choice was made from measurement, not
preference.

Measured on the real supplied geography, PostgreSQL 16.4 + PostGIS 3.6.2:

| Layer | Features | Full-resolution GeoJSON | Served (simplified) | gzipped | Ratio | Time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Country outline | 1 | 595 kB | 10.2 kB | 4.3 kB | 1.7% | 27 ms |
| Regions | 4 | 1,478 kB | 62.4 kB | 18.9 kB | 4.2% | 14 ms |
| **Districts (the national map)** | **146** | **10,035 kB** | **376 kB** | **132 kB** | **3.7%** | **32 ms** |
| Counties in one district | 1 | 68 kB | 4.9 kB | 2.0 kB | 7.2% | 20 ms |
| Subcounties in one district | 16 | 164 kB | 23.5 kB | 6.8 kB | 14.4% | 19 ms |
| One district's geometry | 1 | — | 3.4 kB | — | — | 5 ms |
| One district's bounds | 1 | — | 4 numbers | — | — | 4 ms |

**Pre-simplified GeoJSON was chosen.** The largest view anyone needs is the
national district layer, and it is 132 kB gzipped in 32 ms. Vector tiles would
add a tile server, a tile cache, an invalidation story tied to boundary
versions, and a build step — to improve a payload that is already smaller than
a photograph. Prompt 6 explicitly warns against introducing tile infrastructure
for its own sake, and the measurement says it would be exactly that.

Zoom-aware simplification was rejected for the same reason at a smaller scale:
the per-level tolerances the importer already applies produce the table above,
and a second tolerance axis would mean cache keys multiplying by zoom for no
measured gain.

If a future layer changes this — a parish level, or a national subcounty
requirement — the measurement should be repeated rather than the conclusion
inherited.

## What the browser is served

**Simplified geometry only.** `geom` is the analytical copy and has no route.
There is no query parameter that returns it, so no client can request
full-resolution geometry by accident or on purpose. The subcounty layer carries
1.67 million vertices; an endpoint that could return it would be a
denial-of-service vector wearing a map.

**A closed property allow-list.** A feature carries exactly these, and the set
is pinned by a test so widening it is a decision someone made rather than a
field that appeared:

```
unit_id   level   code   name   parent_id   path   area_sq_km   is_active
```

All administrative reference data. Nothing derived from health data, no
coordinates beyond the boundary itself, and no geometry-table internals —
`validity_issues`, `repair_method`, `vertex_count` and the rest stay
server-side, where they are diagnostics rather than public map data.

**A feature ceiling of 400.** Above the 146-district national layer, below the
2,190-subcounty national one. A request that matches more is **refused with
413**, never truncated: a map that quietly stops at 400 features has a hole in
it and nothing on screen would say so. The error names the matched count and
suggests narrowing by parent, and that remedy is asserted to actually work.

## Endpoints

All under the existing `/api/v1/geography` namespace — a map is a way of
reading the hierarchy, not a second API.

| Route | Purpose |
| --- | --- |
| `GET /geography/map/metadata` | What the caller can draw, from which boundary version, at what tolerance |
| `GET /geography/map/features` | One layer as GeoJSON: `level`, plus `parent_id` or `within_id` |
| `GET /geography/national` | The caller's root geography and the level below it |
| `GET /geography/units/{id}/geometry` | One unit as a GeoJSON Feature |
| `GET /geography/units/{id}/bounds` | One unit's extent, without its geometry |
| `GET /geography/units/{id}/breadcrumbs` | The visible ancestor chain |
| `GET /geography/units/{id}/children` | Direct children |
| `GET /geography/districts/{code}` | District lookup by code |
| `GET /geography/subcounties/{code}` | Subcounty lookup by code |
| `GET /geography/boundary-versions` | Every registered boundary version |

### `parent_id` and `within_id`

Two restrictions, because the hierarchy has five levels and a map has fewer
useful steps.

- `parent_id` — direct children only.
- `within_id` — every descendant at the requested level, by materialised path.

The district-to-subcounty drill needs the second. Subcounties hang off
**counties**, so filtering subcounties by a district as *parent* returns
nothing — which on screen reads as "this district has no subcounties" rather
than as a badly formed question. The path pattern is anchored with a separator
so `UG/300` cannot match `UG/3001`.

The largest district holds 44 subcounties; every one of the 146 districts is
asserted to drill within the ceiling, so there is no district that cannot be
opened.

## Authorisation

Every route requires `geography:view` and applies the caller's geography scope
**inside the query**, reusing `GeographyService`'s predicate rather than
restating it — two implementations of "what may this user see" is one more than
a system should have.

### Hidden and absent are the same answer

A unit outside the caller's scope raises the same not-found, with the same
message, as a unit that does not exist. This is asserted on the messages
themselves, not just the status codes. A parent or subtree id is resolved
through the scoped service *before* it is used as a filter, because filtering on
an unresolved id would return an empty collection — and an empty collection
would tell the caller the id was real.

### No permission is a 403; no scope is not

These fail differently, deliberately:

- **Missing `geography:view`** → 403 naming the permission, never the resource.
- **Holding the permission with no geography assigned** → 200 with an empty
  collection, or 404 on a unit route. Never 403.

A 403 for a scope miss would distinguish a misconfigured account from one simply
looking outside its area, and on a per-unit route would confirm the unit exists.

### The viewport follows assigned scope, not visibility

The scope predicate deliberately admits **ancestors**, so a district officer can
render the breadcrumb "Uganda / Northern / Gulu". That means Uganda is visible
to them — and an initial viewport derived from "the shallowest visible unit"
would zoom every district officer out to the whole country on each page load.
The map opens on the caller's *assigned* geography instead. A national account's
scope root is the country, so both readings agree there.

## Caching

Responses carry a strong `ETag` derived from the boundary version, the level,
the filters and the limit — the inputs that determine the bytes. It is therefore
stable across restarts and replicas, where a timestamp or a process counter
would not be. Publishing a new boundary version changes the version code and so
invalidates every layer at once, which is correct: the hierarchy moved.

`Cache-Control: private` — the payload depends on the caller's scope, so a
shared cache must never serve one user's layer to another.

The caller is deliberately **not** mixed into the ETag. Responses are private and
never shared between users, and a principal-derived validator would be a weak
identifier of who fetched what.

## The national map

`/national`, in the existing React application, using MapLibre GL JS.

### No external basemap

There is no tile provider, no third-party attribution, and no request leaving
the browser for anything but the MARS API. A basemap would send the viewport —
and by inference which district an officer is looking at — to whoever serves the
tiles. Uganda is drawn from MARS's own boundaries on a neutral ground, which is
all a boundary map needs. This also means the map works on an isolated network.

### No fabricated values

One neutral fill. No choropleth, no demonstration metric, no colour scale.
MARS has no indicator to colour by yet, and a plausible-looking gradient over
invented numbers is the single most misleading thing the page could contain.
The page says so in as many words.

### The map is not the only way in

The geography list sits beside the map at all times — not as a fallback that
appears when something fails. A `<canvas>` cannot be made properly keyboard
navigable, so if selecting a district required clicking a polygon, the page
would be unusable by keyboard and invisible to a screen reader.

Both representations are driven by the same feature collection, so they cannot
disagree. On a narrow screen the list comes **first**, because it is the
representation that works everywhere.

Selection is indicated three ways — a background, a left bar, and
`aria-current` — so it never depends on colour alone, and survives greyscale.

### Loading cost

MapLibre is roughly 800 kB, and most of MARS is not a map. The canvas component
is loaded lazily, which keeps it out of the bundle every other page pays for:

| Bundle | Before | After |
| --- | ---: | ---: |
| Main entry | 998 kB (264 kB gzipped) | **43 kB (11 kB gzipped)** |
| Map chunk | — | 957 kB (252 kB gzipped), on demand |

## Error responses

The map routes publish their failure modes in the contract, so a generated
client knows what it must handle:

| Status | Meaning |
| --- | --- |
| 403 | The caller does not hold `geography:view` |
| 404 | No such unit, **or** outside the caller's scope - the same response by design |
| 413 | The layer exceeds the feature ceiling; narrow with `parent_id` or `within_id` |

The rest of the MARS API documents only its success shapes. That is a
pre-existing gap and retrofitting every operation was out of scope here, but
these routes have designed failure modes a client branches on, and a contract
that omitted them would leave every consumer to discover them at runtime.

## Verification

| Tier | Count | Against |
| --- | ---: | --- |
| API contract | 34 | The application, with service fakes - no database |
| Map integration | 61 | Live PostgreSQL 16.4 + PostGIS 3.6.2 |
| ...of which real supplied geography | 11 | The 2,653 imported Ugandan units |
| ...of which over HTTP | 16 | The full request path, including cache headers |
| Frontend | 28 | The view, with MapLibre stubbed |

The real-source tier asserts the measured payload figures above, that a national
subcounty request is refused, and that **all 146 districts** drill to their
subcounties within the ceiling - not a chosen one.

**Not covered:** MapLibre's own rendering. jsdom has no WebGL, so the canvas
component is stubbed in the frontend tests. Everything up to and including the
props handed to MapLibre is asserted; the pixels it then draws are the
library's responsibility and are not exercised here.
