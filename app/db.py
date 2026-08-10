"""Relational store: accounts, device ownership, and the import ledger.

SQLite by default, but everything goes through SQLAlchemy and DATABASE_URL so that
moving to Postgres or MariaDB later is a configuration change. No raw sqlite3, no
dialect-specific SQL.

The bulk of the data lives in InfluxDB; this holds only what Influx is bad at
answering — "who owns this machine" and "have I already imported this file".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Device(Base):
    """Maps a machine serial to the account that first uploaded data from it.

    Without this, two people sharing an instance would silently merge their therapy
    data under whichever account happened to upload a given file.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model: Mapped[str] = mapped_column(String(32))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Upload(Base):
    """One imported session file.

    file_sha256 is the deduplication key. Filenames are useless for this: the device
    restarts numbering at 00100001.BYS after a reset, so the same name refers to
    different nights over time.
    """

    __tablename__ = "uploads"
    __table_args__ = (UniqueConstraint("user_id", "file_sha256", name="uq_user_file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    serial: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    session_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    night_date: Mapped[str] = mapped_column(String(10), index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    ahi: Mapped[float]
    avg_pressure: Mapped[float]
    p90_pressure: Mapped[float]
    avg_leak: Mapped[float]
    points_written: Mapped[int] = mapped_column(Integer)
    warnings: Mapped[str] = mapped_column(String(2000), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def create_db_engine(settings: Settings) -> Engine:
    """Build the engine, with the SQLite-specific setup SQLite actually needs."""
    url = settings.database_url
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        # sqlite:///./data/app.db -> make sure ./data exists before connecting
        path_part = url.split("///", 1)[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        # FastAPI serves requests on a threadpool; SQLite's default thread check
        # would reject connections reused across them.
        connect_args={"check_same_thread": False} if is_sqlite else {},
        pool_pre_ping=True,
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            # WAL lets the sessions page be read while a bulk import is still
            # writing; the default rollback journal would block it.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
