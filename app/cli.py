import argparse
import json
from app.config import get_settings
from app.db import Tenant, Document
from sqlalchemy import select
from app.runtime import Runtime
from app.security import issue_key
from app.schemas import DocumentInput
from app.services import upsert_documents
from app.worker import run_once

DEMO = [
    (
        "architecture",
        "DistributedRAG architecture",
        "Engineering",
        "DistributedRAG separates its HTTP API from indexing workers. PostgreSQL stores documents and durable jobs in the same transaction. OpenSearch stores BM25 text and HNSW vectors across primary shards. Redis caches search responses. Workers claim jobs with SKIP LOCKED, renew leases, and publish only with a valid fencing token.",
    ),
    (
        "recovery",
        "Worker recovery runbook",
        "Operations",
        "When an indexing worker crashes, its lease expires and another worker reclaims the job. Every attempt has a unique fencing token. Expired workers cannot publish an active revision. Repeated failures enter the dead-letter queue. Operators can fix the dependency and retry the latest failed job. Monitor queue lag, attempt count, and dead-letter depth.",
    ),
    (
        "security",
        "Tenant isolation and API access",
        "Security",
        "Every API key belongs to exactly one tenant. Tenant identity comes from the validated key and never from request data. Search filters both lexical and vector candidates by tenant, then validates document ownership and active revision in PostgreSQL. Read keys can search and list documents. Write keys can ingest, delete, reindex, retry jobs and manage keys.",
    ),
    (
        "hybrid",
        "How hybrid retrieval ranks results",
        "Engineering",
        "Hybrid search combines BM25 lexical retrieval with vector nearest-neighbor retrieval. Reciprocal rank fusion scores each chunk by 1 divided by 60 plus its rank in each channel. Results are deduplicated by document. Keyword search excels at identifiers and exact terms; semantic embeddings help retrieve differently worded passages. Offline feature hashing is only a demonstration and does not understand meaning.",
    ),
    (
        "freshness",
        "Incremental indexing and cache freshness",
        "Engineering",
        "Document upserts use a stable external ID and a content checksum. Identical content and metadata do not create another job. Updates increment the revision and enqueue indexing atomically. Deletion immediately creates a tombstone. Cache keys include tenant identity, query options and the tenant generation, which changes on writes and index activation.",
    ),
    (
        "scale",
        "Scaling the search cluster",
        "Operations",
        "Use PostgreSQL, OpenSearch and Redis for the distributed deployment. Add worker replicas to increase ingestion concurrency. OpenSearch distributes chunks across primary shards and can replicate them across nodes. Benchmark recall, latency, indexing throughput and recovery before claiming million-document capacity. The local backend scans a bounded corpus and is only intended for development.",
    ),
]


def main():
    parser = argparse.ArgumentParser(description="DistributedRAG administration")
    parser.add_argument("command", choices=["init", "tenant", "demo", "drain", "gc"])
    parser.add_argument("--name", default="Acme Research")
    args = parser.parse_args()
    runtime = Runtime(get_settings())
    try:
        if args.command == "init":
            runtime.initialize()
            print("Database and index initialized.")
        elif args.command in {"tenant", "demo"}:
            with runtime.session.begin() as session:
                tenant = Tenant(name=args.name)
                session.add(tenant)
                session.flush()
                _, key = issue_key(session, tenant.id)
            if args.command == "demo":
                upsert_documents(
                    runtime,
                    tenant.id,
                    [
                        DocumentInput(external_id=id, title=title, collection=collection, content=content)
                        for id, title, collection, content in DEMO
                    ],
                )
                while run_once(runtime):
                    pass
            print(json.dumps({"tenant_id": tenant.id, "name": tenant.name, "api_key": key}, indent=2))
        elif args.command == "gc":
            # Keyset batches avoid loading a million-document table into memory.
            cursor = ""
            cleaned = 0
            while True:
                with runtime.session() as session:
                    docs = session.scalars(
                        select(Document).where(Document.id > cursor).order_by(Document.id).limit(200)
                    ).all()
                if not docs:
                    break
                for doc in docs:
                    revision = doc.revision if doc.deleted else doc.active_revision
                    if revision:
                        runtime.index.cleanup(doc.tenant_id, doc.id, revision)
                    cursor = doc.id
                    cleaned += 1
            print(f"Swept {cleaned} documents.")
        else:
            while run_once(runtime):
                pass
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
