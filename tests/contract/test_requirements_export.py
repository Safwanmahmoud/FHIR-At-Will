"""The exported requirements files must still match ``uv.lock``.

``requirements*.txt`` are generated artifacts, and a generated artifact that can
drift from its source is worse than no artifact: someone installing from a stale
export gets a different dependency set than CI resolved, with hashes that look
authoritative because they are. This test makes the drift loud instead.

It shells out to ``uv export``, which reads ``uv.lock`` and touches no network.
If ``uv`` is not on PATH the test skips rather than fails, so a contributor
installing from the very file under test is not blocked by it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]

# filename -> the group selection it was exported with
EXPORTS: dict[str, tuple[str, ...]] = {
    "requirements.txt": ("--no-dev",),
    "requirements-dev.txt": ("--only-group", "dev"),
    "requirements-notebook.txt": ("--only-group", "notebook"),
}


@pytest.mark.parametrize(("filename", "selection"), sorted(EXPORTS.items()))
def test_the_export_matches_the_lockfile(filename: str, selection: tuple[str, ...]) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed; cannot regenerate the export to compare against")

    committed = ROOT / filename
    assert committed.is_file(), f"{filename} is missing. Regenerate it with uv export."

    result = subprocess.run(  # noqa: S603
        [
            uv,
            "export",
            "--format",
            "requirements.txt",
            *selection,
            "--no-emit-project",
            "--frozen",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    # uv writes the invoking command into a header comment, which differs between
    # writing to a file and writing to stdout. Compare the requirements themselves.
    def requirements(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith("#")]

    assert requirements(result.stdout) == requirements(committed.read_text(encoding="utf-8")), (
        f"{filename} is stale. Regenerate it:\n"
        f"  uv export --format requirements.txt {' '.join(selection)} "
        f"--no-emit-project -o {filename}"
    )
