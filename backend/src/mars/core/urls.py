"""URL handling that more than one layer needs.

Small on purpose. ``strip_url_credentials`` lives here rather than in an
adapter because both the adapter and the status service need it, and
``services`` may not import ``integrations`` (ADR 0003). A URL utility is not
knowledge about any particular external system.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def strip_url_credentials(url: str | None) -> str | None:
    """Remove any userinfo from a URL.

    ``https://admin:district@dhis2.example.org`` is a valid URL and a password
    in plain sight. It is stripped before the URL is stored on a run record,
    written to a log, or returned by an endpoint - all three of which are
    places a credential gets read by someone who should not have it.
    """
    if not url:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        parts = parts._replace(netloc=parts.netloc.rsplit("@", 1)[1])
    return urlunsplit(parts).rstrip("/")


__all__ = ["strip_url_credentials"]
