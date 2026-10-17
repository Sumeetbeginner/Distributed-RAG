"""Stream a JSONL corpus through bounded API batches, with retry/backpressure.
Each line: {"external_id":"...","title":"...","content":"...","collection":"..."}
"""

import argparse
import json
import os
import time
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    key = os.environ.get("DRAG_API_KEY")
    if not key:
        parser.error("Set DRAG_API_KEY")
    total = 0
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {key}"}, timeout=60) as client:

        def send(batch):
            for attempt in range(9):
                response = client.post("/api/documents/batch", json={"documents": batch})
                if response.status_code not in {429, 502, 503, 504}:
                    response.raise_for_status()
                    return
                time.sleep(min(60, 2**attempt))
            raise RuntimeError("Backpressure did not clear; restart safely using the same external IDs")

        batch, size = [], 0
        with open(args.path, encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                item = json.loads(line)
                length = len(json.dumps(item).encode())
                if batch and (len(batch) >= 50 or size + length > 4_000_000):
                    send(batch)
                    total += len(batch)
                    print(f"Accepted {total} documents", flush=True)
                    batch, size = [], 0
                batch.append(item)
                size += length
            if batch:
                send(batch)
                total += len(batch)
    print(f"Accepted {total} documents. Indexing completes asynchronously.")


if __name__ == "__main__":
    main()
