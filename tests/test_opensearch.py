import json
from types import SimpleNamespace
import httpx
import pytest
from app.config import Settings
from app.index import OpenSearchIndex


def index_with_handler(handler):
    index = OpenSearchIndex(SimpleNamespace(settings=Settings(_env_file=None, search_backend="opensearch")))
    index.client.close()
    index.client = httpx.Client(base_url="http://index", transport=httpx.MockTransport(handler))
    return index


def test_both_search_channels_include_tenant_and_collection_filters():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.url.params["allow_partial_search_results"] == "false"
        return httpx.Response(200, json={"hits": {"hits": []}})

    index = index_with_handler(handler)
    index.search("tenant-123", "recovery", [0.1] * 384, "hybrid", "operations", 100)
    assert len(requests) == 2
    for body in requests:
        encoded = json.dumps(body)
        assert "tenant-123" in encoded and "operations" in encoded
    vector = next(body for body in requests if "knn" in body["query"])
    assert vector["query"]["knn"]["vector"]["filter"]["bool"]["filter"][0] == {
        "term": {"tenant_id": "tenant-123"}
    }
    index.close()


def test_partial_bulk_failure_is_not_accepted():
    def handler(request):
        assert request.url.params["refresh"] == "wait_for"
        return httpx.Response(200, json={"errors": True})

    index = index_with_handler(handler)
    with pytest.raises(RuntimeError):
        index.put([{"id": "doc:1:0", "content": "test"}])
    index.close()


def test_partial_search_failure_is_not_silently_returned():
    index = index_with_handler(
        lambda request: httpx.Response(
            200, json={"timed_out": False, "_shards": {"failed": 1}, "hits": {"hits": []}}
        )
    )
    with pytest.raises(RuntimeError):
        index.search("tenant", "hello", [], "keyword", None, 10)
    index.close()


def test_cleanup_is_tenant_scoped_and_never_removes_newer_revisions():
    def handler(request):
        filters = json.loads(request.content)["query"]["bool"]["filter"]
        assert filters == [
            {"term": {"tenant_id": "tenant"}},
            {"term": {"document_id": "doc"}},
            {"range": {"revision": {"lt": 4}}},
        ]
        return httpx.Response(200, json={"failures": [], "timed_out": False})

    index = index_with_handler(handler)
    index.cleanup("tenant", "doc", 4)
    index.close()


def test_node_failover_retries_connection_failure():
    from app.index import FailoverClient

    client = FailoverClient(Settings(_env_file=None, opensearch_urls="http://one,http://two"))
    client.close()

    def offline(request):
        raise httpx.ConnectError("node offline", request=request)

    client.clients = [
        httpx.Client(base_url="http://one", transport=httpx.MockTransport(offline)),
        httpx.Client(
            base_url="http://two",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "green"})),
        ),
    ]
    assert client.get("/_cluster/health").json()["status"] == "green"
    client.close()
