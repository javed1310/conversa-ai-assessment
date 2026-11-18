"""Run the same deterministic verification gate on Windows, Linux or macOS.

Run with the installed backend environment's Python. No dependencies are installed,
no services are started, and no live model calls are made by this runner.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-frontend-build", action="store_true")
    parser.add_argument("--backend-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    backend = root / "backend"
    commands = [
        ["ruff", "check", "app", "tests"],
        ["ruff", "format", "--check", "app", "tests"],
        ["mypy", "app"],
        ["pytest", "-q"],
        ["app.export_contracts", "--check"],
    ]
    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    if not args.backend_only and npm is None:
        parser.error("npm is required for frontend checks")
    try:
        for command in commands:
            print("Checking:", " ".join(command), flush=True)
            subprocess.run([sys.executable, "-m", *command], cwd=backend, check=True)
        if not args.backend_only:
            scripts = ["lint", "typecheck", "test"]
            if not args.skip_frontend_build:
                scripts.append("build")
            for script in scripts:
                subprocess.run(
                    [str(npm), "--prefix", str(root / "frontend"), "run", script],
                    cwd=root,
                    check=True,
                )
    except subprocess.CalledProcessError as error:
        return error.returncode
    print("PASS: all selected checks. Live model behavior is evaluated separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
