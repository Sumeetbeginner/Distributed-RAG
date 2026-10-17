"""Shared chunking, lexical ranking, and reciprocal rank fusion primitives."""

import hashlib
import math
import re
from collections import Counter, defaultdict
import httpx


def tokens(text):
    return re.findall(r"\w+", text.lower(), re.UNICODE)


def chunk_text(text, size, overlap):
    words = text.split()
    chunks = []
    for start in range(0, len(words), size - overlap):
        chunks.append(" ".join(words[start : start + size]))
        if start + size >= len(words):
            break
    return chunks


def bm25(query, rows):
    corpus = [tokens(row["title"] + " " + row["content"]) for row in rows]
    if not corpus:
        return []
    frequencies = [Counter(words) for words in corpus]
    df = Counter(word for counts in frequencies for word in counts)
    avg = sum(map(len, corpus)) / len(corpus) or 1
    ranked = []
    for row, words, counts in zip(rows, corpus, frequencies):
        score = 0.0
        for term in set(tokens(query)):
            tf = counts[term]
            idf = math.log(1 + (len(rows) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(words) / avg))
        if score > 0:
            ranked.append({**row, "raw_score": score})
    return sorted(ranked, key=lambda row: (-row["raw_score"], row["id"]))


def fuse(rankings, limit):
    scores, records, ranks = defaultdict(float), {}, defaultdict(dict)
    for channel, rows in rankings.items():
        for rank, row in enumerate(rows, 1):
            key = row["id"]
            scores[key] += 1 / (60 + rank)
            records[key] = row
            ranks[key][channel] = rank
    return [
        {**records[key], "score": round(scores[key], 7), "ranks": ranks[key]}
        for key in sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    ]


class Embeddings:
    def __init__(self, settings):
        self.settings = settings
        self.client = httpx.Client(timeout=45)

    def embed(self, texts):
        s = self.settings
        if s.embedding_provider == "hash":
            # Reproducible feature hashing for offline plumbing tests, NOT semantic embeddings.
            vectors = []
            for text in texts:
                vector = [0.0] * s.embedding_dimensions
                for word in tokens(text) or [text]:
                    h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big")
                    vector[h % len(vector)] += 1 if h & (1 << 63) else -1
                norm = math.sqrt(sum(x * x for x in vector)) or 1
                vectors.append([x / norm for x in vector])
            return vectors
        vectors = []
        for offset in range(0, len(texts), 32):
            batch = texts[offset : offset + 32]
            headers = {"Authorization": f"Bearer {s.embedding_api_key}"} if s.embedding_api_key else {}
            response = self.client.post(
                s.embedding_url.rstrip("/") + "/embeddings",
                headers=headers,
                json={"model": s.embedding_model, "input": batch},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda row: row["index"])
            if len(data) != len(batch) or [r["index"] for r in data] != list(range(len(batch))):
                raise ValueError("Embedding provider returned invalid batch indices")
            for row in data:
                vector = row["embedding"]
                if len(vector) != s.embedding_dimensions or not all(math.isfinite(x) for x in vector):
                    raise ValueError("Embedding dimensions or values do not match index")
                norm = math.sqrt(sum(x * x for x in vector))
                if norm == 0:
                    raise ValueError("Embedding provider returned a zero vector")
                vectors.append([x / norm for x in vector])
        return vectors
