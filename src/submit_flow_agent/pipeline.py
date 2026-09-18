"""End-to-end monthly pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from submit_flow_agent.config import load_site_config
from submit_flow_agent.excel_writer import write_detail_excel, write_summary_excel
from submit_flow_agent.field_mapper import FieldMappingError, RuntimeClock, map_fields_to_file, runtime_statement_date
from submit_flow_agent.file_scanner import ScanResult, scan_input_files
from submit_flow_agent.logger import ProcessLog
from submit_flow_agent.ocr.base import OcrAdapter, write_ocr_raw_result
from submit_flow_agent.ocr.factory import create_ocr_adapter
from submit_flow_agent.pricing import validate_task_pricing_snapshot
from submit_flow_agent.pdf_renderer import render_monthly_source_to_images, safe_artifact_stem
from submit_flow_agent.review import apply_confirmed_data, build_review_report, write_review_report
from submit_flow_agent.validator import validate_data_to_file


class PipelineError(RuntimeError):
    """Raised when the monthly pipeline cannot complete."""


@dataclass(frozen=True)
class RunMonthResult:
    site_key: str
    site_name: str
    month: str
    output_dir: Path
    recognized_data: Path
    validation_report: Path
    detail_workbook: Path | None
    summary_workbook: Path | None
    process_log: Path
    status: str
    review_report: Path | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "site_key": self.site_key,
            "site_name": self.site_name,
            "month": self.month,
            "output_dir": str(self.output_dir),
            "recognized_data": str(self.recognized_data),
            "validation_report": str(self.validation_report),
            "detail_workbook": str(self.detail_workbook) if self.detail_workbook else None,
            "summary_workbook": str(self.summary_workbook) if self.summary_workbook else None,
            "process_log": str(self.process_log),
            "review_report": str(self.review_report) if self.review_report else None,
            "status": self.status,
        }


def run_month(
    project_dir: Path | str,
    *,
    site_key: str = "xinan_high_school",
    output_base: Path | str = "outputs",
    config_path: Path | str = "config/sites.json",
    confirmed_data: Path | str | None = None,
    ocr_adapter: OcrAdapter | None = None,
    ocr_adapter_name: str | None = None,
    runtime_clock: RuntimeClock | None = None,
    pricing_snapshot: dict[str, object] | None = None,
    expected_month: str | None = None,
) -> RunMonthResult:
    project_root = Path(project_dir)
    site = load_site_config(site_key=site_key, config_path=config_path)
    scan_result = scan_input_files(project_root, expected_month=expected_month)
    if pricing_snapshot is None:
        raise PipelineError("run_month requires a frozen task pricing_snapshot.")
    task_pricing_snapshot = validate_task_pricing_snapshot(pricing_snapshot, site_key=site.site_key, month=scan_result.month)
    statement_date = runtime_statement_date(runtime_clock)
    output_dir = site.output_dir(scan_result.month, output_base)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_ocr_dir = output_dir / "raw_ocr"
    recognized_path = output_dir / "recognized_data.json"
    validation_path = output_dir / "validation_report.json"
    review_path = output_dir / "review_report.json"
    detail_path = output_dir / site.output_workbook_name("detail", scan_result.month)
    summary_path = output_dir / site.output_workbook_name("summary", scan_result.month)
    log_path = output_dir / "process_log.txt"
    process_log = ProcessLog(log_path)

    process_log.add("run-month started")
    process_log.add(f"project_dir={project_root}")
    process_log.add(f"site_key={site.site_key}")
    process_log.add(f"site_name={site.site_name}")
    process_log.add(f"month={scan_result.month}")
    process_log.add(f"statement_date={statement_date}")
    if confirmed_data is not None:
        process_log.add(f"confirmed_data={confirmed_data}")
    for file_type, path in scan_result.files.items():
        process_log.add(f"source_file[{file_type}]={path.name}")

    detail_output: Path | None = None
    summary_output: Path | None = None
    try:
        adapter = ocr_adapter or create_ocr_adapter(ocr_adapter_name)
        process_log.add(f"ocr_adapter={type(adapter).__name__}")
        raw_files = _generate_ocr_raw(scan_result, output_dir, raw_ocr_dir, adapter)
        process_log.add(f"raw_ocr_dir={raw_ocr_dir}")
        for raw_file in raw_files:
            process_log.add(f"raw_ocr_file={raw_file.name}")

        try:
            recognized = map_fields_to_file(
                project_root,
                recognized_path,
                site_key=site_key,
                config_path=config_path,
                ocr_raw_dir=raw_ocr_dir,
                runtime_clock=lambda: date.fromisoformat(statement_date),
                pricing_snapshot=task_pricing_snapshot,
                expected_month=scan_result.month,
            )
            process_log.add(f"recognized_data={recognized_path}")
            process_log.add(f"mapping_mode={recognized.mapping_mode}")
        except FieldMappingError as exc:
            review = build_review_report(
                month=scan_result.month,
                recognized_data_path=recognized_path,
                validation_report_path=validation_path,
                mapping_error=exc,
            )
            write_review_report(review, review_path)
            process_log.add(f"review_report={review_path}")
            process_log.add(f"review_issue_count={review['issue_count']}")
            process_log.add(f"failed_reason={exc}")
            process_log.add("run-month need_review")
            process_log.write()
            return _need_review_result(site, scan_result.month, output_dir, recognized_path, validation_path, review_path, log_path)

        if confirmed_data is not None:
            apply_confirmed_data(recognized_path, confirmed_data)
            process_log.add("manual_confirmed_data_applied=True")

        validation = validate_data_to_file(recognized_path, validation_path)
        process_log.add(f"validation_report={validation_path}")
        process_log.add(f"validation_status={validation.status}")
        process_log.add(f"can_write_excel={validation.can_write_excel}")
        if not validation.can_write_excel:
            review = build_review_report(
                month=scan_result.month,
                recognized_data_path=recognized_path,
                validation_report_path=validation_path,
                validation_report=validation,
            )
            write_review_report(review, review_path)
            reason = "; ".join(validation.errors) if validation.errors else "can_write_excel=false"
            process_log.add(f"review_report={review_path}")
            process_log.add(f"review_issue_count={review['issue_count']}")
            process_log.add(f"failed_reason={reason}")
            process_log.add("run-month need_review")
            process_log.write()
            return _need_review_result(site, scan_result.month, output_dir, recognized_path, validation_path, review_path, log_path)

        detail_result = write_detail_excel(
            validation_path,
            project_root,
            detail_path,
            site_key=site_key,
            config_path=config_path,
            pricing_snapshot=task_pricing_snapshot,
        )
        detail_output = detail_result.output
        process_log.add(f"detail_workbook={detail_output}")

        summary_result = write_summary_excel(
            validation_path,
            project_root,
            summary_path,
            site_key=site_key,
            config_path=config_path,
            pricing_snapshot=task_pricing_snapshot,
        )
        summary_output = summary_result.output
        process_log.add(f"summary_workbook={summary_output}")
        process_log.add("run-month completed")
        process_log.write()

        return RunMonthResult(
            site_key=site.site_key,
            site_name=site.site_name,
            month=scan_result.month,
            output_dir=output_dir,
            recognized_data=recognized_path,
            validation_report=validation_path,
            detail_workbook=detail_output,
            summary_workbook=summary_output,
            process_log=log_path,
            status="pass",
        )
    except Exception as exc:
        process_log.add(f"exception={type(exc).__name__}: {exc}")
        process_log.add("run-month failed")
        process_log.write()
        raise


def _need_review_result(
    site: object,
    month: str,
    output_dir: Path,
    recognized_path: Path,
    validation_path: Path,
    review_path: Path,
    log_path: Path,
) -> RunMonthResult:
    return RunMonthResult(
        site_key=site.site_key,
        site_name=site.site_name,
        month=month,
        output_dir=output_dir,
        recognized_data=recognized_path,
        validation_report=validation_path,
        detail_workbook=None,
        summary_workbook=None,
        process_log=log_path,
        review_report=review_path,
        status="need_review",
    )


def _generate_ocr_raw(scan_result: ScanResult, output_dir: Path, raw_ocr_dir: Path, adapter: OcrAdapter) -> list[Path]:
    raw_ocr_dir.mkdir(parents=True, exist_ok=True)
    image_dir = raw_ocr_dir / "images"
    _clear_generated_ocr_artifacts(raw_ocr_dir, image_dir)
    raw_files: list[Path] = []
    for source_file in scan_result.files.values():
        pages = render_monthly_source_to_images(source_file, image_dir, dpi=120)
        result = adapter.recognize(source_file, pages)
        raw_path = raw_ocr_dir / f"{safe_artifact_stem(source_file.stem)}.json"
        write_ocr_raw_result(result, raw_path, relative_to=output_dir)
        raw_files.append(raw_path)
    return raw_files


def _clear_generated_ocr_artifacts(raw_ocr_dir: Path, image_dir: Path) -> None:
    for raw_file in raw_ocr_dir.glob("*.json"):
        if raw_file.is_file() and not raw_file.is_symlink():
            raw_file.unlink()
    if image_dir.exists():
        for image_file in image_dir.glob("*.png"):
            if image_file.is_file() and not image_file.is_symlink():
                image_file.unlink()
