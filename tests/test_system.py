import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import func, select
from app.db import Document, Job, Tenant
from app.retrieval import chunk_text, fuse
from app.schemas import SearchInput
from app.services import search
from app.worker import claim_job, fail, process, publish, renew, run_once
from conftest import ingest


def drain(runtime):
    while run_once(runtime, heartbeat=False):
        pass


def test_ingest_search_update_delete_and_cache(system):
    client, rt, a, auth, _, _ = system
    first = ingest(client, auth).json()
    assert first["revision"] == 1
    assert client.post("/api/search", headers=auth, json={"query": "recovery"}).json()["results"] == []
    drain(rt)
    response = client.post("/api/search", headers=auth, json={"query": "recovery"}).json()
    assert response["results"][0]["revision"] == 1
    assert not response["cache_hit"]
    assert client.post("/api/search", headers=auth, json={"query": "recovery"}).json()["cache_hit"]
    same = ingest(client, auth).json()
    assert same["unchanged"]
    with rt.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
    updated = ingest(client, auth, content="Updated recovery procedure uses checkpoint replay.").json()
    assert updated["revision"] == 2
    # Old revision stays available until activation.
    assert search(rt, a, SearchInput(query="recovery"))["results"][0]["revision"] == 1
    drain(rt)
    latest = search(rt, a, SearchInput(query="checkpoint"))
    assert latest["results"][0]["revision"] == 2
    assert "checkpoint" in latest["results"][0]["content"]
    assert client.delete("/api/documents/" + first["id"], headers=auth).status_code == 200
    # Tombstone invalidates cache even before the deletion worker runs.
    assert not search(rt, a, SearchInput(query="checkpoint"))["results"]
    drain(rt)
    assert ingest(client, auth, content="Restored recovery procedure.").json()["revision"] == 4
    drain(rt)
    assert search(rt, a, SearchInput(query="recovery"))["results"][0]["revision"] == 4


def test_tenant_isolation_on_all_paths(system):
    client, rt, _, auth_a, _, auth_b = system
    doc_a = ingest(client, auth_a, content="alpha-private-secret recovery").json()
    doc_b = ingest(client, auth_b, content="beta-private-secret recovery").json()
    drain(rt)
    for mode in ["keyword", "vector", "hybrid"]:
        result = client.post("/api/search", headers=auth_b, json={"query": "recovery", "mode": mode}).json()
        assert [row["document_id"] for row in result["results"]] == [doc_b["id"]]
        assert "alpha-private-secret" not in str(result)
    assert client.delete("/api/documents/" + doc_a["id"], headers=auth_b).status_code == 404
    assert client.post("/api/documents/" + doc_a["id"] + "/reindex", headers=auth_b).status_code == 404
    assert client.post("/api/jobs/" + doc_a["job_id"] + "/retry", headers=auth_b).status_code == 404
    for path in ["/api/documents", "/api/jobs", "/api/audit"]:
        assert doc_a["id"] not in client.get(path, headers=auth_b).text
    assert (
        client.post("/api/answer", headers=auth_b, json={"query": "recovery"}).json()["answer_mode"]
        == "extractive"
    )


def test_read_keys_revocation_and_admin_auth(system):
    client, rt, _, auth, _, other = system
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/admin/tenants", json={"name": "No"}).status_code == 403
    key = client.post("/api/keys", headers=auth, json={"label": "reader", "scope": "read"}).json()
    reader = {"Authorization": "Bearer " + key["api_key"]}
    assert client.get("/api/documents", headers=reader).status_code == 200
    assert ingest(client, reader).status_code == 403
    assert client.post("/api/keys", headers=reader, json={"label": "bad"}).status_code == 403
    assert client.delete("/api/keys/" + key["id"], headers=other).status_code == 404
    assert client.delete("/api/keys/" + key["id"], headers=auth).status_code == 200
    assert client.get("/api/me", headers=reader).status_code == 401


def test_old_worker_is_fenced_after_reclaim(system):
    client, rt, _, auth, _, _ = system
    doc = ingest(client, auth).json()
    old = claim_job(rt)
    with rt.session.begin() as session:
        session.get(Job, old.id).lease_until = time.time() - 1
    new = claim_job(rt)
    assert new.token != old.token
    assert not renew(rt, old)
    assert not publish(rt, old, 900)
    process(rt, new)
    assert not publish(rt, old, 900)
    with rt.session() as session:
        assert session.get(Document, doc["id"]).chunk_count == 1
        assert session.get(Job, old.id).state == "done"


def test_superseded_work_never_reactivates_old_revision(system):
    client, rt, a, auth, _, _ = system
    doc = ingest(client, auth).json()
    old = claim_job(rt)
    ingest(client, auth, content="New revision about recovery.")
    process(rt, old)
    drain(rt)
    with rt.session() as session:
        assert session.get(Job, old.id).state == "superseded"
        assert session.get(Document, doc["id"]).active_revision == 2
    assert search(rt, a, SearchInput(query="recovery"))["results"][0]["revision"] == 2


def test_delete_during_inflight_index_cannot_resurrect(system, monkeypatch):
    client, rt, a, auth, _, _ = system
    doc = ingest(client, auth).json()
    claim = claim_job(rt)
    original = rt.index.put

    def put_then_delete(rows):
        original(rows)
        client.delete("/api/documents/" + doc["id"], headers=auth)

    monkeypatch.setattr(rt.index, "put", put_then_delete)
    process(rt, claim)
    assert search(rt, a, SearchInput(query="recovery"))["results"] == []


