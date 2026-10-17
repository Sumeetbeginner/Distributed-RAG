import hashlib
import json
import time
from fastapi import HTTPException
from sqlalchemy import func, select
from app.db import Audit, Document, Job, Tenant
from app.retrieval import fuse


def lock_tenant(session, tenant_id):
    return session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())


def document_view(doc):
    return {
        key: getattr(doc, key)
        for key in (
            "id",
            "external_id",
            "title",
            "collection",
            "source",
            "revision",
            "active_revision",
            "status",
            "chunk_count",
            "updated_at",
            "deleted",
        )
    }


def upsert_documents(runtime, tenant_id, inputs):
    if len({doc.external_id for doc in inputs}) != len(inputs):
        raise HTTPException(422, "Duplicate external_id within batch")
    if sum(len(doc.content) for doc in inputs) > 5_000_000:
        raise HTTPException(413, "Batch content exceeds 5 million characters")
    output = []
    with runtime.session.begin() as session:
        tenant = lock_tenant(session, tenant_id)
        pending = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.tenant_id == tenant_id, Job.state.in_(["queued", "running"]))
        )
        for data in inputs:
            if len(data.content) > runtime.settings.max_document_chars:
                raise HTTPException(413, "Document exceeds configured character limit")
            checksum = hashlib.sha256(json.dumps(data.model_dump(), sort_keys=True).encode()).hexdigest()
            doc = session.scalar(
                select(Document).where(
                    Document.tenant_id == tenant_id, Document.external_id == data.external_id
                )
            )
            if doc and doc.checksum == checksum and not doc.deleted:
                output.append({**document_view(doc), "unchanged": True})
                continue
            if pending >= runtime.settings.max_pending_jobs:
                raise HTTPException(429, "Indexing backlog is full; retry after workers catch up")
            pending += 1
            if not doc:
                doc = Document(tenant_id=tenant_id, **data.model_dump(), checksum=checksum)
                session.add(doc)
                session.flush()
            else:
                for field, value in data.model_dump().items():
                    setattr(doc, field, value)
                doc.checksum, doc.revision = checksum, doc.revision + 1
                doc.deleted, doc.status, doc.updated_at = False, "queued", time.time()
            job = Job(tenant_id=tenant_id, document_id=doc.id, revision=doc.revision)
            session.add(job)
            tenant.generation += 1
            session.add(Audit(tenant_id=tenant_id, action="document.upsert", resource_id=doc.id))
            session.flush()
            output.append({**document_view(doc), "job_id": job.id, "unchanged": False})
    return output


def delete_document(runtime, tenant_id, document_id):
    with runtime.session.begin() as session:
        tenant = lock_tenant(session, tenant_id)
        doc = session.scalar(
            select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
        )
        if not doc:
            raise HTTPException(404, "Document not found")
        if not doc.deleted:
            doc.deleted, doc.status, doc.revision = True, "deleted", doc.revision + 1
            doc.content, doc.chunk_count = "", 0
            doc.updated_at = time.time()
            tenant.generation += 1
            session.add(
                Job(tenant_id=tenant_id, document_id=doc.id, revision=doc.revision, operation="delete")
            )
            session.add(Audit(tenant_id=tenant_id, action="document.delete", resource_id=doc.id))
    return {"deleted": True}


def search(runtime, tenant_id, query):
    start = time.perf_counter()
    with runtime.session() as session:
        generation = session.get(Tenant, tenant_id).generation
    signature = json.dumps(query.model_dump(), sort_keys=True)
    key = f"drag:search:{tenant_id}:{generation}:" + hashlib.sha256(signature.encode()).hexdigest()
    cached = runtime.cache.get(key)
    if cached:
        return {**cached, "cache_hit": True, "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
    vector = runtime.embeddings.embed([query.query])[0] if query.mode != "keyword" else []
    candidates = runtime.index.search(
        tenant_id, query.query, vector, query.mode, query.collection, min(max(query.limit * 10, 100), 500)
    )
    ids = {row["document_id"] for rows in candidates.values() for row in rows}
    with runtime.session() as session:
        active = (
            {
                doc.id: doc.active_revision
                for doc in session.scalars(
                    select(Document).where(
                        Document.tenant_id == tenant_id, Document.id.in_(ids), Document.deleted.is_(False)
                    )
                )
            }
            if ids
            else {}
        )
    # Never trust external-index contents for authorization or visibility.
    candidates = {
        channel: [
            row
            for row in rows
            if row["tenant_id"] == tenant_id and active.get(row["document_id"]) == row["revision"]
        ]
        for channel, rows in candidates.items()
    }
    fused = fuse(candidates, 500)
    results, seen = [], set()
    for row in fused:
        if row["document_id"] in seen:
            continue
        seen.add(row["document_id"])
        result = {k: v for k, v in row.items() if k not in {"vector", "tenant_id", "raw_score"}}
        results.append(result)
        if len(results) >= query.limit:
            break
    response = {
        "results": results,
        "cache_hit": False,
        "mode": query.mode,
        "generation": generation,
        "embedding_provider": runtime.settings.embedding_provider,
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    }
    runtime.cache.put(key, response)
    return response
