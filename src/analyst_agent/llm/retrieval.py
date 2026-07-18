"""Embedding + reranking access via embeddings_server (torch GPU service).

Embeddings (bge-m3) and reranking (bge-reranker) are served by embeddings_server
at EMBEDDINGS_URL: the same llama.cpp-style `/v1/rerank` contract, and `/embed`
(returns {"vectors": [...]}, already L2-normalized). Migrated off llama-server
when the reranker moved to the consolidated GPU service.

For overview-dedup we only need the reranker: raw cosine similarity does not
separate a summary from a merely-related requirement in an SRS (everything is
topically similar), whereas the reranker scores true subsumption ~0.95 vs <0.05.
"""

from __future__ import annotations

import math
import os

import httpx

# Embeddings + reranking now served by embeddings_server (torch GPU service),
# not llama-server. Same /v1/rerank contract; /embed returns {"vectors": [...]}
# (already L2-normalized). reqoach uses host networking -> localhost:8601.
EMBEDDINGS_URL = os.environ.get("EMBEDDINGS_URL", "http://localhost:8601")
RERANK_MODEL = os.environ.get("RERANK_MODEL_NAME", "bge-reranker")


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def rerank(query: str, documents: list[str], timeout: float = 60.0) -> list[float]:
    """Return one 0-1 relevance score per document, in INPUT order.

    llama-server returns raw logits with an `index` per result; we sigmoid them
    (same as noted-rag) so a fixed threshold is meaningful.
    """
    if not documents:
        return []
    r = httpx.post(
        f"{EMBEDDINGS_URL}/v1/rerank",
        json={"model": RERANK_MODEL, "query": query, "documents": documents},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    results = body.get("results") or body.get("data") or []
    scored = [0.0] * len(documents)
    for item in results:
        idx = int(item.get("index", 0))
        raw = item.get("relevance_score", item.get("score", 0.0))
        if 0 <= idx < len(scored):
            scored[idx] = _sigmoid(float(raw))
    return scored


def embed(texts: list[str], timeout: float = 120.0) -> list[list[float]]:
    """L2-normalized dense embeddings (bge-m3). Kept for future cross-document
    work; overview-dedup uses rerank(), not this."""
    if not texts:
        return []
    r = httpx.post(
        f"{EMBEDDINGS_URL}/embed",
        json={"texts": list(texts), "dense": True, "sparse": False},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("vectors", [])  # embeddings_server returns L2-normalized
