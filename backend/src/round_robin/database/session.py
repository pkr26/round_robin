from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from round_robin.configuration import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def configure_database(url: str):
    global engine
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    SessionLocal.configure(bind=engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        yield session
