# DistributedRAG

**A tenant-isolated enterprise search engine with durable ingestion, hybrid retrieval, and evidence-backed answers.**

FastAPI · PostgreSQL · OpenSearch · Redis · Python · responsive browser console

DistributedRAG is a runnable engineering project built around the hard parts of retrieval: incremental updates, tenant boundaries, competing workers, crash recovery, index visibility, cache invalidation, and distributed search. It includes an offline development mode and a three-node search cluster configuration.

> **Capacity is a target, not a measured claim.** The distributed architecture supports sharded indexing and horizontal workers. Million-document throughput, recall, and latency must be established with your corpus and hardware. The local backend is intentionally bounded and not designed for production scale.

## What works

- **Workspace console:** search playground, evidence passages, document library, uploads, job monitoring, key management, and audit events. Responsive dark UI, keyboard-accessible controls, explicit empty/loading/error states.
- **Multi-tenancy:** hashed tenant API keys; read/write scopes; server-derived tenant identity; filters on both retrieval paths; database verification of ownership and active revisions.
- **Hybrid search:** BM25 + HNSW nearest neighbors in OpenSearch, reciprocal rank fusion, collection filtering, per-channel rank explanations, and document deduplication.
- **Incremental ingestion:** stable external IDs, checksums, atomic document/job writes, batched JSON ingestion, TXT/Markdown/CSV/text-PDF upload, revisions, reindexing, and immediate deletion tombstones.
- **Durable workers:** PostgreSQL `FOR UPDATE SKIP LOCKED`, renewable leases, token-fenced publication, idempotent chunk IDs, bounded attempts, exponential retry delay with jitter, dead letters, and manual retry.
- **Distributed indexing:** configurable primary shards/replicas, multi-node client failover, three OpenSearch nodes in Compose, independent API and worker scaling.
- **Caching:** tenant/generation/query-scoped Redis cache, database-authoritative invalidation, bounded in-process fallback, tenant rate limits and indexing backpressure.
- **RAG:** optional Groq/OpenAI-compatible generation with numbered sources; source excerpts when no LLM is configured. Invalid citation IDs are rejected; factual correctness still requires evaluation.
- **Operations:** health endpoints, tenant statistics, queue lag, structured-safe worker events, streaming corpus importer, benchmark script, local tests and a real-stack integration CI job.

## Run locally in five minutes

Python **3.11+** is required. No Docker, model download, or API key is needed for this mode.

```bash
git clone https://github.com/Sumeetbeginner/Distributed-RAG.git
cd Distributed-RAG
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements-dev.lock
cp .env.example .env                     # Windows: copy .env.example .env
python -m app.cli init
python -m app.cli demo
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** and paste the tenant API key printed by `demo`. That command creates a new workspace and six indexed sample documents each time it runs. The API key is displayed once; the server stores only its SHA-256 digest. The browser keeps the key only in memory, so refresh requires signing in again.

Run the background worker in a second terminal using the same virtual environment and project directory:

```bash
python -m app.worker
```

Try: **“How do workers recover from failures?”** Turn on **Generate answer** to see cited excerpts. Add a document, watch its job become `done`, and search for it.

**Local mode uses SQLite and deterministic hashed token vectors. These vectors are not semantic embeddings.** The implementation has a configurable 20,000-active-chunk scan limit. Use a real embedding provider and OpenSearch for semantic search and larger workloads.

## Run the distributed development cluster

Requirements: Docker Compose v2.24+ and enough memory for PostgreSQL, Redis, three OpenSearch JVMs and the app (allocate roughly 6 GB or more to Docker for development). This configuration is local development infrastructure, not a secure public deployment.

On Linux, OpenSearch may require:

```bash
sudo sysctl -w vm.max_map_count=262144
```

Start services and workers:

```bash
docker compose up --build -d
docker compose exec api python -m app.cli demo
docker compose up -d --scale worker=3 worker
```

Open **http://localhost:8000**. The `init` service runs before API/workers. PostgreSQL and OpenSearch have persistent volumes. Redis caches are disposable. Backend ports are not published; the API binds to host loopback.

```bash
docker compose logs -f worker
docker compose exec api python -m app.cli tenant --name "Second workspace"
docker compose exec api python -m app.cli gc
docker compose down                       # keeps volumes
```

There are **4 primary shards, 1 replica, and 3 cluster-manager-eligible search nodes** by default. Client requests rotate between all three nodes and retry transient transport failures. Shards use default document-ID routing, distributing large tenants across the cluster. Queries fan out to shards with a mandatory tenant filter. This avoids routing an entire large tenant to one shard.

PostgreSQL and Redis in Compose each have one instance. PostgreSQL is still the control-plane availability boundary; production requires a managed HA database or an equivalent failover setup. Redis loss falls back to per-process cache/rate limiting. OpenSearch replicas alone do not make the whole application highly available.

## Enable semantic embeddings and an LLM

Configure the server `.env` before indexing:

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_URL=http://your-embedding-service:8080/v1
EMBEDDING_API_KEY=your-provider-key-if-required
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIMENSIONS=384

LLM_URL=https://api.groq.com/openai/v1
LLM_API_KEY=your-groq-key
LLM_MODEL=your-available-chat-model
```

