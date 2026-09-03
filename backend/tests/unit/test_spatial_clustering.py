"""Prompt 20 schema and vocabulary guarantees requiring no database."""

from mars.analytics.clustering import CLUSTER_METHOD_CODE
from mars.domain.adjacency import GeographyAdjacency
from mars.domain.clustering import SpatialClusterResult, SpatialClusterRun
from mars.domain.enums import ClusterMethod, ClusterOutcome


def _constraints(model: type) -> set[str]:
    return {constraint.name for constraint in model.__table__.constraints if constraint.name}


def _columns(model: type) -> set[str]:
    return {column.name for column in model.__table__.columns}


def test_clustering_method_is_named_but_not_chosen() -> None:
    assert CLUSTER_METHOD_CODE == "spatial_cluster_detection"
    assert {method.value for method in ClusterMethod} == {
        "neighbour_concentration",
        "contiguous_high_cluster",
    }


def test_non_evaluable_cluster_outcomes_do_not_mean_not_clustered() -> None:
    assert ClusterOutcome.NOT_CLUSTERED is not ClusterOutcome.NOT_EVALUATED_NO_NEIGHBOURS
    assert len(ClusterOutcome) == 8


def test_adjacency_is_versioned_symmetric_pair_storage() -> None:
    assert {
        "boundary_version_id",
        "geography_unit_id",
        "neighbour_unit_id",
        "derivation",
    } <= _columns(GeographyAdjacency)
    assert "ck_geography_adjacency_an_area_is_not_its_own_neighbour" in _constraints(
        GeographyAdjacency
    )


def test_completed_cluster_run_requires_method_and_privacy_versions() -> None:
    assert "ck_spatial_cluster_run_completed_run_carries_governance" in _constraints(
        SpatialClusterRun
    )


def test_cluster_result_keeps_neighbour_evidence_and_method() -> None:
    assert {
        "method_version_id",
        "neighbour_evidence",
        "input_fingerprint",
        "reporting_completeness",
    } <= _columns(SpatialClusterResult)
    assert "ck_spatial_cluster_result_not_clustered_means_evaluated" in _constraints(
        SpatialClusterResult
    )
