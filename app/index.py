import json
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
from sqlalchemy import delete, select
from app.db import Document, LocalChunk
from app.retrieval import bm25


FIELDS = ("id", "tenant_id", "document_id", "revision", "ordinal", "title", "content", "collection", "source")


class LocalIndex:
    def __init__(self, runtime):
        self.runtime = runtime

    def initialize(self):
        pass

    def put(self, rows):
        with self.runtime.session.begin() as session:
            for row in rows:
                session.merge(LocalChunk(**row))

    def cleanup(self, tenant_id, document_id, before_revision):
        with self.runtime.session.begin() as session:
            session.execute(
                delete(LocalChunk).where(
                    LocalChunk.tenant_id == tenant_id,
                    LocalChunk.document_id == document_id,
                    LocalChunk.revision < before_revision,
                )
            )

    def search(self, tenant_id, query, vector, mode, collection, count):
        with self.runtime.session() as session:
            stmt = (
                select(LocalChunk)
                .join(Document, Document.id == LocalChunk.document_id)
                .where(
                    LocalChunk.tenant_id == tenant_id,
                    Document.tenant_id == tenant_id,
                    Document.deleted.is_(False),
                    LocalChunk.revision == Document.active_revision,
                )
            )
            if collection:
                stmt = stmt.where(LocalChunk.collection == collection)
            chunks = session.scalars(stmt.limit(self.runtime.settings.local_max_chunks + 1)).all()
            if len(chunks) > self.runtime.settings.local_max_chunks:
                raise ValueError("Local search limit reached; use the OpenSearch backend")
            rows = [{**{key: getattr(row, key) for key in FIELDS}, "vector": row.vector} for row in chunks]
        ranked = {}
        if mode != "vector":
            ranked["keyword"] = bm25(query, rows)[:count]
        if mode != "keyword":
            ranked["vector"] = sorted(
                [{**row, "raw_score": sum(a * b for a, b in zip(vector, row["vector"]))} for row in rows],
                key=lambda row: (-row["raw_score"], row["id"]),
            )[:count]
        return ranked

    def health(self):
        return {"backend": "local", "status": "demo", "shards": 1, "replicas": 0}

    def close(self):
        pass


