"""Check local Markdown links and canonical repository references."""

from __future__ import annotations

import re
from pathlib import Path

MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
CANONICAL_REPOSITORY = "https://github.com/Safwanmahmoud/FHIR-It-Will"


def main() -> int:
    problems: list[str] = []
    markdown_files = [Path("README.md"), *Path("docs").rglob("*.md")]

    for document in markdown_files:
        text = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (document.parent / relative).resolve().exists():
                problems.append(f"{document}: missing local link target {target}")

    identity_files = (
        Path("pyproject.toml"),
        Path("docker/api/Dockerfile"),
        Path("src/fhirbridge/api/openapi.py"),
        Path("src/fhirbridge/api/errors.py"),
    )
    for path in identity_files:
        if CANONICAL_REPOSITORY not in path.read_text(encoding="utf-8"):
            problems.append(f"{path}: canonical repository URL is missing")

    if problems:
        print("\n".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
