"""Versioned encounter and aggregate source-ingestion pipelines.

Each source passes the same gate: receive, checksum, identify schema version,
parse, standardise, validate, quarantine, load raw, transform to canonical,
reconcile.
"""
