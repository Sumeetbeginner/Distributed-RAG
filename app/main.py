import io
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from sqlalchemy import func, select, text
from app.config import get_settings
from app.db import ApiKey, Audit, Document, Job, Tenant
from app.rag import answer
from app.runtime import Runtime
from app.schemas import BatchInput, DocumentInput, KeyInput, SearchInput, TenantInput
from app.security import authenticate, issue_key, require_admin, require_write
from app.services import delete_document, document_view, lock_tenant, search, upsert_documents

logger = logging.getLogger("distributedrag.api")


def create_app(settings=None):
    runtime = Runtime(settings or get_settings())

    @asynccontextmanager
    async def lifespan(app):
        # Schema/index initialization is an explicit CLI operation, never concurrent API startup DDL.
        yield
        runtime.close()

    app = FastAPI(title="DistributedRAG", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware("http")
    async def security_headers(request, call_next):
        # Bound every request before JSON or multipart parsing, including chunked uploads.
        if request.method in {"POST", "PUT", "PATCH"}:
            cap = (
                runtime.settings.max_upload_bytes + 100_000
                if request.url.path == "/api/documents/upload"
                else 6_000_000
            )
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > cap:
                    return JSONResponse({"detail": "Request exceeds body limit"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; "
                "script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
            )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        logger.error("request failed type=%s path=%s", type(error).__name__, request.url.path)
        return JSONResponse({"detail": "Service unavailable; retry or check service health"}, status_code=503)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        try:
            with runtime.session() as session:
                session.execute(text("SELECT 1"))
                session.execute(select(Tenant.id).limit(1))
            index = runtime.index.health()
            if index["status"] == "red":
                raise RuntimeError("index unavailable")
            return {"status": "ready"}
        except Exception as error:
            raise HTTPException(503, "Database or index not ready") from error

    @app.post("/api/admin/tenants", status_code=201)
    def create_tenant(data: TenantInput, request: Request, x_admin_key: str = Header(default="")):
        require_admin(request, x_admin_key)
        with runtime.session.begin() as session:
            tenant = Tenant(name=data.name)
            session.add(tenant)
            session.flush()
            key_id, key = issue_key(session, tenant.id)
            return {"tenant_id": tenant.id, "name": tenant.name, "key_id": key_id, "api_key": key}

    @app.get("/api/me")
    def me(principal=Depends(authenticate)):
        with runtime.session() as session:
            tenant = session.get(Tenant, principal.tenant_id)
            return {
                "tenant_id": tenant.id,
                "name": tenant.name,
                "scope": principal.scope,
                "backend": runtime.settings.search_backend,
                "embeddings": runtime.settings.embedding_provider,
                "llm_enabled": bool(runtime.settings.llm_api_key),
            }

    @app.post("/api/documents", status_code=202)
    def ingest(data: DocumentInput, principal=Depends(authenticate)):
        require_write(principal)
        return upsert_documents(runtime, principal.tenant_id, [data])[0]

    @app.post("/api/documents/batch", status_code=202)
    def batch(data: BatchInput, principal=Depends(authenticate)):
        require_write(principal)
        return {"documents": upsert_documents(runtime, principal.tenant_id, data.documents)}

    @app.post("/api/documents/upload", status_code=202)
    def upload(
        file: UploadFile,
        collection: str = Query("general", min_length=1, max_length=100),
        principal=Depends(authenticate),
    ):
        require_write(principal)
        filename = Path(file.filename or "document.txt").name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".txt", ".md", ".csv", ".pdf"}:
            raise HTTPException(415, "Supported files: TXT, Markdown, CSV and text PDFs")
        raw = file.file.read(runtime.settings.max_upload_bytes + 1)
        if len(raw) > runtime.settings.max_upload_bytes:
            raise HTTPException(413, "File exceeds upload size limit")
        try:
            if suffix == ".pdf":
                reader = PdfReader(io.BytesIO(raw))
                if len(reader.pages) > 300:
                    raise ValueError("PDF exceeds 300 pages")
                pieces, size = [], 0
                for page in reader.pages:
                    part = page.extract_text() or ""
                    size += len(part)
                    if size > runtime.settings.max_document_chars:
                        raise ValueError("Extracted text too large")
                    pieces.append(part)
                content = "\n".join(pieces)
            else:
                content = raw.decode("utf-8-sig")
            data = DocumentInput(external_id=filename, title=filename, content=content, collection=collection)
        except Exception as error:
            raise HTTPException(
                422, "Could not extract valid text; use UTF-8 text or a text-based PDF"
            ) from error
        return upsert_documents(runtime, principal.tenant_id, [data])[0]

    @app.get("/api/documents")
    def documents(
        before: float | None = None,
        after_id: str = "",
        limit: int = Query(30, ge=1, le=100),
        principal=Depends(authenticate),
    ):
        with runtime.session() as session:
            stmt = select(Document).where(
                Document.tenant_id == principal.tenant_id, Document.deleted.is_(False)
            )
            if before is not None:
                from sqlalchemy import and_, or_

                stmt = stmt.where(
                    or_(
                        Document.updated_at < before,
                        and_(Document.updated_at == before, Document.id < after_id),
                    )
                )
            rows = session.scalars(
                stmt.order_by(Document.updated_at.desc(), Document.id.desc()).limit(limit + 1)
            ).all()
            more = len(rows) > limit
            rows = rows[:limit]
            return {
                "documents": [document_view(doc) for doc in rows],
                "next": {"before": rows[-1].updated_at, "after_id": rows[-1].id} if more else None,
            }

    @app.delete("/api/documents/{document_id}")
    def remove(document_id: str, principal=Depends(authenticate)):
        require_write(principal)
        return delete_document(runtime, principal.tenant_id, document_id)

    @app.post("/api/documents/{document_id}/reindex", status_code=202)
    def reindex(document_id: str, principal=Depends(authenticate)):
        require_write(principal)
        with runtime.session.begin() as session:
            tenant = lock_tenant(session, principal.tenant_id)
            doc = session.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.tenant_id == principal.tenant_id,
                    Document.deleted.is_(False),
                )
            )
            if not doc:
                raise HTTPException(404, "Document not found")
            pending = session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.tenant_id == principal.tenant_id, Job.state.in_(["queued", "running"]))
            )
            if pending >= runtime.settings.max_pending_jobs:
                raise HTTPException(429, "Indexing backlog is full")
            doc.revision += 1
            doc.status, doc.updated_at = "queued", time.time()
            tenant.generation += 1
            job = Job(tenant_id=principal.tenant_id, document_id=doc.id, revision=doc.revision)
            session.add(job)
            session.add(Audit(tenant_id=principal.tenant_id, action="document.reindex", resource_id=doc.id))
            session.flush()
            return {"job_id": job.id, "revision": doc.revision}

    @app.post("/api/search")
    def retrieve(data: SearchInput, principal=Depends(authenticate)):
        return search(runtime, principal.tenant_id, data)

    @app.post("/api/answer")
    def rag(data: SearchInput, principal=Depends(authenticate)):
        return answer(runtime, principal.tenant_id, data)

    @app.get("/api/jobs")
    def jobs(state: str | None = None, limit: int = Query(50, ge=1, le=100), principal=Depends(authenticate)):
        with runtime.session() as session:
            stmt = select(Job).where(Job.tenant_id == principal.tenant_id)
            if state:
                stmt = stmt.where(Job.state == state)
            rows = session.scalars(stmt.order_by(Job.created_at.desc()).limit(limit)).all()
            return {
                "jobs": [
                    {
                        k: getattr(row, k)
                        for k in (
                            "id",
                            "document_id",
                            "revision",
                            "operation",
                            "state",
                            "attempts",
                            "error",
                            "created_at",
                            "available_at",
                            "lease_until",
                        )
                    }
                    for row in rows
                ]
            }

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, principal=Depends(authenticate)):
        require_write(principal)
        with runtime.session.begin() as session:
            lock_tenant(session, principal.tenant_id)
            # Tenant lock serializes against worker publication and document mutation.
            job = session.scalar(
                select(Job).where(Job.id == job_id, Job.tenant_id == principal.tenant_id).with_for_update()
            )
            if not job:
                raise HTTPException(404, "Job not found")
            doc = session.get(Document, job.document_id)
            if job.state != "dead" or job.revision != doc.revision:
                raise HTTPException(409, "Only the latest dead-letter job can be retried")
            job.state, job.attempts, job.available_at, job.error = "queued", 0, time.time(), ""
            if not doc.deleted:
                doc.status = "queued"
            session.add(Audit(tenant_id=principal.tenant_id, action="job.retry", resource_id=job.id))
            return {"queued": True}

    @app.get("/api/stats")
    def stats(principal=Depends(authenticate)):
        with runtime.session() as session:
            docs, chunks = session.execute(
                select(func.count(Document.id), func.coalesce(func.sum(Document.chunk_count), 0)).where(
                    Document.tenant_id == principal.tenant_id, Document.deleted.is_(False)
                )
            ).one()
            states = dict(
                session.execute(
                    select(Job.state, func.count())
                    .where(Job.tenant_id == principal.tenant_id)
                    .group_by(Job.state)
                ).all()
            )
            oldest = session.scalar(
                select(func.min(Job.available_at)).where(
                    Job.tenant_id == principal.tenant_id, Job.state == "queued"
                )
            )
            return {
                "documents": docs,
                "chunks": chunks,
                "jobs": states,
                "queue_lag_seconds": round(max(0, time.time() - oldest), 1) if oldest else 0,
                "backend": runtime.settings.search_backend,
                "embedding_provider": runtime.settings.embedding_provider,
            }

    @app.get("/api/audit")
    def audit(principal=Depends(authenticate)):
        with runtime.session() as session:
            rows = session.scalars(
                select(Audit)
                .where(Audit.tenant_id == principal.tenant_id)
                .order_by(Audit.created_at.desc())
                .limit(50)
            ).all()
            return {
                "events": [
                    {"action": row.action, "resource_id": row.resource_id, "created_at": row.created_at}
                    for row in rows
                ]
            }

    @app.get("/api/keys")
    def keys(principal=Depends(authenticate)):
        require_write(principal)
        with runtime.session() as session:
            rows = session.scalars(select(ApiKey).where(ApiKey.tenant_id == principal.tenant_id)).all()
            return {
                "keys": [
                    {
                        "id": row.id,
                        "label": row.label,
                        "scope": row.scope,
                        "revoked": row.revoked,
                        "current": row.id == principal.key_id,
                    }
                    for row in rows
                ]
            }

    @app.post("/api/keys", status_code=201)
    def create_key(data: KeyInput, principal=Depends(authenticate)):
        require_write(principal)
        with runtime.session.begin() as session:
            key_id, key = issue_key(session, principal.tenant_id, data.label, data.scope)
            session.add(Audit(tenant_id=principal.tenant_id, action="key.create", resource_id=key_id))
            return {"id": key_id, "api_key": key}

    @app.delete("/api/keys/{key_id}")
    def revoke(key_id: str, principal=Depends(authenticate)):
        require_write(principal)
        if key_id == principal.key_id:
            raise HTTPException(409, "Create and switch to another write key before revoking this key")
        with runtime.session.begin() as session:
            key = session.scalar(
                select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == principal.tenant_id)
            )
            if not key:
                raise HTTPException(404, "Key not found")
            key.revoked = True
            session.add(Audit(tenant_id=principal.tenant_id, action="key.revoke", resource_id=key.id))
            return {"revoked": True}

    @app.get("/api/cluster")
    def cluster(principal=Depends(authenticate)):
        require_write(principal)
        return runtime.index.health()

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="console")
    return app


app = create_app()
