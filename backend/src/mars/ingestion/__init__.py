"""Source ingestion pipelines. Empty until Prompt 9.

Each source passes the same gate: receive, checksum, identify schema version,
parse, standardise, validate, quarantine, load raw, transform to canonical,
reconcile.
"""
