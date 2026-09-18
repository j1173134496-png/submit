"""Controlled local site package inspection, validation, and registration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from submit_flow_agent.config import (
    ConfigError,
    SUPPORTED_DETAIL_PROFILES,
    SUPPORTED_SUMMARY_PROFILES,
    list_sites,
    load_site_config,
)
from submit_flow_agent.file_scanner import FileScanError, scan_input_files


class SitePackageError(RuntimeError):
    """Raised when a site package is unsafe or invalid."""


def inspect_site_package(package_dir: Path | str) -> dict[str, Any]:
    root = _package_root(package_dir)
    site = _read_site_json(root)
    samples = root / "samples"
    detected_month = None
    sample_files: dict[str, str] = {}
    if samples.is_dir():
        try:
            scan = scan_input_files(samples)
            detected_month = scan.month
            sample_files = {file_type: path.name for file_type, path in scan.files.items()}
        except FileScanError:
            detected_month = None
    return {
        "site_key": site.get("site_key"),
        "site_name": site.get("site_name"),
        "aliases": site.get("aliases") or [],
        "detected_month": detected_month,
        "sample_files": sample_files,
        "meters": site.get("meters") or [],
        "detail_profile": site.get("detail_profile") or "single_detail_row",
        "summary_profile": site.get("summary_profile") or "meter_rows_summary",
        "templates": {
            "detail": site.get("detail_workbook"),
            "summary": site.get("summary_workbook"),
        },
    }


def validate_site_package(
    package_dir: Path | str,
    *,
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    root = _package_root(package_dir)
    site = _read_site_json(root)
    _validate_site_schema(site, root)
    _reject_existing_site_or_alias(site, config_path)
    templates = _template_paths(root, site)
    hashes = _validate_template_hashes(site, templates)
    return {
        "valid": True,
        "site_key": site["site_key"],
        "site_name": site["site_name"],
        "aliases": site.get("aliases") or [],
        "template_hashes": hashes,
    }


def register_site_package(
    package_dir: Path | str,
    *,
    config_path: Path | str = "config/sites.json",
    _replace_config: Callable[[Path, Path], None] | None = None,
    _publish_template_dir: Callable[[Path, Path], None] | None = None,
) -> dict[str, Any]:
    root = _package_root(package_dir)
    site = _read_site_json(root)
    if site.get("confirmed") is not True:
        raise SitePackageError("register-site requires site.json confirmed=true.")
    validation = validate_site_package(root, config_path=config_path)

    config_file = Path(config_path)
    payload = json.loads(config_file.read_text(encoding="utf-8-sig"))
    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise SitePackageError(f"{config_file} must contain a sites list.")

    templates = _template_paths(root, site)
    site_key = str(site["site_key"])
    _reject_unsafe_site_key(site_key)
    templates_root = config_file.parent / "templates"
    _ensure_template_root(templates_root)
    target_template_dir = templates_root / site_key
    if target_template_dir.exists() or target_template_dir.is_symlink():
        raise SitePackageError(f"Template target already exists for site_key: {site_key}")

    registered_site = {key: value for key, value in site.items() if key != "confirmed"}
    registered_site["template_dir"] = f"templates/{site_key}"
    payload["sites"] = [*sites, registered_site]

    replace_config = _replace_config or _replace_path
    publish_template_dir = _publish_template_dir or _replace_path

    try:
        with tempfile.TemporaryDirectory(dir=config_file.parent) as tmpdir:
            tmp_root = Path(tmpdir)
            tmp_config = tmp_root / config_file.name
            tmp_template_dir = tmp_root / "templates" / site_key
            tmp_template_dir.parent.mkdir()
            tmp_template_dir.mkdir()
            for label, source in templates.items():
                target = tmp_template_dir / source.name
                if target.exists():
                    raise SitePackageError(f"Duplicate template target for {label}: {target.name}")
                shutil.copy2(source, target, follow_symlinks=False)
            tmp_config.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            publish_template_dir(tmp_template_dir, target_template_dir)
            try:
                replace_config(tmp_config, config_file)
            except Exception:
                _rollback_template_dir(target_template_dir, templates_root)
                raise
    except SitePackageError:
        raise
    except Exception as exc:
        raise SitePackageError("Site package registration failed; no partial registration was kept.") from exc

    return {
        **validation,
        "registered": True,
        "config_path": str(config_file),
        "template_dir": str(target_template_dir),
    }


def list_registered_sites(config_path: Path | str = "config/sites.json") -> dict[str, list[dict[str, object]]]:
    return list_sites(config_path)


def _package_root(package_dir: Path | str) -> Path:
    root = Path(package_dir)
    if not root.is_dir():
        raise SitePackageError(f"Site package directory does not exist: {root}")
    return root


def _read_site_json(root: Path) -> dict[str, Any]:
    path = root / "site.json"
    if path.is_symlink() or not path.is_file():
        raise SitePackageError(f"site.json must be a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SitePackageError(f"site.json is not valid JSON: {path}") from exc
    site = payload.get("site") if isinstance(payload.get("site"), dict) else payload
    if not isinstance(site, dict):
        raise SitePackageError("site.json must contain a site object.")
    return site


def _validate_site_schema(site: dict[str, Any], root: Path) -> None:
    detail_profile = str(site.get("detail_profile") or "single_detail_row")
    summary_profile = str(site.get("summary_profile") or "meter_rows_summary")
    if detail_profile not in SUPPORTED_DETAIL_PROFILES:
        raise SitePackageError(f"UNSUPPORTED_PROFILE detail_profile={detail_profile}")
    if summary_profile not in SUPPORTED_SUMMARY_PROFILES:
        raise SitePackageError(f"UNSUPPORTED_PROFILE summary_profile={summary_profile}")
    _template_paths(root, site)
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "sites.json"
        site_for_parse = {key: value for key, value in site.items() if key != "confirmed"}
        site_for_parse.setdefault("template_dir", str(root / "templates"))
        config_path.write_text(json.dumps({"sites": [site_for_parse]}, ensure_ascii=False), encoding="utf-8")
        try:
            load_site_config(str(site["site_key"]), config_path=config_path)
        except (KeyError, ConfigError) as exc:
            raise SitePackageError(f"Invalid site schema: {exc}") from exc


def _reject_existing_site_or_alias(site: dict[str, Any], config_path: Path | str) -> None:
    new_identities = _identity_values(site)
    _reject_internal_identity_conflicts(new_identities)
    # Retired identities remain reserved so a future registration cannot
    # silently reuse an old site key, name, or alias.
    existing = list_sites(config_path, include_retired=True)["sites"]
    for registered in existing:
        registered_identities = _identity_values(registered)
        for new_field, new_value in new_identities:
            for existing_field, existing_value in registered_identities:
                if not _identity_matches((new_field, new_value), (existing_field, existing_value)):
                    continue
                if new_field == "site_key" and existing_field == "site_key":
                    raise SitePackageError(f"Duplicate site_key: {new_value}")
                registered_key = str(registered.get("site_key") or "")
                alias_note = " Alias conflict." if "alias" in {new_field, existing_field} else ""
                raise SitePackageError(
                    "Identity conflict: "
                    f"{new_field} conflicts with registered {existing_field} "
                    f"on site_key={registered_key}.{alias_note}"
                )


def _identity_values(site: dict[str, Any]) -> list[tuple[str, str]]:
    identities = [
        ("site_key", str(site.get("site_key") or "")),
        ("site_name", str(site.get("site_name") or "")),
    ]
    identities.extend(("alias", str(alias)) for alias in (site.get("aliases") or []))
    return [(field, _collapse_identity(value)) for field, value in identities if _collapse_identity(value)]


def _reject_internal_identity_conflicts(identities: list[tuple[str, str]]) -> None:
    for index, left in enumerate(identities):
        for right in identities[index + 1 :]:
            if _identity_matches(left, right):
                raise SitePackageError(
                    f"Identity conflict within site package: {left[0]} conflicts with {right[0]}."
                )


def _identity_matches(left: tuple[str, str], right: tuple[str, str]) -> bool:
    left_field, left_value = left
    right_field, right_value = right
    if left_field == "site_key" or right_field == "site_key":
        return left_value.casefold() == right_value.casefold()
    return left_value == right_value


def _collapse_identity(value: str) -> str:
    return " ".join(value.split())


def _reject_unsafe_site_key(site_key: str) -> None:
    path = Path(site_key)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise SitePackageError(f"Path traversal is not allowed for site_key: {site_key}")


def _ensure_template_root(templates_root: Path) -> None:
    if templates_root.is_symlink():
        raise SitePackageError("Template root must not be a symbolic link.")
    if templates_root.exists() and not templates_root.is_dir():
        raise SitePackageError("Template root must be a directory.")
    templates_root.mkdir(exist_ok=True)


def _replace_path(source: Path, target: Path) -> None:
    os.replace(source, target)


def _rollback_template_dir(target_template_dir: Path, templates_root: Path) -> None:
    if not target_template_dir.exists() and not target_template_dir.is_symlink():
        return
    if target_template_dir.is_symlink():
        target_template_dir.unlink()
        return
    try:
        target_template_dir.resolve().relative_to(templates_root.resolve())
    except ValueError as exc:
        raise SitePackageError("Rollback target escapes template root.") from exc
    if target_template_dir.is_dir():
        shutil.rmtree(target_template_dir)
    else:
        target_template_dir.unlink()


def _template_paths(root: Path, site: dict[str, Any]) -> dict[str, Path]:
    template_dir = root / "templates"
    detail_name = _safe_filename(site.get("detail_workbook"), "detail_workbook")
    summary_name = _safe_filename(site.get("summary_workbook"), "summary_workbook")
    paths = {
        "detail": template_dir / detail_name,
        "summary": template_dir / summary_name,
    }
    for label, path in paths.items():
        if path.is_symlink():
            raise SitePackageError(f"Template {label} must not be a symbolic link: {path}")
        if not path.is_file():
            raise SitePackageError(f"Template {label} does not exist: {path}")
        try:
            path.resolve().relative_to(template_dir.resolve())
        except ValueError as exc:
            raise SitePackageError(f"Template {label} path escapes templates directory.") from exc
    return paths


def _safe_filename(value: Any, field: str) -> str:
    if value is None or str(value).strip() == "":
        raise SitePackageError(f"site.json missing {field}.")
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise SitePackageError(f"Path traversal is not allowed for {field}: {value}")
    return str(value)


def _validate_template_hashes(site: dict[str, Any], templates: dict[str, Path]) -> dict[str, str]:
    actual = {label: _sha256(path) for label, path in templates.items()}
    expected = site.get("template_hashes")
    if expected is None:
        return actual
    if not isinstance(expected, dict):
        raise SitePackageError("template_hashes must be an object.")
    for label, digest in actual.items():
        if expected.get(label) != digest:
            raise SitePackageError(f"Template hash changed for {label}.")
    return actual


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
