"""Fail when a tracked notebook contains persisted execution state."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    dirty: list[str] = []
    for path in Path("notebooks").rglob("*.ipynb"):
        notebook = json.loads(path.read_text(encoding="utf-8"))
        for index, cell in enumerate(notebook.get("cells", [])):
            if cell.get("cell_type") != "code":
                continue
            if cell.get("execution_count") is not None or cell.get("outputs"):
                dirty.append(f"{path}: code cell {index} contains execution state")

    if dirty:
        print("\n".join(dirty))
        print("Clear notebook outputs and execution counts before committing.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
