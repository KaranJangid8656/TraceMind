from __future__ import annotations

import argparse
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine

logger = logging.getLogger(__name__)

ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[1] / "alembic.ini"
MIGRATION_LOCK_ID = 860_412_026_101_901


class MigrationStateError(RuntimeError):
    pass


class MigrationExecutionError(RuntimeError):
    pass


def alembic_config() -> Config:
    return Config(str(ALEMBIC_CONFIG_PATH))


def expected_heads(config: Config | None = None) -> set[str]:
    heads = set(ScriptDirectory.from_config(config or alembic_config()).get_heads())
    if not heads:
        raise MigrationStateError("Application migration history has no head revision")
    return heads


def verify_database_revision(bind: Engine) -> None:
    expected = expected_heads()
    try:
        with bind.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
    except SQLAlchemyError:
        raise MigrationStateError("Database migration state could not be verified") from None

    if not current:
        raise MigrationStateError(
            "Database is not baselined; apply migrations to a fresh database or follow the "
            "documented existing-database baseline procedure"
        )
    if current != expected:
        raise MigrationStateError(
            "Database migration revision does not match the application migration head"
        )


def upgrade_database(bind: Engine) -> None:
    if bind.dialect.name != "postgresql":
        raise MigrationExecutionError("Migration execution requires PostgreSQL")

    config = alembic_config()
    try:
        with bind.connect() as connection:
            locked = False
            try:
                connection.execute(
                    text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID}
                )
                connection.commit()
                locked = True
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            finally:
                if connection.in_transaction():
                    connection.rollback()
                if locked:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": MIGRATION_LOCK_ID},
                    )
                    connection.commit()
    except Exception:  # noqa: BLE001 - migration scripts may raise arbitrary exceptions.
        raise MigrationExecutionError("Database migration failed") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TraceMind database migration maintenance")
    parser.add_argument("command", choices=["upgrade"])
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "upgrade":
            upgrade_database(engine)
    except MigrationExecutionError as exc:
        logger.error("event=migration_failed category=migration_execution")
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