class FailoverClient:
    """Round-robin across configured nodes, retrying only transient transport/server failures."""

    def __init__(self, settings):
        urls = [url.strip() for url in settings.opensearch_urls.split(",") if url.strip()]
        urls = urls or [settings.opensearch_url]
        self.clients = [
            httpx.Client(
                base_url=url.rstrip("/"),
                timeout=45,
                auth=(settings.opensearch_username, settings.opensearch_password)
                if settings.opensearch_username
                else None,
            )
            for url in urls
        ]
        self.turns, self.lock = itertools.count(), threading.Lock()

    def request(self, method, path, **kwargs):
        with self.lock:
            start = next(self.turns) % len(self.clients)
        last = None
        for offset in range(len(self.clients)):
            client = self.clients[(start + offset) % len(self.clients)]
            try:
                response = client.request(method, path, **kwargs)
                if response.status_code not in {502, 503, 504}:
                    return response
                response.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                last = error
        raise last

    def head(self, path, **kwargs):
        return self.request("HEAD", path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def close(self):
        for client in self.clients:
            client.close()


class OpenSearchIndex:
    def __init__(self, runtime):
        self.settings = runtime.settings
        s = self.settings
        self.client = FailoverClient(s)
        self.name = s.index_name

    def initialize(self):
        s = self.settings
        response = self.client.head(f"/{self.name}")
        if response.status_code == 404:
            body = {
                "settings": {
                    "index.knn": True,
                    "number_of_shards": s.index_shards,
                    "number_of_replicas": s.index_replicas,
                },
                "mappings": {
                    "dynamic": "strict",
                    "_meta": {
                        "embedding_provider": s.embedding_provider,
                        "embedding_model": s.embedding_model,
                    },
                    "properties": {
                        **{
                            key: {"type": "keyword"}
                            for key in ["id", "tenant_id", "document_id", "collection"]
                        },
                        "revision": {"type": "integer"},
                        "ordinal": {"type": "integer"},
                        "title": {"type": "text"},
                        "content": {"type": "text"},
                        "source": {"type": "keyword", "index": False},
                        "vector": {
                            "type": "knn_vector",
                            "dimension": s.embedding_dimensions,
                            "method": {
                                "name": "hnsw",
                                "engine": "lucene",
                                "space_type": "cosinesimil",
                                "parameters": {"ef_construction": 128, "m": 16},
                            },
                        },
                    },
                },
            }
            result = self.client.put(f"/{self.name}", json=body)
            if result.status_code != 400 or "resource_already_exists_exception" not in result.text:
                result.raise_for_status()
        else:
            response.raise_for_status()
        mapping = self.client.get(f"/{self.name}/_mapping")
        mapping.raise_for_status()
        spec = mapping.json()[self.name]["mappings"]
        if spec["properties"]["vector"]["dimension"] != s.embedding_dimensions or spec.get("_meta") != {
            "embedding_provider": s.embedding_provider,
            "embedding_model": s.embedding_model,
        }:
            raise ValueError("Embedding configuration changed: create a new index and reindex all documents")

    def put(self, rows):
        for start in range(0, len(rows), 100):
            payload = "".join(
                json.dumps({"index": {"_index": self.name, "_id": row["id"]}}) + "\n" + json.dumps(row) + "\n"
                for row in rows[start : start + 100]
            )
            response = self.client.post(
                "/_bulk",
                params={"refresh": "wait_for"},
                content=payload,
                headers={"Content-Type": "application/x-ndjson"},
            )
            response.raise_for_status()
            if response.json().get("errors"):
                raise RuntimeError("OpenSearch bulk indexing partially failed; retry is idempotent")

    def cleanup(self, tenant_id, document_id, before_revision):
        response = self.client.post(
            f"/{self.name}/_delete_by_query",
            params={"conflicts": "proceed"},
            json={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"document_id": document_id}},
                            {"range": {"revision": {"lt": before_revision}}},
                        ]
                    }
                }
            },
        )
        response.raise_for_status()
        if response.json().get("failures") or response.json().get("timed_out"):
            raise RuntimeError("OpenSearch cleanup incomplete")

    def search(self, tenant_id, query, vector, mode, collection, count):
        filters = [{"term": {"tenant_id": tenant_id}}]
        if collection:
            filters.append({"term": {"collection": collection}})
        requests = {}
        if mode != "vector":
            requests["keyword"] = {
                "bool": {
                    "filter": filters,
                    "must": [{"multi_match": {"query": query, "fields": ["title^2", "content"]}}],
                }
            }
        if mode != "keyword":
            requests["vector"] = {
                "knn": {"vector": {"vector": vector, "k": count, "filter": {"bool": {"filter": filters}}}}
            }

        def execute(item):
            channel, body = item
            response = self.client.post(
                f"/{self.name}/_search",
                json={"size": count, "query": body, "_source": {"excludes": ["vector"]}},
                params={"allow_partial_search_results": "false"},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("timed_out") or data.get("_shards", {}).get("failed", 0):
                raise RuntimeError("Search incomplete; retry when all shards are available")
            return channel, [{**hit["_source"], "raw_score": hit["_score"]} for hit in data["hits"]["hits"]]

        with ThreadPoolExecutor(max_workers=2) as pool:
            return dict(pool.map(execute, requests.items()))

    def health(self):
        result = self.client.get(f"/_cluster/health/{self.name}")
        result.raise_for_status()
        data = result.json()
        return {
            "backend": "opensearch",
            "status": data["status"],
            "active_shards": data["active_shards"],
            "nodes": data["number_of_nodes"],
        }

    def close(self):
        self.client.close()
