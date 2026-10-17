# Verification record

## Local checks performed

- Python API/worker/retrieval/security tests pass on Python 3.12.
- Ruff lint passes for application, tests and scripts.
- Browser JavaScript passes `node --check`.
- Browser smoke check against a real locally running API and SQLite database completed: workspace login, live statistics, hybrid retrieval, source-excerpt answers, document ingestion, job listing, runtime settings and creation of a read-only API key.
- Browser emitted no JavaScript page errors during those flows.
- Desktop (1440px) and mobile (390px) layouts were inspected; no horizontal viewport overflow was detected at mobile width.

## Distributed verification boundary

Docker is unavailable in the implementation environment, so the real PostgreSQL/OpenSearch/Redis integration test was not run locally. The suite explicitly skips that test unless `RUN_DISTRIBUTED_TESTS=1` is set. A dedicated GitHub Actions job starts the three-node Compose stack and runs it against real services.

OpenSearch request contracts, partial-bulk failure handling, shard-failure rejection and multi-endpoint failover are covered with mocked HTTP transports locally. These are not substitutes for a running-cluster integration test.

No live embedding provider or paid chat API was used. Provider response validation and citation-ID validation are tested with HTTP mocks. Offline source-excerpt answers were tested end to end.

No million-document benchmark or production availability SLA is claimed. Use the included corpus importer, benchmark, and operations failure drills to collect that evidence on target infrastructure.
