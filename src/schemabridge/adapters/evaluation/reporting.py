"""Deterministic JSON and Markdown evaluation report writer."""

from __future__ import annotations

import json
from pathlib import Path

from schemabridge.application.ports.evaluation import EvaluationError, EvaluationErrorCode
from schemabridge.domain.evaluation import EvaluationReport, EvaluationRunStatus


class FileEvaluationReportWriter:
    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()

    def write(self, report: EvaluationReport, json_path: Path, markdown_path: Path) -> None:
        resolved_json = self._output_path(json_path, ".json")
        resolved_markdown = self._output_path(markdown_path, ".md")
        try:
            resolved_json.parent.mkdir(parents=True, exist_ok=True)
            resolved_markdown.parent.mkdir(parents=True, exist_ok=True)
            resolved_json.write_text(
                json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            resolved_markdown.write_text(_markdown(report), encoding="utf-8")
        except OSError as error:
            raise EvaluationError(
                EvaluationErrorCode.REPORT_WRITE_FAILED,
                "evaluation report artifacts could not be written",
            ) from error

    def _output_path(self, value: Path, suffix: str) -> Path:
        resolved = (self._root / value).resolve() if not value.is_absolute() else value.resolve()
        if not resolved.is_relative_to(self._root) or resolved.suffix != suffix:
            raise EvaluationError(
                EvaluationErrorCode.REPORT_WRITE_FAILED,
                f"evaluation output must be a {suffix} file inside the repository",
            )
        return resolved


def _markdown(report: EvaluationReport) -> str:
    release_label = report.release.revision
    if report.release.dirty:
        release_label += " (dirty/uncommitted; not a release-commit claim)"
    lines = [
        "# SchemaBridge synthetic evaluation report",
        "",
        "> Generated evidence for the small synthetic fixture only. Raw counts are shown; no "
        "confidence interval or production-quality claim is justified.",
        "",
        f"- Overall required-run status: **{'PASS' if report.successful else 'FAIL'}**",
        f"- Source revision: `{release_label}`",
        f"- Source fingerprint: `{report.release.source_fingerprint}`",
        f"- Ground-truth version: `{report.ground_truth_version}`",
        f"- Fixture fingerprint: `{report.fixture_fingerprint}`",
        f"- Package: `{report.release.package_version}`",
        "- Regression thresholds: none; the measured fixture is too small to justify one.",
        "",
    ]
    for run in report.runs:
        lines.extend((f"## {run.mode.value}", "", f"Adapter: `{run.adapter}`."))
        if run.status is EvaluationRunStatus.NOT_RUN:
            lines.extend(("", f"Status: **not run** — {run.reason}", ""))
            continue
        lines.extend(("", f"Status: **{run.status.value}**", ""))
        for section in run.sections:
            lines.extend(
                (
                    f"### {section.label}",
                    "",
                    f"Section status: **{section.status.value}**",
                    "",
                )
            )
            if section.metrics:
                lines.extend(
                    (
                        "| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |",
                        "|---|---:|---:|---:|---:|---:|",
                    )
                )
                for metric in section.metrics:
                    lines.append(
                        f"| `{metric.name}` | {metric.numerator:g}/{metric.denominator:g} | "
                        f"{metric.value:.3f} | {metric.evaluated_cases} | "
                        f"{metric.skipped_cases} | {metric.failed_cases} |"
                    )
                lines.append("")
                for metric in section.metrics:
                    lines.append(f"- `{metric.name}`: {metric.note}")
                lines.append("")
            if section.cases:
                lines.extend(
                    (
                        "| Case | Status | Expected | Actual | Detail |",
                        "|---|---|---|---|---|",
                    )
                )
                for case in section.cases:
                    lines.append(
                        f"| `{case.id}` | {case.status.value} | {_cell(case.expected)} | "
                        f"{_cell(case.actual)} | {_cell(case.detail)} |"
                    )
                lines.append("")
            for failure in section.failures:
                lines.append(f"- Failure: `{failure}`")
            if section.failures:
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
