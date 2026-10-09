"""Standalone corpus tooling is checked without touching the frozen corpus."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def test_authoring_synthetic_cases_preserve_fixture_contract(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2] / "evals/research_v1"
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location(
        "researchx_test_authoring", root / "build_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    (tmp_path / "assets").mkdir()
    for category in ("financial", "events", "digest", "deep", "cross"):
        for index in range(16):
            identifier = f"{category}-syn-{index + 1:02d}"
            assets, values, gaps, kind = module.synthetic(category, index, identifier)
            asset = assets[0]
            payload = json.loads((tmp_path / asset.path).read_text())
            assert payload["synthetic"] and payload["case"] == identifier
            assert payload["declared_gaps"] == gaps if gaps else "declared_gaps" not in payload
            assert values["revenue"] == str(100 + index * 7)
            assert (kind is None) == ("disagreement" not in payload)
            assert asset.provenance == "synthetic"
    assert len(list((tmp_path / "assets").glob("*.json"))) == 80


def test_authoring_cli_help_from_arbitrary_directory(tmp_path):
    script = Path(__file__).resolve().parents[2] / "evals/research_v1/build_dataset.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--pdf-dir" in result.stdout
