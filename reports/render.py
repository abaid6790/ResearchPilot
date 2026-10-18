"""Plain Markdown rendering of a report (citation markers kept as ``[@S<id>]``)."""

from __future__ import annotations

from reports.models import ReportDocument


def to_markdown(report: ReportDocument) -> str:
    parts = [f"# {report.title}"]
    for section in report.sections:
        parts.append(f"{'#' * (section.level + 1)} {section.heading}\n\n{section.body}")
    return "\n\n".join(parts) + "\n"
