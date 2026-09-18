"""Input file scanning and month detection for MVP-003."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from submit_flow_agent.pdf_renderer import MONTHLY_SOURCE_EXTENSIONS


REQUIRED_FILE_TYPES = (
    "generation_statement",
    "fee_statement",
    "energy_statement",
)

FILE_TYPE_KEYWORDS = {
    "generation_statement": ("发电单",),
    "fee_statement": ("电费结算单",),
    "energy_statement": ("电量结算单",),
}
CONTENT_TYPE_KEYWORDS = {
    "generation_statement": (
        "发电单",
        "发电结算单",
        "发电情况电子账单",
        "发电项目户号",
        "电表编号",
        "上期示数",
        "本期示数",
        "本期起始日期",
        "本期结束日期",
        "月总发电量",
    ),
    "fee_statement": (
        "电费结算单",
        "上网电费",
        "上网电价",
        "结算电价",
        "结算小计",
    ),
    "energy_statement": (
        "电量结算单",
        "上网电量",
        "结算电量",
        "电量结算",
    ),
}

MONTH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(20\d{2})"
    r"(?:"
    r"(0[1-9]|1[0-2])"
    r"|[-.](0[1-9]|1[0-2])"
    r"|\u5e74\s*(0?[1-9]|1[0-2])\s*\u6708"
    r")"
    r"(?![A-Za-z0-9])"
)
SEMANTIC_MONTH_PATTERN = re.compile(
    r"(?:\u53d1\u7535\u6708\u4efd|\u8d2d\u7535\u6708\u4efd|\u7ed3\u7b97\u6708\u4efd|\u8d26\u5355\u6708\u4efd|\u7535\u8d39\u6708\u4efd|\u7535\u91cf\u6708\u4efd|\u6240\u5c5e\u6708\u4efd|\u7ed3\u7b97\u5e74\u6708)"
    r"\s*[:\uff1a]?\s*"
    r"(20\d{2})"
    r"(?:"
    r"(0[1-9]|1[0-2])"
    r"|[-.](0[1-9]|1[0-2])"
    r"|\u5e74\s*(0?[1-9]|1[0-2])\s*\u6708?"
    r")",
)


class FileScanError(RuntimeError):
    """Raised when source files cannot be classified into one valid month."""


@dataclass(frozen=True)
class ScanResult:
    month: str
    files: dict[str, Path]

    def to_dict(self, relative_to: Path | None = None) -> dict[str, object]:
        return {
            "month": self.month,
            "files": {
                file_type: _display_path(path, relative_to)
                for file_type, path in self.files.items()
            },
        }


@dataclass(frozen=True)
class PdfClassificationResult:
    role: str | None
    source: str
    confidence: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "role": self.role,
            "source": self.source,
            "confidence": self.confidence,
        }


def scan_input_files(project_dir: Path | str, *, expected_month: str | None = None) -> ScanResult:
    """Scan one project directory and classify three monthly PDF/JPG/PNG inputs."""

    root = Path(project_dir)
    if not root.exists():
        raise FileScanError(f"Input directory does not exist: {root}")
    if not root.is_dir():
        raise FileScanError(f"Input path is not a directory: {root}")

    matches: dict[str, list[Path]] = {file_type: [] for file_type in REQUIRED_FILE_TYPES}
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix.lower() not in MONTHLY_SOURCE_EXTENSIONS:
            continue
        file_type = classify_source_pdf(path)
        if file_type:
            matches[file_type].append(path)

    missing = [file_type for file_type, paths in matches.items() if not paths]
    if missing:
        raise FileScanError(f"Missing required source file types: {', '.join(missing)}.")

    duplicated = [file_type for file_type, paths in matches.items() if len(paths) > 1]
    if duplicated:
        details = "; ".join(
            f"{file_type}: {', '.join(path.name for path in matches[file_type])}"
            for file_type in duplicated
        )
        raise FileScanError(f"Duplicate source files found: {details}.")

    files = {file_type: paths[0] for file_type, paths in matches.items()}
    filename_months = {file_type: detect_month(path.name) for file_type, path in files.items()}
    semantic_months = {
        file_type: detect_semantic_months(extract_pdf_text(path))
        for file_type, path in files.items()
    }
    if expected_month is not None:
        if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", expected_month):
            raise FileScanError(f"Invalid expected month: {expected_month}.")
        filename_conflicts = {
            file_type: month
            for file_type, month in filename_months.items()
            if month is not None and month != expected_month
        }
        if filename_conflicts:
            details = ", ".join(f"{file_type}={month}" for file_type, month in filename_conflicts.items())
            raise FileScanError(f"Source file months do not match expected month {expected_month}: {details}.")
        content_conflicts = {
            file_type: months
            for file_type, months in semantic_months.items()
            if any(month != expected_month for month in months)
        }
        if content_conflicts:
            details = ", ".join(
                f"{file_type}={','.join(months)}"
                for file_type, months in content_conflicts.items()
            )
            raise FileScanError(f"PDF content months do not match expected month {expected_month}: {details}.")
        return ScanResult(month=expected_month, files=files)

    months: dict[str, str | None] = {}
    for file_type in REQUIRED_FILE_TYPES:
        content_months = semantic_months[file_type]
        if len(content_months) > 1:
            raise FileScanError(
                f"PDF content contains conflicting statement months for {file_type}: {', '.join(content_months)}."
            )
        content_month = content_months[0] if content_months else None
        filename_month = filename_months[file_type]
        if content_month and filename_month and content_month != filename_month:
            raise FileScanError(
                f"PDF content month {content_month} does not match filename month {filename_month} for {file_type}."
            )
        months[file_type] = content_month or filename_month
    missing_months = [file_type for file_type, month in months.items() if month is None]
    if missing_months:
        raise FileScanError(f"Could not detect month for file types: {', '.join(missing_months)}.")

    unique_months = set(months.values())
    if len(unique_months) != 1:
        details = ", ".join(f"{file_type}={month}" for file_type, month in months.items())
        raise FileScanError(f"Source file months do not match: {details}.")

    return ScanResult(month=unique_months.pop() or "", files=files)


def classify_source_file(filename: str) -> str | None:
    for file_type, keywords in FILE_TYPE_KEYWORDS.items():
        if all(keyword in filename for keyword in keywords):
            return file_type
    return None


def classify_source_pdf(path: Path | str) -> str | None:
    return classify_source_pdf_result(path).role


def classify_source_pdf_result(path: Path | str) -> PdfClassificationResult:
    source = Path(path)
    if source.suffix.lower() not in MONTHLY_SOURCE_EXTENSIONS:
        return PdfClassificationResult(role=None, source="unsupported_extension", confidence="none")
    content_type = _classify_text(_extract_pdf_text(source)) if source.suffix.lower() == ".pdf" else None
    if content_type is not None:
        return PdfClassificationResult(role=content_type, source="pdf_text", confidence="high")
    filename_type = classify_source_file(source.name)
    if filename_type is not None:
        return PdfClassificationResult(role=filename_type, source="filename_hint", confidence="supporting")
    return PdfClassificationResult(role=None, source="unclassified", confidence="none")


def classify_source_pdf_by_content(path: Path | str) -> PdfClassificationResult:
    result = classify_source_pdf_result(path)
    if result.source == "pdf_text":
        return result
    return PdfClassificationResult(role=result.role, source=result.source, confidence="low")


def classify_pdf_text_content(text: str, *, source: str) -> PdfClassificationResult:
    role = _classify_text(text)
    if role is None:
        return PdfClassificationResult(role=None, source=source, confidence="none")
    return PdfClassificationResult(role=role, source=source, confidence="high")


def extract_pdf_text(path: Path | str) -> str:
    return _extract_pdf_text(Path(path))

def detect_month(filename: str) -> str | None:
    match = MONTH_PATTERN.search(filename)
    if not match:
        return None
    month = match.group(2) or match.group(3) or match.group(4)
    return f"{match.group(1)}-{int(month):02d}"


def detect_semantic_months(text: str) -> list[str]:
    """Return statement months that are attached to explicit business month labels."""

    months: list[str] = []
    for match in SEMANTIC_MONTH_PATTERN.finditer(text or ""):
        month = match.group(2) or match.group(3) or match.group(4)
        normalized = f"{match.group(1)}-{int(month):02d}"
        if normalized not in months:
            months.append(normalized)
    return months


def _display_path(path: Path, relative_to: Path | None) -> str:
    if relative_to is None:
        return path.name
    try:
        return str(path.relative_to(relative_to))
    except ValueError:
        return str(path)


def _classify_text(text: str) -> str | None:
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    scores = {
        file_type: sum(1 for keyword in keywords if keyword in compact)
        for file_type, keywords in CONTENT_TYPE_KEYWORDS.items()
    }
    winner, score = max(scores.items(), key=lambda item: item[1])
    if score <= 0:
        return None
    if sum(1 for value in scores.values() if value == score) > 1:
        return None
    return winner


def _extract_pdf_text(source: Path) -> str:
    if source.suffix.lower() != ".pdf":
        return ""
    text = _extract_pdf_text_with_pypdf(source)
    if text:
        return text
    try:
        data = source.read_bytes()
    except OSError:
        return ""
    candidates = []
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            candidates.append(data.decode(encoding, errors="ignore"))
        except LookupError:
            continue
    return "\n".join(candidates)


def _extract_pdf_text_with_pypdf(source: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except Exception:
        return ""
    try:
        reader = PdfReader(str(source))
        parts = [page.extract_text() or "" for page in reader.pages[:2]]
    except Exception:
        return ""
    return "\n".join(parts)
