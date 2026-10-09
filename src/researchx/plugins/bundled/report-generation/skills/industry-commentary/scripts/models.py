"""Contract for industry-commentary."""

from importlib import import_module
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from researchx.plugins.bundled.report_generation.reporting import ReportDocument
else:
    ReportDocument = import_module(
        "researchx.plugins.bundled.report-generation.reporting"
    ).ReportDocument


class ReportResult(ReportDocument):
    kind: Literal["industry-commentary"] = "industry-commentary"
