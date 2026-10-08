"""Read and audit complete cases without running a model or uploading data."""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from collections import Counter, defaultdict
from pathlib import Path

from openharness.evaluation.models import EvalCase

_SOURCE_DATASET = Path(__file__).resolve().parents[3] / "evals" / "research_v1"
DEFAULT_DATASET = (
    _SOURCE_DATASET if _SOURCE_DATASET.is_dir() else Path.cwd() / "evals" / "research_v1"
)


def load_cases(directory=DEFAULT_DATASET):
    directory = Path(directory).resolve()
    return [
        EvalCase.model_validate(json.loads(line))
        for line in (directory / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def asset_path(directory, asset):
    root = Path(directory).resolve()
    candidate = (root / asset.path).resolve()
    if not candidate.is_relative_to(root / "assets") or not candidate.is_file():
        raise ValueError("素材路径必须位于数据集 assets 目录")
    return candidate


def dataset_version(cases):
    content = "\n".join(case.model_dump_json() for case in cases)
    return "research-v1-" + hashlib.sha256(content.encode()).hexdigest()[:16]


def directory_version(directory, cases):
    """A frozen run keeps its original schema version while optional fields evolve."""
    root = Path(directory)
    manifest = root / "manifest.json"
    if manifest.is_file():
        saved = json.loads(manifest.read_text())
        if saved["cases_sha256"] != hashlib.sha256((root / "cases.jsonl").read_bytes()).hexdigest():
            raise ValueError("归档任务集内容已经变更")
        return saved["version"]
    return dataset_version(cases)


def validate_dataset(directory=DEFAULT_DATASET):
    try:
        cases = load_cases(directory)
    except (OSError, ValueError):
        return {
            "valid": False,
            "errors": ["任务集不存在或结构标注无效"],
            "count": 0,
            "version": None,
        }
    errors = []
    if len(cases) != 200 or len({c.id for c in cases}) != 200:
        errors.append("必须包含 200 条唯一任务")
    categories = Counter(c.category for c in cases)
    for category in ("financial", "events", "digest", "deep", "cross"):
        group = [c for c in cases if c.category == category]
        if len(group) != 40 or Counter(c.material for c in group) != {
            "synthetic": 16,
            "snapshot": 16,
            "live": 8,
        }:
            errors.append(f"{category} 的任务或素材分布不正确")
        if Counter(c.difficulty for c in group) != {"basic": 12, "intermediate": 20, "complex": 8}:
            errors.append(f"{category} 的难度分布不正确")
    if Counter((c.environment, c.split) for c in cases) != {
        ("fixed", "dev"): 112,
        ("fixed", "holdout"): 48,
        ("live", "dev"): 28,
        ("live", "holdout"): 12,
    }:
        errors.append("开发/保留测试集分布不正确")
    tags = Counter(tag for c in cases for tag in set(c.tags))
    for name, minimum in (("conflict", 40), ("missing_data", 20), ("recovery", 20)):
        if tags[name] < minimum:
            errors.append(f"{name} 覆盖不足 {minimum} 条")
    for kind in ("scope", "fact", "calculation", "interpretation"):
        if sum(c.path.conflict_kind == kind for c in cases) < 1:
            errors.append(f"没有覆盖 {kind} 冲突")
    splits = defaultdict(set)
    inputs = Counter()
    for case in cases:
        splits[case.family].add(case.split)
        inputs[json.dumps(case.agent_input()["turns"], ensure_ascii=False)] += 1
        for asset in case.assets:
            try:
                path = asset_path(directory, asset)
                if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
                    errors.append(f"{case.id} 素材哈希不匹配")
            except ValueError:
                errors.append(f"{case.id} 素材路径无效")
            splits[asset.family].add(case.split)
            splits[asset.sha256].add(case.split)
        if not case.annotation_basis.strip() or not case.reference_facts:
            errors.append(f"{case.id} 标注不完整")
        if not (Path(directory) / "manifest.json").is_file() and (
            set(case.reference_locations) != set(case.reference_facts)
            or any(not value for value in case.reference_locations.values())
            or any(not (r.source_ids or r.source_locators) for r in case.requirements)
        ):
            errors.append(f"{case.id} 参考事实或验收项缺少来源定位")
        for requirement in case.requirements:
            if requirement.check == "numeric":
                try:
                    if (
                        not Decimal(requirement.value).is_finite()
                        or Decimal(requirement.atol) < 0
                        or Decimal(requirement.rtol) < 0
                        or not requirement.unit
                        or not requirement.period
                        or not (
                            requirement.source_ids
                            or case.environment == "live"
                            and requirement.source_locators
                        )
                    ):
                        errors.append(f"{case.id} 数值金标缺少有限值、来源、单位、期间或有效容限")
                except (InvalidOperation, TypeError):
                    errors.append(f"{case.id} 数值金标无效")
        if case.path.conflict == "reopen" and not any(
            a.available_from_turn > 0 for a in case.assets
        ):
            # Historical archived corpora are still readable, but are not accepted as newly authored v1.
            if not (Path(directory) / "manifest.json").is_file():
                errors.append(f"{case.id} 重开任务缺少后续新增材料")
        if any(term in case.title for term in ("TODO", "待填写", "占位")):
            errors.append(f"{case.id} 包含占位标注")
    if any(len(s) > 1 for s in splits.values()):
        errors.append("资料族或素材跨集合泄漏")
    if any(count > 1 for count in inputs.values()):
        errors.append("重复任务输入")
    calibration_path = Path(directory) / "calibration.jsonl"
    if not calibration_path.is_file():
        errors.append("缺少人工校准样本")
    else:
        calibration = [
            json.loads(line) for line in calibration_path.read_text().splitlines() if line.strip()
        ]
        if (
            len(calibration) != 40
            or len({c["case_id"] for c in calibration}) != 40
            or not {c["case_id"] for c in calibration} <= {c.id for c in cases}
        ):
            errors.append("校准样本必须覆盖40条不同任务")
    return {
        "valid": not errors,
        "errors": errors,
        "count": len(cases),
        "version": directory_version(directory, cases),
        "categories": dict(categories),
        "tags": dict(tags),
        "materials": dict(Counter(c.material for c in cases)),
    }
