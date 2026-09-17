"""Findings and CI status.md rendering per FORMAT.md §7-8."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    severity: str  # error | warn | info
    code: str
    file: str
    message: str


@dataclass
class Report:
    changes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add_change(self, text: str) -> None:
        self.changes.append(text)

    def add(self, severity: str, code: str, file: str, message: str) -> None:
        self.findings.append(Finding(severity, code, file, message))

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


def render_status(report: Report, today: str, run_label: str | None = None) -> str:
    """`run_label` is the human timestamp after 'Run:'; defaults to '<today> 00:00'
    (the reproducible form fixtures rely on) when not given explicitly."""
    if run_label is None:
        run_label = f"{today} 00:00"
    lines = ["---", "gtd: ci-status", "---", "# CI status", "", f"Run: {run_label} (today = {today})", ""]

    lines.append("## Changes")
    lines.append("")
    lines += [f"- {c}" for c in report.changes] or []
    if not report.changes:
        lines.append("- none")
    lines.append("")

    lines.append("## Errors")
    lines.append("")
    errors = report.by_severity("error")
    if errors:
        lines += [f"- {f.file}: {f.message}" for f in errors]
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Warnings")
    lines.append("")
    warnings = report.by_severity("warn")
    if warnings:
        lines += [f"- {f.file}: {f.code} — {f.message}" for f in warnings]
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Info")
    lines.append("")
    infos = report.by_severity("info")
    if infos:
        lines += [f"- {f.code} — {f.message}" for f in infos]
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def findings_body(report: Report) -> str:
    """Everything below the 'Run:' line — used to decide whether status changed."""
    body = render_status(report, "0000-00-00")
    return body.split("## Changes", 1)[1]
