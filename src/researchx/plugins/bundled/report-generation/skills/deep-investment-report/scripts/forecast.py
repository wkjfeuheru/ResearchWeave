"""Legacy deep-report CLI adapter; new workflows call earnings-forecast via SkillTool."""

from __future__ import annotations

if not __package__:
    from researchx.plugins.research_script_support import script_package

    __package__ = script_package(__file__)

from typing import TYPE_CHECKING
from importlib import import_module
from .models import DeepResult
from researchx.plugins.research_script_support import processing_main

if TYPE_CHECKING:
    from researchx.plugins.bundled.analysis_modeling.skills.earnings_forecast.scripts import (
        forecast as _forecast,
    )
else:
    _forecast = import_module(
        "researchx.plugins.bundled.analysis-modeling.skills.earnings-forecast.scripts.forecast"
    )
project_year = _forecast.project_year
REQUIRED_ASSUMPTIONS = _forecast.REQUIRED_ASSUMPTIONS
SECTION_KEYS = _forecast.SECTION_KEYS


def calculate_deep(result: DeepResult) -> DeepResult:
    """Keep old report validation and forecast behavior for existing callers."""
    keys = [section.key for section in result.sections]
    if len(keys) != len(set(keys)):
        raise ValueError("报告章节不可重复")
    result.gaps.extend(f"缺少报告章节: {key}" for key in sorted(SECTION_KEYS - set(keys)))
    result.gaps.extend(gap for section in result.sections for gap in section.gaps)
    for section in result.sections:
        if not section.paragraphs:
            result.gaps.append(f"{section.title}: 章节缺少有依据的内容")
    return _forecast.calculate_forecast(result)


def main(argv: list[str] | None = None) -> None:
    processing_main(DeepResult, calculate_deep, argv)


if __name__ == "__main__":
    main()
