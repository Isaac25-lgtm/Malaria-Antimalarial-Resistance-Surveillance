"""How a role-switched database URL must be built.

The integration suite reaches the vault as three different PostgreSQL roles.
Building those URLs looks trivial and is not: the previous helper managed two
independent mistakes at once, and loopback trust authentication hid both until
the suite met a cluster that actually required a password.

These are pure ``URL`` semantics, so they run without a database and fail fast,
rather than surfacing as eight authentication errors in CI.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

BASE = "postgresql+psycopg://mars:ci@localhost:5432/mars_test"


def test_setting_password_to_none_does_not_clear_it() -> None:
    """``set(password=None)`` means "leave it alone", not "remove it"."""
    switched = make_url(BASE).set(username="other_role", password=None)

    assert switched.username == "other_role"
    assert switched.password == "ci"


def test_str_of_a_url_masks_the_password() -> None:
    """Stringifying a URL is lossy, and the loss is silent.

    ``create_engine`` accepts the masked string happily and then offers ``***``
    as the password, which fails only at connection time.
    """
    rendered = str(make_url(BASE).set(username="other_role"))

    assert "***" in rendered
    assert make_url(rendered).password == "***"


def test_a_role_switched_url_carries_its_own_password() -> None:
    """The shape the fixture must use: explicit password, URL object kept."""
    switched = make_url(BASE).set(username="other_role", password="role-secret")

    assert switched.username == "other_role"
    assert switched.password == "role-secret"
    assert switched.render_as_string(hide_password=False).endswith(
        "other_role:role-secret@localhost:5432/mars_test"
    )
    # A URL object survives the trip into an engine unmasked.
    engine = create_engine(switched)
    try:
        assert engine.url.password == "role-secret"
    finally:
        engine.dispose()
