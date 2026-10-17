"""Opt-in tests against real PostgreSQL + OpenSearch + Redis, never mocks."""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.config import Settings
from app.db import Document, Job, Tenant
from app.runtime import Runtime
from app.schemas import DocumentInput, SearchInput
from app.services import delete_document, search, upsert_documents
from app.worker import claim_job, process, publish

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DISTRIBUTED_TESTS") != "1", reason="Requires distributed services"
)


@pytest.fixture
def distributed():
    settings = Settings(index_name="test-" + uuid.uuid4().hex, rate_limit=10000)
    assert settings.search_backend == "opensearch" and settings.database_url.startswith("postgresql")
    runtime = Runtime(settings)
    runtime.index.initialize()
    tenants = []
    with runtime.session.begin() as session:
        for name in ["integration-a", "integration-b"]:
            tenant = Tenant(name=name)
            session.add(tenant)
            session.flush()
            tenants.append(tenant.id)
    try:
        yield runtime, tenants
    finally:
        runtime.index.client.delete("/" + settings.index_name)
        with runtime.session.begin() as session:
            from sqlalchemy import delete
            from app.db import Audit

            for model in [Job, Document, Audit]:
                session.execute(delete(model).where(model.tenant_id.in_(tenants)))
            session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        runtime.close()


def test_real_cluster_hybrid_isolation_and_fencing(distributed):
    rt, (a, b) = distributed
    # Claim only our jobs to avoid relying on an empty shared queue.
    ids = []
    for tenant in (a, b):
        doc = upsert_documents(
            rt,
            tenant,
            [
                DocumentInput(
                    external_id="runbook",
                    title="Recovery",
                    content="Database recovery uses renewable leases and replication.",
                )
            ],
        )[0]
        ids.append(doc["id"])
    claims = []
    # This test must run with regular workers stopped (documented command).
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim_job(rt), range(2)))
    assert all(claims) and len({c.id for c in claims}) == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda claim: process(rt, claim), claims))
    for mode in ["keyword", "vector", "hybrid"]:
        query = SearchInput(query="recovery leases", mode=mode)
        assert [r["document_id"] for r in search(rt, a, query)["results"]] == [ids[0]]
        assert [r["document_id"] for r in search(rt, b, query)["results"]] == [ids[1]]
    assert search(rt, a, SearchInput(query="recovery leases"))["cache_hit"]
    updated = upsert_documents(
        rt,
        a,
        [
            DocumentInput(
                external_id="runbook", title="Recovery", content="New recovery uses checkpoint replay."
            )
        ],
    )[0]
    old = claim_job(rt)
    with rt.session.begin() as session:
        session.get(Job, old.id).lease_until = time.time() - 1
    new = claim_job(rt)
    assert not publish(rt, old, 100)
    process(rt, new)
    assert search(rt, a, SearchInput(query="checkpoint"))["results"][0]["revision"] == updated["revision"]
    delete_document(rt, a, ids[0])
    assert search(rt, a, SearchInput(query="checkpoint"))["results"] == []
    process(rt, claim_job(rt))
    health = rt.index.health()
    assert health["status"] in ["green", "yellow"]
