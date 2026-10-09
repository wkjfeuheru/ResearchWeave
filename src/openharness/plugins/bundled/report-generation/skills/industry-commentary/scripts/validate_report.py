"""Verify report structure, upstream metrics and evidence gaps without calculating."""

if not __package__:
    from openharness.utils.research_script_support import script_package

    __package__ = script_package(__file__)

from typing import TYPE_CHECKING
from importlib import import_module
from .models import ReportResult
from openharness.utils.research_script_support import processing_main

if TYPE_CHECKING:
    from openharness.plugins.bundled.report_generation.reporting import (
        validate_report as validate_report,
    )
else:
    validate_report = import_module(
        "openharness.plugins.bundled.report-generation.reporting"
    ).validate_report


def main(argv: list[str] | None = None) -> None:
    processing_main(ReportResult, validate_report, argv)


if __name__ == "__main__":
    main()
