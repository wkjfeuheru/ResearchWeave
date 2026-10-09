"""Export a checked report using this Skill's template."""

if not __package__:
    from researchx.plugins.research_script_support import script_package

    __package__ = script_package(__file__)

from typing import TYPE_CHECKING
from importlib import import_module
from pathlib import Path
from .models import ReportResult
from researchx.plugins.research_script_support import export_main

if TYPE_CHECKING:
    from researchx.plugins.bundled.report_generation.reporting import export_report as _export
else:
    _export = import_module("researchx.plugins.bundled.report-generation.reporting").export_report


def export_result(
    result: ReportResult,
    directory: Path,
    session_directory: Path | None = None,
    task_id: str | None = None,
) -> dict[str, object]:
    return _export(
        result,
        directory,
        session_directory,
        task_id,
        template=Path(__file__).parents[1] / "templates/report.md",
    )


def main(argv: list[str] | None = None) -> None:
    export_main(ReportResult, export_result, argv)


if __name__ == "__main__":
    main()
