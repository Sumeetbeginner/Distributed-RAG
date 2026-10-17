import httpx
import pytest
from app.config import Settings
from app.retrieval import Embeddings
from app.worker import run_once
from conftest import ingest


def test_embedding_provider_checks_dimensions_and_batch_indices():
    model = Embeddings(Settings(_env_file=None, embedding_provider="openai", embedding_dimensions=32))
    model.client.close()

    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 32}]})

    model.client = httpx.Client(transport=httpx.MockTransport(handler))
    result = model.embed(["hello"])
    assert len(result[0]) == 32
    assert sum(v * v for v in result[0]) == pytest.approx(1)
    with pytest.raises(ValueError):
        model.embed(["hello", "world"])
    model.client.close()


def test_hash_vectors_are_stable_and_nonzero_for_punctuation():
    model = Embeddings(Settings(_env_file=None))
    assert model.embed(["hello world"]) == model.embed(["hello world"])
    assert sum(v * v for v in model.embed(["!!!"])[0]) == pytest.approx(1)
    model.client.close()


def test_generated_answer_validates_citation_ids(system, monkeypatch):
    client, rt, _, auth, _, _ = system
    ingest(client, auth)
    run_once(rt, heartbeat=False)
    rt.settings.llm_api_key = "test-key"
    real_client = httpx.Client

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "A fabricated source [99]."}}]})

    monkeypatch.setattr(
        "app.rag.httpx.Client", lambda **kw: real_client(transport=httpx.MockTransport(handler))
    )
    response = client.post("/api/answer", headers=auth, json={"query": "recovery"}).json()
    assert response["answer_mode"] == "unverified"
    assert "fabricated" not in response["answer"]
    assert response["citations"][0]["number"] == 1
