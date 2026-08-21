"""fhirbridge — verification-first, BYOK narrative-to-FHIR conversion service."""

from importlib import metadata

try:
    __version__ = metadata.version("fhirbridge")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
