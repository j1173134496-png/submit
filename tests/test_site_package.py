from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from submit_flow_agent.site_package import (
    SitePackageError,
    inspect_site_package,
    list_registered_sites,
    register_site_package,
    validate_site_package,
)


class SitePackageTests(unittest.TestCase):
    def test_inspect_and_validate_candidate_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = _write_package(root, site_key="demo_school", aliases=["Demo School"])
            result = inspect_site_package(package)

            self.assertEqual(result["site_key"], "demo_school")
            self.assertEqual(result["detected_month"], "2026-05")
            self.assertEqual([meter["meter_no"] for meter in result["meters"]], ["METER-001"])

            validation = validate_site_package(package)
            self.assertTrue(validation["valid"])
            self.assertEqual(validation["site_key"], "demo_school")

    def test_register_site_package_is_atomic_and_rejects_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "sites.json"
            shutil.copy2(Path("config/sites.json"), config_path)
            (root / "templates").mkdir()
            package = _write_package(root / "pkg", site_key="demo_school", aliases=["Demo School"], confirmed=True)

            result = register_site_package(package, config_path=config_path)

            self.assertTrue(result["registered"])
            payload = list_registered_sites(config_path)
            self.assertIn("demo_school", [site["site_key"] for site in payload["sites"]])
            self.assertTrue((root / "templates" / "demo_school" / "detail.xlsx").is_file())
            with self.assertRaisesRegex(SitePackageError, "Duplicate site_key"):
                register_site_package(package, config_path=config_path)

    def test_register_rolls_back_when_template_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _copy_registry(root)
            package = _write_package(
                root / "pkg",
                site_key="demo_template_failure",
                aliases=["Demo Template Failure"],
                confirmed=True,
            )
            before = config_path.read_text(encoding="utf-8")

            def fail_template_publish(source: Path, target: Path) -> None:
                raise OSError("injected template publish failure")

            with self.assertRaisesRegex(SitePackageError, "partial registration"):
                register_site_package(
                    package,
                    config_path=config_path,
                    _publish_template_dir=fail_template_publish,
                )

            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertFalse((root / "templates" / "demo_template_failure").exists())
            payload = list_registered_sites(config_path)
            self.assertNotIn("demo_template_failure", [site["site_key"] for site in payload["sites"]])

    def test_register_rolls_back_template_when_config_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _copy_registry(root)
            package = _write_package(
                root / "pkg",
                site_key="demo_config_failure",
                aliases=["Demo Config Failure"],
                confirmed=True,
            )
            before = config_path.read_text(encoding="utf-8")

            def fail_config_publish(source: Path, target: Path) -> None:
                raise OSError("injected config publish failure")

            with self.assertRaisesRegex(SitePackageError, "partial registration"):
                register_site_package(
                    package,
                    config_path=config_path,
                    _replace_config=fail_config_publish,
                )

            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertFalse((root / "templates" / "demo_config_failure").exists())
            payload = list_registered_sites(config_path)
            self.assertNotIn("demo_config_failure", [site["site_key"] for site in payload["sites"]])

    def test_register_rejects_existing_template_target_without_touching_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _copy_registry(root)
            target = root / "templates" / "demo_existing_target"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("user-owned", encoding="utf-8")
            package = _write_package(
                root / "pkg",
                site_key="demo_existing_target",
                aliases=["Demo Existing Target"],
                confirmed=True,
            )

            with self.assertRaisesRegex(SitePackageError, "Template target already exists"):
                register_site_package(package, config_path=config_path)

            self.assertEqual(marker.read_text(encoding="utf-8"), "user-owned")
            payload = list_registered_sites(config_path)
            self.assertNotIn("demo_existing_target", [site["site_key"] for site in payload["sites"]])

    def test_validate_rejects_alias_conflict_unknown_profile_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            alias_conflict = _write_package(root / "alias", site_key="demo_alias", aliases=["西乡中学"])
            with self.assertRaisesRegex(SitePackageError, "Identity conflict"):
                validate_site_package(alias_conflict)

            unknown_profile = _write_package(root / "profile", site_key="demo_profile")
            site_path = unknown_profile / "site.json"
            payload = json.loads(site_path.read_text(encoding="utf-8"))
            payload["detail_profile"] = "mystery"
            site_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(SitePackageError, "UNSUPPORTED_PROFILE"):
                validate_site_package(unknown_profile)

            traversal = _write_package(root / "traversal", site_key="demo_traversal")
            site_path = traversal / "site.json"
            payload = json.loads(site_path.read_text(encoding="utf-8"))
            payload["detail_workbook"] = "../detail.xlsx"
            site_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(SitePackageError, "Path traversal"):
                validate_site_package(traversal)

    def test_validate_rejects_unified_identity_namespace_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            alias_to_key = _write_package(root / "alias_key", site_key="demo_alias_key", aliases=["xinan_high_school"])
            with self.assertRaisesRegex(SitePackageError, "Identity conflict"):
                validate_site_package(alias_to_key)

            key_to_alias = _write_package(root / "key_alias", site_key="西乡中学", aliases=["Demo Key Alias"])
            with self.assertRaisesRegex(SitePackageError, "Identity conflict"):
                validate_site_package(key_to_alias)

            key_to_name = _write_package(root / "key_name", site_key="深圳市西乡中学", aliases=["Demo Key Name"])
            with self.assertRaisesRegex(SitePackageError, "Identity conflict"):
                validate_site_package(key_to_name)

            internal_conflict = _write_package(
                root / "internal",
                site_key="internal_conflict",
                site_name=" internal_conflict ",
                aliases=["Internal Conflict"],
            )
            with self.assertRaisesRegex(SitePackageError, "within site package"):
                validate_site_package(internal_conflict)

            key_case_conflict = _write_package(
                root / "key_case",
                site_key="XINAN_HIGH_SCHOOL",
                aliases=["Demo Key Case"],
            )
            with self.assertRaisesRegex(SitePackageError, "Duplicate site_key"):
                validate_site_package(key_case_conflict)

            config_path = root / "sites.json"
            payload = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
            payload["sites"][0]["aliases"].append("Whitespace School")
            config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            whitespace_conflict = _write_package(
                root / "whitespace",
                site_key="demo_whitespace",
                aliases=["Whitespace   School"],
            )
            with self.assertRaisesRegex(SitePackageError, "Identity conflict"):
                validate_site_package(whitespace_conflict, config_path=config_path)

    def test_validate_rejects_template_hash_change_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = _write_package(root / "hash", site_key="demo_hash")
            detail = package / "templates" / "detail.xlsx"
            detail.write_bytes(b"changed")
            with self.assertRaisesRegex(SitePackageError, "Template hash changed"):
                validate_site_package(package)

            symlink_package = _write_package(root / "symlink", site_key="demo_symlink")
            link = symlink_package / "templates" / "detail.xlsx"
            link.unlink()
            try:
                os.symlink(symlink_package / "templates" / "summary.xlsx", link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not available in this environment")
            with self.assertRaisesRegex(SitePackageError, "symbolic link"):
                validate_site_package(symlink_package)


def _write_package(
    root: Path,
    *,
    site_key: str,
    site_name: str | None = None,
    aliases: list[str] | None = None,
    confirmed: bool = False,
) -> Path:
    package = root
    templates = package / "templates"
    samples = package / "samples"
    templates.mkdir(parents=True)
    samples.mkdir()
    detail = templates / "detail.xlsx"
    summary = templates / "summary.xlsx"
    detail.write_bytes(b"detail-template")
    summary.write_bytes(b"summary-template")
    for name in ("demo发电单202605.pdf", "demo电费结算单202605.pdf", "demo电量结算单202605.pdf"):
        (samples / name).write_bytes(b"%PDF-1.4\n")
    site = {
        "site_key": site_key,
        "site_name": site_name or f"{site_key} name",
        "project_name": f"{site_key} project",
        "aliases": aliases or [],
        "company_name": "Demo Company",
        "school_base_price": "0.6998",
        "school_discount_rate": "1",
        "detail_workbook": "detail.xlsx",
        "detail_sheet": "1",
        "summary_workbook": "summary.xlsx",
        "summary_sheet": "Sheet1",
        "detail_profile": "single_detail_row",
        "summary_profile": "meter_rows_summary",
        "meters": [
            {
                "meter_no": "METER-001",
                "multiplier": "150",
                "role": "primary",
                "detail_enabled": True,
                "grid_export_allocation": "all",
            }
        ],
        "template_hashes": {
            "detail": _sha256(detail),
            "summary": _sha256(summary),
        },
    }
    if confirmed:
        site["confirmed"] = True
    (package / "site.json").write_text(json.dumps(site, ensure_ascii=False), encoding="utf-8")
    return package


def _copy_registry(root: Path) -> Path:
    config_path = root / "sites.json"
    shutil.copy2(Path("config/sites.json"), config_path)
    (root / "templates").mkdir()
    return config_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
