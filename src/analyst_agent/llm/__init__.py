"""Local LLM access via agent_server (see local-llm-agent-server memory)."""

from analyst_agent.llm.client import AgentServerClient, LLMError

__all__ = ["AgentServerClient", "LLMError"]
