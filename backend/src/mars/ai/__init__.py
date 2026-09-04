"""Ask MARS: an optional assistant that sits inside MARS and outside its core.

Blueprint 055-056. Every screen, alert, explanation object, investigation
workflow and report works with this package removed, and a deployment with no
approved provider says so rather than pretending.

The boundary this package must never cross: it may **read** what the caller is
already authorised to read, and **describe** it. It cannot create a signal,
change a metric, transition an investigation, diagnose anything, or write an
analytical record. There is no code path here that writes to ``mars_analytics``
and none that reads ``mars.identity`` - a module-boundary test enforces both.
"""
