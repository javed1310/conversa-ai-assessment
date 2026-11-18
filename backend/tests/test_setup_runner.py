"""Reviewer setup must select a compatible installed runtime without changing policy."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[2] / "scripts/uv.py"
    spec = importlib.util.spec_from_file_location("setup_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("version,expected", [([3, 12], "installed-python"), ([3, 13], None)])
def test_probe_rejects_incompatible_default(runner, monkeypatch, version, expected):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, json.dumps([version, "installed-python"])
        ),
    )
    assert runner.compatible_python(["python"]) == expected


def test_installed_launcher_is_used_when_path_default_is_incompatible(runner, monkeypatch):
    monkeypatch.delenv("UV_PYTHON", raising=False)
    monkeypatch.setattr(
        runner,
        "compatible_python",
        lambda command: "python312" if command == ["py", "-3.12"] else None,
    )
    assert runner.select_python() == "python312"


def test_explicit_runtime_is_respected(runner, monkeypatch):
    monkeypatch.setenv("UV_PYTHON", "chosen-python")
    monkeypatch.setattr(
        runner,
        "compatible_python",
        lambda command: "chosen-python" if command == ["chosen-python"] else None,
    )
    assert runner.select_python() == "chosen-python"


def test_invalid_explicit_runtime_does_not_silently_switch(runner, monkeypatch):
    monkeypatch.setenv("UV_PYTHON", "incompatible-python")
    monkeypatch.setattr(runner, "compatible_python", lambda command: None)
    with pytest.raises(RuntimeError, match="UV_PYTHON must identify"):
        runner.select_python()


def test_missing_runtime_has_actionable_error(runner, monkeypatch):
    monkeypatch.delenv("UV_PYTHON", raising=False)
    monkeypatch.setattr(runner, "compatible_python", lambda command: None)
    with pytest.raises(RuntimeError, match="No runtime was downloaded or replaced"):
        runner.select_python()
