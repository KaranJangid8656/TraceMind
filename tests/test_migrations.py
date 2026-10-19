import logging
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

import app.models  # noqa: F401
from app.database import Base
from app.migrations import (
    MIGRATION_LOCK_ID,
    MigrationExecutionError,
    MigrationStateError,
    upgrade_database,
    verify_database_revision,
)


def test_initial_migration_builds_current_metadata(tmp_path, monkeypatch):
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(Config("alembic.ini"), "head")

    assert not logging.getLogger("app.rca").disabled

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261019_01"
            )
    finally:
        engine.dispose()


def test_database_at_expected_revision_is_accepted(tmp_path, monkeypatch):
    database_path = tmp_path / "current.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")

    engine = create_engine(database_url)
    try:
        verify_database_revision(engine)
    finally:
        engine.dispose()


def test_unbaselined_database_is_rejected(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'unbaselined.db').as_posix()}")
    try:
        with pytest.raises(MigrationStateError, match="Database is not baselined"):
            verify_database_revision(engine)
    finally:
        engine.dispose()


def test_database_behind_expected_head_is_rejected(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'behind.db').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('older_revision')"))
        with pytest.raises(MigrationStateError, match="does not match"):
            verify_database_revision(engine)
    finally:
        engine.dispose()


def test_revision_connectivity_failure_is_sanitized():
    class UnavailableEngine:
        def connect(self):
            raise SQLAlchemyError("PRIVATE database credentials")

    with pytest.raises(MigrationStateError) as failure:
        verify_database_revision(UnavailableEngine())

    assert str(failure.value) == "Database migration state could not be verified"
    assert "PRIVATE" not in str(failure.value)


class FakeMigrationConnection:
    def __init__(self):
        self.events = []
        self.transaction_open = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters):
        self.events.append((str(statement), parameters["lock_id"]))
        self.transaction_open = True

    def commit(self):
        self.events.append(("commit", None))
        self.transaction_open = False

    def rollback(self):
        self.events.append(("rollback", None))
        self.transaction_open = False

    def in_transaction(self):
        return self.transaction_open


class FakeMigrationEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self):
        self.connection = FakeMigrationConnection()

    def connect(self):
        return self.connection


def test_migration_execution_uses_postgresql_advisory_lock(monkeypatch):
    migration_engine = FakeMigrationEngine()

    def upgrade(config, revision):
        assert revision == "head"
        assert config.attributes["connection"] is migration_engine.connection
        migration_engine.connection.events.append(("upgrade", None))

    monkeypatch.setattr("app.migrations.command.upgrade", upgrade)

    upgrade_database(migration_engine)

    assert migration_engine.connection.events == [
        ("SELECT pg_advisory_lock(:lock_id)", MIGRATION_LOCK_ID),
        ("commit", None),
        ("upgrade", None),
        ("SELECT pg_advisory_unlock(:lock_id)", MIGRATION_LOCK_ID),
        ("commit", None),
    ]


def test_migration_execution_releases_lock_after_failure(monkeypatch):
    migration_engine = FakeMigrationEngine()

    def fail(config, revision):
        migration_engine.connection.events.append(("upgrade", None))
        raise RuntimeError("PRIVATE migration failure")

    monkeypatch.setattr("app.migrations.command.upgrade", fail)

    with pytest.raises(MigrationExecutionError) as failure:
        upgrade_database(migration_engine)

    assert str(failure.value) == "Database migration failed"
    assert "PRIVATE" not in str(failure.value)
    assert migration_engine.connection.events[-2:] == [
        ("SELECT pg_advisory_unlock(:lock_id)", MIGRATION_LOCK_ID),
        ("commit", None),
    ]
