"""PDF page rendering for MVP-004."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


MONTHLY_SOURCE_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".png"})
MAX_ARTIFACT_STEM_LENGTH = 72


class PdfRenderError(RuntimeError):
    """Raised when a PDF cannot be rendered into page images."""


@dataclass(frozen=True)
class RenderedPage:
    source_file: Path
    page: int
    image_path: Path

    def to_dict(self, relative_to: Path | None = None) -> dict[str, object]:
        return {
            "page": self.page,
            "image_path": _display_path(self.image_path, relative_to),
        }


def render_pdf_to_images(
    pdf_path: Path | str,
    output_dir: Path | str,
    *,
    dpi: int = 200,
    pdftoppm_path: Path | str | None = None,
) -> list[RenderedPage]:
    """Render one PDF into PNG page images using Poppler pdftoppm."""

    source = Path(pdf_path)
    if not source.exists():
        raise PdfRenderError(f"PDF does not exist: {source}")
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise PdfRenderError(f"Input is not a PDF file: {source}")
    if dpi <= 0:
        raise PdfRenderError(f"DPI must be positive, got {dpi}.")

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    executable = Path(pdftoppm_path) if pdftoppm_path is not None else find_pdftoppm()
    artifact_stem = safe_artifact_stem(source.stem)
    output_prefix = target_dir / artifact_stem
    _remove_existing_outputs(target_dir, artifact_stem)

    command = [str(executable), "-png", "-r", str(dpi), str(source), str(output_prefix)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "unknown error").strip()
        raise PdfRenderError(f"pdftoppm failed for {source}: {message}")

    image_paths = sorted(target_dir.glob(f"{artifact_stem}-*.png"), key=_page_sort_key)
    if not image_paths:
        raise PdfRenderError(f"pdftoppm produced no PNG pages for {source}.")

    return [
        RenderedPage(source_file=source, page=_page_number(path), image_path=path)
        for path in image_paths
    ]


def render_monthly_source_to_images(
    source_path: Path | str,
    output_dir: Path | str,
    *,
    dpi: int = 200,
    pdftoppm_path: Path | str | None = None,
) -> list[RenderedPage]:
    """Render a PDF or normalize one JPG/PNG source into OCR-ready PNG pages."""

    source = Path(source_path)
    if source.suffix.lower() == ".pdf":
        return render_pdf_to_images(source, output_dir, dpi=dpi, pdftoppm_path=pdftoppm_path)
    validate_monthly_source_file(source)

    from PIL import Image, ImageOps, UnidentifiedImageError

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_artifact_stem(source.stem)}-1.png"
    try:
        with Image.open(source) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.save(target, format="PNG")
    except (OSError, UnidentifiedImageError) as exc:
        raise PdfRenderError(f"Could not decode monthly source image: {source}") from exc
    return [RenderedPage(source_file=source, page=1, image_path=target)]


def validate_monthly_source_file(source_path: Path | str) -> Path:
    """Validate a regular PDF/JPG/PNG source by extension and magic bytes."""

    source = Path(source_path)
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise PdfRenderError(f"Monthly source is not an existing regular file: {source}")
    suffix = source.suffix.lower()
    if suffix not in MONTHLY_SOURCE_EXTENSIONS:
        raise PdfRenderError(f"Unsupported monthly source extension: {source.name}")
    try:
        header = source.read_bytes()[:8]
    except OSError as exc:
        raise PdfRenderError(f"Could not read monthly source: {source}") from exc
    valid = (
        (suffix == ".pdf" and header.startswith(b"%PDF-"))
        or (suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
        or (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
    )
    if not valid:
        raise PdfRenderError(f"Monthly source signature does not match its extension: {source.name}")
    return source


def safe_artifact_stem(stem: str, *, max_length: int = MAX_ARTIFACT_STEM_LENGTH) -> str:
    """Keep generated OCR artifact names below common Windows path limits."""

    if len(stem) <= max_length:
        return stem
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]
    prefix_length = max(1, max_length - len(digest) - 1)
    return f"{stem[:prefix_length]}.{digest}"


def find_pdftoppm() -> Path:
    """Find a usable pdftoppm executable in PATH or the bundled Codex runtime."""

    found = _find_pdftoppm_from_path(windows=sys.platform.startswith("win"))
    if found is not None:
        return found

    if not sys.platform.startswith("win"):
        raise PdfRenderError("Could not find pdftoppm executable for PDF rendering.")

    runtime_root = Path(sys.executable).resolve().parent.parent
    bundled = runtime_root / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
    if bundled.exists():
        return bundled

    raise PdfRenderError("Could not find pdftoppm executable for PDF rendering.")


def _find_pdftoppm_from_path(*, windows: bool) -> Path | None:
    names = ("pdftoppm.exe", "pdftoppm") if windows else ("pdftoppm",)
    for name in names:
        found = shutil.which(name)
        if found is None:
            continue
        candidate = Path(found)
        if not windows or candidate.suffix.lower() == ".exe":
            return candidate
    return None


def _remove_existing_outputs(target_dir: Path, stem: str) -> None:
    for old_page in target_dir.glob(f"{stem}-*.png"):
        old_page.unlink()


def _page_sort_key(path: Path) -> int:
    return _page_number(path)


def _page_number(path: Path) -> int:
    suffix = path.stem.rsplit("-", 1)[-1]
    try:
        return int(suffix)
    except ValueError as exc:
        raise PdfRenderError(f"Could not parse rendered page number from {path.name}.") from exc


def _display_path(path: Path, relative_to: Path | None) -> str:
    if relative_to is None:
        return str(path)
    try:
        return str(path.relative_to(relative_to))
    except ValueError:
        return str(path)
