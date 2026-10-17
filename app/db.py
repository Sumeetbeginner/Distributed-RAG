import time
import uuid
from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(120), default="default")
    scope: Mapped[str] = mapped_column(String(16), default="write")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id"),
        Index("ix_document_tenant_updated", "tenant_id", "updated_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(240))
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    collection: Mapped[str] = mapped_column(String(100), default="general")
    source: Mapped[str] = mapped_column(String(1000), default="")
    checksum: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active_revision: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("document_id", "revision"),
        Index("ix_job_claim", "state", "available_at", "lease_until"),
        Index("ix_job_tenant_created", "tenant_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    revision: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(16), default="index")
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    token: Mapped[str] = mapped_column(String(36), default="")
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=time.time)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    error: Mapped[str] = mapped_column(String(500), default="")


class LocalChunk(Base):
    __tablename__ = "local_chunks"
    __table_args__ = (Index("ix_chunk_tenant_doc", "tenant_id", "document_id", "revision"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    document_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    collection: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(1000))
    vector: Mapped[list] = mapped_column(JSON)


class Audit(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


def make_database(url):
    sqlite = url.startswith("sqlite")
    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30} if sqlite else {}, pool_pre_ping=True
    )
    if sqlite:

        @event.listens_for(engine, "connect")
        def sqlite_setup(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def sqlite_begin(connection):
            # SQLite demonstration mode serializes transactions; PostgreSQL uses row locks.
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine, sessionmaker(engine, expire_on_commit=False)
