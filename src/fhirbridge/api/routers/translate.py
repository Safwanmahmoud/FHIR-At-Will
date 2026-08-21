"""Structured-format conversion stubs (AGENTS.md 3, 11.4).

HL7 v2, C-CDA and tabular conversion are explicitly out of scope for v1. The
interface is defined and returns ``501`` so that clients can code against it and
so the shape is settled before an implementation lands.

The reason for the exclusion is worth stating in the response body rather than
only in the docs: these formats have deterministic, well-tested converters
(Microsoft FHIR Converter, matchbox/FML, interface engines). An LLM is the wrong
tool for a format with a published grammar, and using one here would trade
correctness for nothing.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter

from fhirbridge.api.deps import PrincipalDep
from fhirbridge.domain.errors import NotImplementedInV1Error

router = APIRouter(prefix="/v1/translate", tags=["translation"])

_GUIDANCE: Final[dict[str, str]] = {
    "hl7v2": (
        "HL7 v2 to FHIR is deterministic and is not implemented here in v1. Use the "
        "Microsoft FHIR Converter, matchbox/FML, or your interface engine, then send "
        "the resulting bundle to POST /v1/validate."
    ),
    "cda": (
        "C-CDA to FHIR is deterministic and is not implemented here in v1. Use a "
        "C-CDA-on-FHIR mapping engine, then send the resulting bundle to "
        "POST /v1/validate. Narrative sections inside a C-CDA can be sent to "
        "POST /v1/conversions once that endpoint ships."
    ),
    "tabular": (
        "Tabular to FHIR is deterministic and is not implemented here in v1. Map "
        "columns with FML or your ETL tool, then send the resulting bundle to "
        "POST /v1/validate."
    ),
}

_RESPONSES: dict[int | str, dict[str, Any]] = {
    501: {
        "description": (
            "Not implemented in v1 by design. The response names the deterministic "
            "tool that should be used instead."
        )
    }
}


def _stub(fmt: str) -> None:
    raise NotImplementedInV1Error(_GUIDANCE[fmt], safe_context={"format": fmt})


@router.post("/hl7v2", summary="Convert HL7 v2 (not implemented in v1)", responses=_RESPONSES)
async def translate_hl7v2(principal: PrincipalDep) -> None:
    del principal
    _stub("hl7v2")


@router.post("/cda", summary="Convert C-CDA (not implemented in v1)", responses=_RESPONSES)
async def translate_cda(principal: PrincipalDep) -> None:
    del principal
    _stub("cda")


@router.post(
    "/tabular", summary="Convert tabular data (not implemented in v1)", responses=_RESPONSES
)
async def translate_tabular(principal: PrincipalDep) -> None:
    del principal
    _stub("tabular")


__all__ = ["router"]
