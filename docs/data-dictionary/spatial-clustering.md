# Spatial clustering and privacy — Prompt 20

MARS stores adjacency per boundary version in `mars_core.geography_adjacency`.
Only same-level polygons sharing an edge are neighbours; a point touch does not
connect two areas. Both directions are stored so every lookup has one meaning.

`mars_analytics.spatial_cluster_run` records either a completed governed run or
a `not_configured` refusal naming the missing method/privacy configuration.
There is no built-in clustering threshold, minimum count, minimum number of
neighbours, cluster size, or disclosure threshold.

`mars_analytics.spatial_cluster_result` records each evaluated administrative
area, its source aggregation, outcome, neighbour counts and the exact neighbour
evidence used. The outcomes distinguish a negative finding from no observation,
no neighbours, insufficient neighbours, low case count, incomplete reporting,
and an inapplicable method.

Two methods are implemented but neither is selected automatically:

- `neighbour_concentration`: compares an area's value with the mean of its
  usable neighbours under an approved ratio and minimum-neighbour rule.
- `contiguous_high_cluster`: finds connected components of areas already
  classified as hotspots and applies an approved minimum component size.

Patient-derived clustering also requires an active `spatial_privacy_policy`.
The run copies both its method version and privacy configuration version. A
fresh, unconfigured deployment writes a refusal and no result rows; that is not
evidence that no cluster exists.

Every result is administrative-area aggregate evidence only. No patient,
household, direct identifier, or coordinate is stored or returned.