def test_failure_retry_dead_letter_and_manual_recovery(system, monkeypatch):
    client, rt, _, auth, _, _ = system
    doc = ingest(client, auth).json()
    rt.settings.max_attempts = 2
    real = rt.embeddings.embed

    def broken(_):
        raise RuntimeError("Sensitive content must not leak")

    monkeypatch.setattr(rt.embeddings, "embed", broken)
    assert run_once(rt, heartbeat=False)
    with rt.session.begin() as session:
        job = session.get(Job, doc["job_id"])
        assert job.state == "queued"
        assert "Sensitive" not in job.error
        job.available_at = 0
    assert run_once(rt, heartbeat=False)
    with rt.session() as session:
        assert session.get(Job, doc["job_id"]).state == "dead"
    assert client.post("/api/jobs/" + doc["job_id"] + "/retry", headers=auth).status_code == 202
    monkeypatch.setattr(rt.embeddings, "embed", real)
    drain(rt)
    assert client.get("/api/stats", headers=auth).json()["jobs"]["done"] == 1


def test_crash_attempt_limit(system):
    client, rt, _, auth, _, _ = system
    doc = ingest(client, auth).json()
    rt.settings.max_attempts = 1
    claim = claim_job(rt)
    with rt.session.begin() as session:
        session.get(Job, claim.id).lease_until = 0
    assert claim_job(rt) is None
    with rt.session() as session:
        assert session.get(Job, doc["job_id"]).state == "dead"


def test_atomic_batch_and_backpressure(system):
    client, rt, _, auth, _, _ = system
    rt.settings.max_pending_jobs = 1
    data = {"documents": [dict(external_id=str(i), title="x", content="some text") for i in range(2)]}
    assert client.post("/api/documents/batch", headers=auth, json=data).status_code == 429
    assert client.get("/api/stats", headers=auth).json()["documents"] == 0
    data["documents"][1]["external_id"] = "0"
    assert client.post("/api/documents/batch", headers=auth, json=data).status_code == 422


def test_concurrent_claims_and_duplicate_upserts(system):
    client, rt, _, auth, _, _ = system
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ingest(client, auth).json(), range(8)))
    assert sum(not row["unchanged"] for row in results) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: claim_job(rt), range(4)))
    assert sum(claim is not None for claim in claims) == 1


def test_upload_filter_validation_and_empty_evidence(system):
    client, rt, _, auth, _, _ = system
    uploaded = client.post(
        "/api/documents/upload?collection=finance",
        headers=auth,
        files={"file": ("budget.md", b"The budget allocation is approved.", "text/markdown")},
    )
    assert uploaded.status_code == 202
    drain(rt)
    query = {"query": "budget", "mode": "keyword", "collection": "engineering"}
    assert client.post("/api/search", headers=auth, json=query).json()["results"] == []
    query["collection"] = "finance"
    assert len(client.post("/api/search", headers=auth, json=query).json()["results"]) == 1
    assert (
        client.post("/api/documents/upload", headers=auth, files={"file": ("script.exe", b"bad")}).status_code
        == 415
    )
    assert client.post("/api/search", headers=auth, json={"query": " "}).status_code == 422
    assert (
        client.post("/api/answer", headers=auth, json={"query": "unmatched", "mode": "keyword"}).json()[
            "answer_mode"
        ]
        == "no_evidence"
    )


def test_untrusted_index_results_are_rejected(system, monkeypatch):
    client, rt, a, auth_a, _, auth_b = system
    doc = ingest(client, auth_b).json()
    drain(rt)
    monkeypatch.setattr(
        rt.index,
        "search",
        lambda *args: {
            "keyword": [
                {"id": "bad", "tenant_id": a, "document_id": doc["id"], "revision": 1, "content": "secret"}
            ]
        },
    )
    assert search(rt, a, SearchInput(query="secret"))["results"] == []


def test_rate_limit_and_request_bound(system):
    client, rt, _, auth, _, _ = system
    rt.settings.rate_limit = 1
    assert client.get("/api/me", headers=auth).status_code == 200
    assert client.get("/api/me", headers=auth).status_code == 429
    assert (
        client.post(
            "/api/search", content=b"x" * 6_000_001, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )


def test_chunking_and_rrf():
    assert chunk_text("a b c d e f", 4, 1) == ["a b c d", "d e f"]
    assert chunk_text("a b c d", 4, 1) == ["a b c d"]
    rows = fuse({"keyword": [{"id": "a"}, {"id": "b"}], "vector": [{"id": "b"}, {"id": "a"}]}, 2)
    assert rows[0]["score"] == rows[1]["score"]
    assert rows[0]["ranks"] == {"keyword": 1, "vector": 2}


def test_failure_does_not_change_generation_or_publish(system):
    client, rt, a, auth, _, _ = system
    doc = ingest(client, auth).json()
    claim = claim_job(rt)
    with rt.session() as session:
        generation = session.get(Tenant, a).generation
    fail(rt, claim, RuntimeError("no"))
    with rt.session() as session:
        assert session.get(Tenant, a).generation == generation
        assert session.get(Document, doc["id"]).active_revision == 0
