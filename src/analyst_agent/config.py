"""Runtime configuration — every external dependency is an env-overridable URL.

Three shared services (see documents/implementation.md §3):
  agent_server     LLM presets (judges, reviewer, framing, coverage, classify)
  ingestion-server document parsing (`structural` strategy)
  embeddings       reranker used by dedup + set-level overlap detection
"""
from __future__ import annotations

import os

AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", "http://localhost:7701")
INGESTION_URL = os.environ.get("INGESTION_URL", "http://localhost:8700")
EMBEDDINGS_URL = os.environ.get("EMBEDDINGS_URL", "http://localhost:8601")
RERANK_MODEL_NAME = os.environ.get("RERANK_MODEL_NAME", "bge-reranker")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STORE = os.environ.get("ANALYST_STORE", os.path.join(_ROOT, "store"))
KNOWLEDGE = os.environ.get("ANALYST_KNOWLEDGE", os.path.join(_ROOT, "knowledge"))

# How many LLM requests to have in flight at once. MATCH THE SERVER'S SLOT COUNT:
# agent_server runs llama.cpp with `--parallel 2` and a 64K context, i.e. 2 slots
# of 32K each. Offering more than that does not add throughput — the excess just
# queues, and the tail request waits several generation rounds and can blow the
# client timeout (observed: 180s timeouts under load while 9 judges queued behind
# 2 slots). Raise this only when the server gains slots.
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "2"))

PORT = int(os.environ.get("PORT", "7803"))
