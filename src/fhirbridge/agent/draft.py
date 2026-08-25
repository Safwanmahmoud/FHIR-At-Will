"""The working FHIR draft the agent edits through tools.

A :class:`DraftState` is a small, deterministic container: a set of resources
keyed by their ``urn:uuid:`` fullUrl, seeded with an empty Patient. The tools own
every fullUrl and every inter-resource reference, so the model never has to
invent or track them — it names the Patient as ``"patient"`` or a prior resource
by the fullUrl a tool handed back, and the draft resolves it.

The draft holds no validation logic of its own. Tools validate a candidate
resource *before* handing it to :meth:`put`; the draft only guarantees that what
it stores serialises to a well-formed collection Bundle.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

PATIENT_ALIAS = "patient"
"""The name the model uses to address the subject Patient in tool arguments."""


def _new_full_url() -> str:
    return f"urn:uuid:{uuid4()}"


@dataclass(slots=True)
class DraftState:
    """The mutable set of resources being assembled into one Bundle."""

    patient_full_url: str
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def new(cls) -> DraftState:
        """Start a draft with an empty Patient as the subject of record."""
        patient_url = _new_full_url()
        state = cls(patient_full_url=patient_url, resources={})
        state.resources[patient_url] = {"resourceType": "Patient"}
        return state

    # --- Reference resolution --------------------------------------------

    def resolve(self, reference: str | None) -> str | None:
        """Map a caller-facing name to a real fullUrl, or ``None`` if unknown.

        ``"patient"`` (or an empty/omitted reference) resolves to the subject
        Patient; a ``urn:uuid:`` value is accepted only if it names a resource
        that actually exists in the draft.
        """
        if reference is None or reference == "" or reference == PATIENT_ALIAS:
            return self.patient_full_url
        if reference in self.resources:
            return reference
        return None

    def patient_reference(self) -> dict[str, str]:
        return {"reference": self.patient_full_url}

    # --- Mutation --------------------------------------------------------

    def get(self, full_url: str) -> dict[str, Any] | None:
        resource = self.resources.get(full_url)
        return copy.deepcopy(resource) if resource is not None else None

    def snapshot(self, full_url: str) -> dict[str, Any] | None:
        """A deep copy tools can mutate freely before deciding to commit."""
        return self.get(full_url)

    def new_resource(self, resource_type: str) -> tuple[str, dict[str, Any]]:
        """A fresh fullUrl and an unregistered skeleton for ``resource_type``.

        The skeleton is *not* stored; the caller validates a fully built version
        and only then calls :meth:`put`.
        """
        return _new_full_url(), {"resourceType": resource_type}

    def put(self, full_url: str, resource: dict[str, Any]) -> None:
        """Commit a validated resource under ``full_url``."""
        self.resources[full_url] = copy.deepcopy(resource)

    # --- Serialisation ---------------------------------------------------

    def to_bundle(self) -> dict[str, Any]:
        """Serialise the draft as a FHIR ``collection`` Bundle.

        The Patient is emitted first; insertion order is otherwise preserved so
        the Bundle reads in the order the note was processed.
        """
        ordered = [self.patient_full_url] + [
            url for url in self.resources if url != self.patient_full_url
        ]
        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"fullUrl": url, "resource": copy.deepcopy(self.resources[url])}
                for url in ordered
                if url in self.resources
            ],
        }

    def summary(self) -> dict[str, int]:
        """Counts by resource type, for a compact tool response."""
        counts: dict[str, int] = {}
        for resource in self.resources.values():
            rtype = str(resource.get("resourceType", "Unknown"))
            counts[rtype] = counts.get(rtype, 0) + 1
        return counts


__all__ = ["PATIENT_ALIAS", "DraftState"]
