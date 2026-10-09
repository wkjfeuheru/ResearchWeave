"""Stratified review packets and auditable judge/human disagreements."""

from __future__ import annotations
from openharness.evaluation.models import EvalCase, RunArtifact

import json
import math
from pathlib import Path

from openharness.utils.fs import atomic_write_text


def calibration_cases(cases: list[EvalCase]) -> list[EvalCase]:
    selected = []
    for category in ("financial", "events", "digest", "deep", "cross"):
        if category in {"financial", "events"}:
            strata = [
                ("synthetic", "basic", 1),
                ("synthetic", "intermediate", 2),
                ("snapshot", "basic", 1),
                ("snapshot", "intermediate", 1),
                ("snapshot", "complex", 1),
                ("live", "basic", 1),
                ("live", "intermediate", 1),
            ]
        elif category in {"digest", "deep"}:
            strata = [
                ("synthetic", "basic", 1),
                ("synthetic", "intermediate", 1),
                ("synthetic", "complex", 1),
                ("snapshot", "basic", 1),
                ("snapshot", "intermediate", 2),
                ("snapshot", "complex", 1),
                ("live", "intermediate", 1),
            ]
        else:
            strata = [
                ("synthetic", "basic", 1),
                ("synthetic", "intermediate", 2),
                ("synthetic", "complex", 1),
                ("snapshot", "basic", 1),
                ("snapshot", "complex", 1),
                ("live", "intermediate", 2),
            ]
        for material, difficulty, count in strata:
            pool = [
                c
                for c in cases
                if (c.category, c.material, c.difficulty) == (category, material, difficulty)
            ]
            # Prefer edge cases without abandoning material/difficulty strata.
            pool.sort(
                key=lambda c: (-len(set(c.tags) & {"recovery", "conflict", "missing_data"}), c.id)
            )
            selected.extend(pool[:count])
    if len(selected) != 40 or len({c.id for c in selected}) != 40:
        raise ValueError("校准样本分层不足")
    return selected


def review_report(directory: str | Path, artifacts: list[RunArtifact]) -> dict[str, object]:
    directory = Path(directory)
    source = directory / "dataset" / "calibration.jsonl"
    if not source.is_file():
        return {"complete": False, "reviewed": 0, "required": 40, "disagreements": []}
    samples = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    reviews_path = directory / "calibration-reviews.jsonl"
    reviews = {}
    if reviews_path.is_file():
        for line in reviews_path.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                reviews[item.get("case_id")] = item
    by_case = {a.case_id: a for a in artifacts}
    packet, disagreements, signatures = [], [], set()
    reviewed = 0
    metrics = (
        "task_success",
        "path_correct",
        "unsupported_statement_rate",
        "citation_correctness",
        "citation_coverage",
    )
    for sample in samples:
        a = by_case.get(sample["case_id"])
        r = reviews.get(sample["case_id"], {})
        scores = {s.name: s.value for s in a.scores if s.name in metrics} if a else {}
        valid = bool(
            a
            and a.judge_results
            and r.get("status") == "reviewed"
            and r.get("reviewer")
            and r.get("basis")
            and r.get("trace_id") == a.trace_id
            and r.get("dataset_version") == a.dataset_version
            and r.get("judge_prompt_hash") == a.provenance.get("judge_prompt_hash")
            and isinstance(r.get("human_scores"), dict)
            and set(metrics) <= r["human_scores"].keys()
            and all(
                value is None
                or (
                    isinstance(value, (int, float))
                    and math.isfinite(value)
                    and 0 <= value <= 1
                    and (name not in {"task_success", "path_correct"} or value in (0, 1))
                )
                for name, value in r["human_scores"].items()
                if name in metrics
            )
        )
        if valid and a is not None:
            reviewed += 1
            signatures.add(
                (
                    a.provenance.get("judge_model"),
                    a.provenance.get("judge_prompt_hash"),
                    a.dataset_version,
                )
            )
            for metric in metrics:
                human, model = r["human_scores"][metric], scores.get(metric)
                if human != model:
                    disagreements.append(
                        {
                            "case_id": a.case_id,
                            "metric": metric,
                            "human": human,
                            "model": model,
                            "basis": r["basis"],
                            "trace_id": a.trace_id,
                        }
                    )
        packet.append(
            {
                **sample,
                "trace_id": a.trace_id if a else None,
                "dataset_version": a.dataset_version if a else None,
                "judge_prompt_hash": a.provenance.get("judge_prompt_hash") if a else None,
                "model_scores": scores,
                "human_scores": r.get("human_scores"),
                "basis": r.get("basis"),
                "reviewer": r.get("reviewer"),
                "status": "reviewed" if valid else "pending",
                "execution_record": f"results/{a.case_id}-r{a.repetition}-{a.run_id}.json"
                if a
                else None,
            }
        )
    complete = len(samples) == 40 and reviewed == 40 and len(signatures) == 1
    if complete:
        signature = next(iter(signatures))
        for a in artifacts:
            a.calibrated = (
                a.provenance.get("judge_model"),
                a.provenance.get("judge_prompt_hash"),
                a.dataset_version,
            ) == signature
    atomic_write_text(
        directory / "calibration-packet.jsonl",
        "\n".join(json.dumps(r, ensure_ascii=False) for r in packet) + "\n",
    )
    return {
        "complete": complete,
        "reviewed": reviewed,
        "required": 40,
        "disagreements": disagreements,
        "pending_cases": [r["case_id"] for r in packet if r["status"] == "pending"],
    }
