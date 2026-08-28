"""The tool-driven narrative-to-FHIR agent (``POST /v1/craft``).

Where ``/v1/NAR2FHIR`` uses a grounded extraction and assembly pipeline, this
package inverts the trust: the model never emits FHIR directly.
It drives a :class:`~fhirbridge.agent.draft.DraftState` through a fixed set of
deterministic tools (:mod:`fhirbridge.agent.tools`), each of which re-validates
its change against the typed models and the terminology server and refuses to
commit anything that does not conform. The model chooses *what* to assert; the
tools guarantee *validity*. The full validation cascade remains the authority on
the assembled result.
"""

from __future__ import annotations

from fhirbridge.agent.draft import DraftState
from fhirbridge.agent.loop import CraftAgent, CraftResult
from fhirbridge.agent.tools import TOOL_SCHEMAS, ToolContext, ToolOutcome, dispatch_tool

__all__ = [
    "TOOL_SCHEMAS",
    "CraftAgent",
    "CraftResult",
    "DraftState",
    "ToolContext",
    "ToolOutcome",
    "dispatch_tool",
]