The embedding service must implement the OpenAI-compatible `POST /embeddings` contract and return the configured dimensions. A self-hosted or hosted compatible embedding endpoint works. Chat and embedding configuration are separate; configuring Groq chat does **not** provide embeddings. Verify model availability in your provider account.

For containers, `localhost` refers to the container. Use a reachable service hostname or the appropriate host gateway. Restart API and workers after configuration changes. Never put provider credentials into client JavaScript or commit `.env`.

OpenSearch index metadata records embedding provider/model/dimension and rejects a mismatch during initialization. Changing embeddings requires a **new `INDEX_NAME` and complete reindex**; the blue/green rollout procedure is in [operations](docs/operations.md). Do not mix vectors from different models.

When LLM generation is enabled, the question and up to eight retrieved chunks are sent to the configured provider. Documents are treated as untrusted evidence, the model has no tools, and citation indices are checked. Citation checking does not prove that an answer is entailed by a source.

## API example

```bash
export DRAG_API_KEY='your-tenant-key'

curl -X POST http://localhost:8000/api/documents \
  -H "Authorization: Bearer $DRAG_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"external_id":"ops/recovery","title":"Recovery runbook","collection":"operations","content":"A worker can reclaim a job after its lease expires. Publication requires its current fencing token."}'

curl -X POST http://localhost:8000/api/search \
  -H "Authorization: Bearer $DRAG_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"How does worker recovery work?","mode":"hybrid","limit":5}'
```

The ingest response is **202 Accepted**, not a promise that indexing has finished. Use `/api/jobs` or the console. An identical payload returns `unchanged: true` and creates no extra job. Failed jobs need a retry or explicit reindex.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/me` | Workspace and runtime capabilities |
| `POST /api/documents` | Incremental upsert |
| `POST /api/documents/batch` | Atomic batch, up to 100 documents |
| `POST /api/documents/upload` | Multipart upload |
| `GET /api/documents` | Keyset-paginated document list |
| `DELETE /api/documents/{id}` | Tombstone immediately, queue cleanup |
| `POST /api/documents/{id}/reindex` | Queue a new revision |
| `POST /api/search` | Hybrid, keyword, or vector retrieval |
| `POST /api/answer` | Evidence-backed answer or excerpts |
| `GET /api/jobs` | Recent jobs, optional state filter |
| `POST /api/jobs/{id}/retry` | Retry the latest dead-letter revision |
| `GET /api/stats`, `/api/audit` | Tenant metrics and recent audit events |
| `GET/POST /api/keys`, `DELETE /api/keys/{id}` | Scoped API-key lifecycle |
| `POST /api/admin/tenants` | Tenant creation using `X-Admin-Key` |
| `GET /health/live`, `/health/ready` | Process and dependency health |

Interactive API documentation: **http://localhost:8000/docs**. Admin HTTP provisioning is disabled unless `ADMIN_KEY` contains at least 24 characters. CLI tenant creation does not require an HTTP admin key.

## Test and benchmark

```bash
python -m pytest -q
ruff check app tests scripts
```

The default suite covers local end-to-end API flows, tenant boundaries, concurrent upserts/claims, crash recovery, fencing, superseded revisions, tombstones, retries, backpressure, key revocation, upload validation, and OpenSearch request/failover contracts. The real-stack test is skipped unless explicitly enabled.

Run integration against an **isolated disposable Compose cluster**, with regular workers stopped so they do not consume the test jobs:

```bash
docker compose up --build -d api
docker compose stop worker
docker compose --profile test run --rm integration
```

GitHub Actions runs unit tests and a real PostgreSQL/OpenSearch/Redis integration test. Never use the integration test against a production queue.

Import a large corpus without loading it all into memory:

```bash
DRAG_API_KEY='...' python scripts/ingest_jsonl.py documents.jsonl
DRAG_API_KEY='...' python scripts/benchmark.py --requests 100 --concurrency 8
```

Each JSONL line is a document payload with a stable `external_id`. Re-running imports is safe for identical data. The importer handles HTTP backpressure; restart it after transport failure. Increase the test workspace's request limit for controlled load tests. Report corpus size, chunks/document, hardware, model, cache state, worker count, p50/p95/p99, errors and relevance metrics alongside any performance claim.

## Architecture and limits

- [Architecture and consistency](docs/architecture.md)
- [Operations, failure drills and scaling](docs/operations.md)
- [Verification record](docs/verification.md)

This initial release does not include SSO, document-level ACLs within a tenant, scheduled SaaS connectors, OCR, a reranker, online resharding orchestration, or a proven million-document benchmark. Tenant separation and API scopes are implemented; those additional enterprise capabilities are future work. Schema initialization creates the initial schema; future schema changes need versioned migrations before deployment.
