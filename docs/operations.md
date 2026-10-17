# Operations

## Configuration

Copy `.env.example` for local development. Settings are parsed and validated by Pydantic; invalid overlap, backend and embedding values fail fast. Compose overrides the database/search/cache addresses. Model and chat settings come from the environment file. Do not commit credentials.

`python -m app.cli init` creates the initial SQL schema and OpenSearch index. Run it once as a release step before starting API/workers. It is safe to rerun for an unchanged initial schema but is **not** a schema migration engine. Deploy future schema changes with reviewed, versioned migrations.

## Scaling workers and API

Workers are independent long-lived processes. Increase `docker compose up -d --scale worker=N worker` gradually while watching database connections, queue lag, embedding provider quotas and index rejection rates. Worker pool capacity is not provider capacity; excess concurrency can make backlogs worse.

Multiple API instances can share the database/index/cache behind a load balancer. The bundled Compose API has a fixed host port, so remove that mapping and add a load balancer before scaling it with Compose. Same-tenant mutations serialize on the tenant row; batch writes amortize that cost. Configure connection pools to fit the database connection budget.

## Recovery procedures

1. Inspect `/api/jobs?state=dead`, queue lag and worker logs. Logs include job IDs and exception classes, not document payloads or provider response bodies.
2. Fix the unavailable service or invalid embedding dimensions. Confirm `/health/ready`.
3. Retry the latest dead job using the console or `POST /api/jobs/{id}/retry`. Older superseded revisions cannot be retried. Read-only keys cannot retry jobs.
4. Run `python -m app.cli gc` periodically for old index revisions. It scans canonical documents in bounded keyset batches. Run it only after initialization and with the same index configuration as the active deployment.
5. Back up PostgreSQL and snapshot OpenSearch. Restore PostgreSQL first; rebuild projections from canonical documents if index snapshots are unavailable.

Retention is explicit: this release retains job and audit history and document tombstones in PostgreSQL. Add a policy-driven scheduled archival/deletion job before large production runs. Coordinate tombstone retention with maximum in-flight work and index cleanup so stale work cannot reappear. A deletion response removes search visibility; it is not a guarantee of physical erasure from old index segments, replicas, logs, snapshots or backups.

## Failure drills (disposable local cluster)

- **Worker crash:** submit a large document, stop a worker mid-job, wait past the configured lease, and run another worker. Assert exactly one active revision and a terminal job state.
- **Stale publication:** the automated test expires a lease, reclaims it, and proves the old token cannot activate the revision.
- **Search node loss:** stop one `search-*` service while running repeated keyword queries. The client should use surviving endpoints. Allow time for shard promotion/election. Restart the node afterward.
- **Embedding outage:** configure an unavailable endpoint in a disposable workspace. Observe retries and eventual `dead`. Restore the endpoint and retry.
- **Redis outage:** stop Redis. Search still works; cache and rate limits become per-process. Bring Redis back and check recovery.
- **Database outage:** reads that require tenant authorization and writes return service errors. Restore the database; workers retry their claim loop without silently losing accepted jobs.

Never run these drills against production without a reviewed procedure.

## Embedding migration / index rebuild

An embedding change can alter dimensionality, semantics and retrieval behavior. It is not a normal document update.

1. Provision a separate deployment/database copy for the new index. Keep the current deployment available.
2. Set a new `INDEX_NAME` plus the new embedding model/provider/dimension and run `init`.
3. Reindex all live canonical documents in that deployment using paginated document listing and the reindex endpoint, or replay the source JSONL corpus with the same external IDs. Merely changing the index name does not enqueue documents.
4. Wait for all new jobs, evaluate relevance and tenant isolation, and compare representative query results.
5. Pause writes or replay changes since the copy, then route clients to the new deployment. Preserve API key/tenant data if clients must keep credentials.
6. Keep the previous deployment for rollback until the retention period ends.

Online dual-write alias switching and automatic index migration are not implemented. Do not switch a live API to an empty new index while assuming its old `active_revision` pointers refer to populated data.

## Million-document evaluation

Define workload before tuning: document count, average bytes, chunks/document, tenant skew, update/delete rate, embedding model and dimension, query concurrency, keyword/vector mix, and relevance judgments.

- Stream the corpus with `scripts/ingest_jsonl.py` using a test tenant. Ingestion is asynchronous and backpressured.
- Record accepted documents/sec and indexed chunks/sec separately. Measure end-to-end indexing delay and dead-letter count.
- Use multiple tenants; deliberately search for another tenant's unique sentinel text.
- Benchmark warm cache and cache misses separately. The included benchmark repeats ten query variants and therefore exercises warm-cache traffic after initial requests.
- Record p50/p95/p99, HTTP errors, query recall@k, nDCG@k, duplicate suppression, freshness delay and recovery time.
- Estimate vector memory, HNSW overhead, replicas, source text and database history. Do not infer capacity from document count alone.
- Repeat worker/node failure drills during load. Publish measured hardware/configuration and raw results with claims.

## Production gaps to close

Use a managed HA PostgreSQL topology; secure OpenSearch with TLS/auth and private networking; select a production-grade cache/gateway limiter; isolate PDF extraction; add versioned migrations, metrics export/traces, retention/backup jobs, SSO and document ACLs if required. The included three-node search topology is a development demonstration, not a production sizing recommendation.
