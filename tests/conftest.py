import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_session
from app.main import app


@pytest.fixture
def client():
    database_url = os.getenv("TEST_DATABASE_URL")
    schema = None
    if database_url:
        engine = create_engine(database_url)
        schema = "tracemind_test_" + uuid4().hex
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    # Deliberately skip production startup: these tests own their isolated database.
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        if schema:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
