"""Application-owned database factories use the supplied application settings."""

from sqlalchemy import create_engine

from mars.core.settings import Settings
from mars.db import session as session_module


def test_create_session_factory_uses_explicit_settings(monkeypatch):
    supplied = Settings(database_url="postgresql+psycopg://explicit:pw@db/mars")
    engine = create_engine("sqlite://")
    observed = []

    def build(settings):
        observed.append(settings)
        return engine

    monkeypatch.setattr(session_module, "_build_engine", build)
    owned_engine, factory = session_module.create_session_factory(supplied)

    assert observed == [supplied]
    assert owned_engine is engine
    assert factory.kw["bind"] is engine
    assert factory.kw["expire_on_commit"] is False
    engine.dispose()
