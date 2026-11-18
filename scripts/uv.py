"""Run pinned workspace uv using an installed Python 3.12, independently of PATH order."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compatible_python(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [
                *command,
                "-c",
                "import json,sys;print(json.dumps([sys.version_info[:2],sys.executable]))",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        version, executable = json.loads(result.stdout.strip())
        return str(executable) if version == [3, 12] else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def select_python() -> str:
    explicit = os.environ.get("UV_PYTHON")
    if explicit:
        executable = compatible_python([explicit])
        if executable:
            return executable
        raise RuntimeError(
            "UV_PYTHON must identify an installed Python 3.12 executable. "
            "Correct that setting or unset it to use automatic installed-interpreter discovery."
        )
    candidates = [
        [str(ROOT / "backend/.venv-system/Scripts/python.exe")],
        [str(ROOT / "backend/.venv/bin/python")],
        ["py", "-3.12"],
        ["python3.12"],
        [sys.executable],
        ["python"],
        ["python3"],
    ]
    for candidate in candidates:
        executable = compatible_python(candidate)
        if executable:
            return executable
    raise RuntimeError(
        "Python 3.12 was not found. Install Python 3.12 alongside your current Python, "
        "or set UV_PYTHON to its executable path. No runtime was downloaded or replaced."
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/uv.py <uv arguments>", file=sys.stderr)
        return 2
    binaries = [ROOT / ".tools/uv/bin/uv.exe", ROOT / ".tools/uv/bin/uv"]
    uv = next((str(path) for path in binaries if path.is_file()), None) or shutil.which("uv")
    if uv is None:
        print("uv is missing. Follow the bootstrap command in README.md.", file=sys.stderr)
        return 2
    try:
        python = select_python()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    env = dict(os.environ)
    env.update(
        UV_CACHE_DIR=str(ROOT / ".cache/uv"),
        UV_PROJECT_ENVIRONMENT=str(ROOT / "backend/.venv-system"),
        UV_PYTHON=python,
        UV_PYTHON_DOWNLOADS="never",
        UV_LINK_MODE="copy",
    )
    return subprocess.run(
        [uv, *sys.argv[1:]], cwd=ROOT / "backend", env=env, check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
