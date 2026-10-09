"""Test-only access to the independent bundled skill implementations."""

from importlib import import_module


SKILLS = {
    "financial": (
        "financial-statement-analysis",
        "analyze_statements",
        "calculate_financial",
        "FinancialResult",
    ),
    "monitor": ("company-event-monitor", "normalize_events", "normalize_monitor", "MonitorResult"),
    "digest": ("research-report-digest", "digest_reports", "normalize_digest", "DigestResult"),
    "deep": ("deep-investment-report", "forecast", "calculate_deep", "DeepResult"),
}


def module(kind, name):
    plugin = SKILLS[kind][0]
    package = "report-generation" if kind == "deep" else "analysis-modeling"
    return import_module(f"researchx.plugins.bundled.{package}.skills.{plugin}.scripts.{name}")


RESULT_TYPES = {kind: getattr(module(kind, "models"), config[3]) for kind, config in SKILLS.items()}
FUNCTIONS = {kind: getattr(module(kind, config[1]), config[2]) for kind, config in SKILLS.items()}
EXPORTERS = {kind: module(kind, "export_report").export_result for kind in SKILLS}


def export_result(result, *args, **kwargs):
    return EXPORTERS[result.kind](result, *args, **kwargs)
