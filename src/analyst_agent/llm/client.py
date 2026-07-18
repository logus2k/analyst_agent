"""Thin client for agent_server's OpenAI-compatible chat endpoint.

Calls an agent preset by name (the documented "A1" pattern): we send only the
user content; the preset supplies the system prompt and sampling. `response_format`
is passed at request time (verified honored through the preset path) so JSON comes
back clean; a fence-stripping fallback covers the rare case it doesn't.
"""

from __future__ import annotations

import json
import os
import re

import httpx

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class AgentServerClient:
    def __init__(self, base_url: str | None = None, timeout: float = 180.0):
        self.base_url = (base_url or os.environ.get("AGENT_SERVER_URL", "http://localhost:7701")).rstrip("/")
        self.timeout = timeout

    def complete_json(self, agent: str, user_content: str) -> dict:
        """Call `agent` (a preset name) with `user_content`, expecting a JSON
        object back. Returns the parsed dict. Raises LLMError on transport
        failure or unparseable output."""
        payload = {
            "model": agent,
            "messages": [{"role": "user", "content": user_content}],
            "response_format": {"type": "json_object"},
        }
        try:
            r = httpx.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"agent_server request failed: {e}") from e
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
        return self._parse_json(content)

    @staticmethod
    def _parse_json(content: str) -> dict:
        content = content.strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # Fence-stripping fallback (```json ... ```), then a brace-slice fallback.
        stripped = _FENCE_RE.sub("", content).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start, end = stripped.find("{"), stripped.rfind("}")
            if 0 <= start < end:
                try:
                    return json.loads(stripped[start:end + 1])
                except json.JSONDecodeError:
                    pass
        raise LLMError(f"could not parse JSON from model output: {content[:200]!r}")
