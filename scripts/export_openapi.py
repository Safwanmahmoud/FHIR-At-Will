"""Regenerate the committed OpenAPI contract without network access."""

from __future__ import annotations

import json
from pathlib import Path

from fhirbridge.api.app import create_app
from fhirbridge.config import Settings

OUTPUT = Path("tests/contract/openapi.snapshot.json")


def main() -> None:
    settings = Settings.model_validate(
        {
            "FHIRBRIDGE_ENV": "development",
            "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/fhirbridge",
            "REDIS_URL": "redis://localhost:6379/0",
            "VALIDATOR_URL": "https://validator.invalid",
            "TERMINOLOGY_URL": "https://terminology.invalid/fhir",
            "VALIDATOR_VERSION": "6.10.2",
            "LLM_EGRESS_ALLOWLIST": "",
        }
    )
    document = create_app(settings).openapi()
    OUTPUT.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
