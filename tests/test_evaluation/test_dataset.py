"""Corpus contracts: count, lineage, distinct inputs, independently checked gold."""

import json
from collections import Counter
from decimal import Decimal

from typer.testing import CliRunner

from researchx.cli import app
from researchx.evaluation.cli import select_cases
from researchx.evaluation.dataset import DEFAULT_DATASET, load_cases, validate_dataset


def test_corpus_is_complete_and_balanced():
    result = validate_dataset()
    assert result["valid"], result["errors"]
    assert result["count"] == 200
    assert result["materials"] == {"synthetic": 80, "snapshot": 80, "live": 40}
    cases = load_cases()
    assert Counter(c.split for c in cases) == {"dev": 140, "holdout": 60}
    assert Counter(c.difficulty for c in cases) == {"basic": 60, "intermediate": 100, "complex": 40}
    assert len(select_cases(cases, representative=True)) == 20
    assert all(c.requirements and c.reference_facts and c.annotation_basis for c in cases)
    calibration = [
        json.loads(line)
        for line in (DEFAULT_DATASET / "calibration.jsonl").read_text().splitlines()
    ]
    assert len(calibration) == 40 and len({item["case_id"] for item in calibration}) == 40
    assert all(item["status"] == "pending" for item in calibration)


def test_numeric_gold_does_not_depend_on_skill_implementation():
    cases = {c.id: c for c in load_cases()}
    case = cases["financial-syn-01"]
    primary = json.loads((DEFAULT_DATASET / case.assets[0].path).read_text())
    expected = (
        (Decimal(primary["revenue"]) - Decimal(primary["cost"])) / Decimal(primary["revenue"]) * 100
    )
    assert Decimal(next(r.value for r in case.requirements if r.id == "numeric")) == expected
    case = cases["financial-real-01"]
    assert next(r.unit for r in case.requirements if r.id == "numeric") == "亿元"


def test_gold_projection_and_cli_have_no_network(monkeypatch):
    case = load_cases()[0]
    public = case.agent_input()
    assert "requirements" not in public and "reference_facts" not in public
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Static validation made a network request")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = CliRunner().invoke(app, ["eval", "validate"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["valid"]
