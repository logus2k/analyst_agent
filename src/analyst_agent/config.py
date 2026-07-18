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

PORT = int(os.environ.get("PORT", "7803"))
