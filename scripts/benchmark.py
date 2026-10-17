"""Measure real API latency; no fabricated performance numbers.

DRAG_API_KEY=... python scripts/benchmark.py --requests 100 --concurrency 8
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import statistics
import time
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--mode", choices=["keyword", "hybrid", "vector"], default="hybrid")
    args = parser.parse_args()
    key = os.environ.get("DRAG_API_KEY")
    if not key:
        parser.error("Set DRAG_API_KEY to a test workspace key")
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests and concurrency must be positive")
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {key}"}, timeout=60) as client:

        def query(i):
            start = time.perf_counter()
            response = client.post("/api/search", json={"query": f"recovery {i % 10}", "mode": args.mode})
            return response.status_code, (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            outcomes = list(pool.map(query, range(args.requests)))
        elapsed = time.perf_counter() - start
    latencies = sorted(ms for status, ms in outcomes if status == 200)
    report = {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "elapsed_s": round(elapsed, 2),
        "successful_requests_per_s": round(len(latencies) / elapsed, 2),
        "errors": {
            str(code): sum(s == code for s, _ in outcomes)
            for code in set(s for s, _ in outcomes)
            if code != 200
        },
    }
    if latencies:
        report.update(
            p50_ms=round(statistics.median(latencies), 2),
            p95_ms=round(latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))], 2),
            p99_ms=round(latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))], 2),
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
