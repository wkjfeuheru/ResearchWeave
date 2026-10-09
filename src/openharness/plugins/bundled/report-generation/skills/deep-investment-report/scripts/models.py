"""Compatibility imports for the original deep-report data contract."""

from importlib import import_module

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openharness.plugins.bundled.analysis_modeling.skills.earnings_forecast.scripts.models import (
        Assumption as Assumption,
        ForecastYear as ForecastYear,
        Scenario as Scenario,
        ReportSection as ReportSection,
        DeepResult as DeepResult,
    )
else:
    _models = import_module(
        "openharness.plugins.bundled.analysis-modeling.skills.earnings-forecast.scripts.models"
    )
    Assumption = _models.Assumption
    ForecastYear = _models.ForecastYear
    Scenario = _models.Scenario
    ReportSection = _models.ReportSection
    DeepResult = _models.DeepResult
