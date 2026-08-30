"""BYOK/BYOM LLM provider gateway (AGENTS.md 7).

The public surface is the :class:`~fhirbridge.llm.gateway.LlmGateway`, which
enforces provider, egress, PHI-acknowledgement, qualification and budget policy
before any model call, and :class:`~fhirbridge.llm.invocation.LlmInvocation`, the
per-request BYOK context parsed from ``X-LLM-*`` headers.
"""

from __future__ import annotations

from fhirbridge.llm.gateway import LlmGateway, LlmResult
from fhirbridge.llm.invocation import LlmInvocation

__all__ = [
    "LlmGateway",
    "LlmInvocation",
    "LlmResult",
]
