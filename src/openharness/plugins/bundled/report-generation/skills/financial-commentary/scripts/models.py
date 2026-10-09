"""Contract for financial-commentary."""

from importlib import import_module
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from openharness.plugins.bundled.report_generation.reporting import ReportDocument
else:
    ReportDocument = import_module(
        "openharness.plugins.bundled.report-generation.reporting"
    ).ReportDocument


class ReportResult(ReportDocument):
    kind: Literal["financial-commentary"] = "financial-commentary"
