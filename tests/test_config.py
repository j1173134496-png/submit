from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from submit_flow_agent.config import ConfigError, REQUIRED_SITE_FIELDS, list_site_keys, list_sites, load_site_config


class SiteConfigTests(unittest.TestCase):
    def test_load_xinan_site_config_contains_required_fields(self) -> None:
        site = load_site_config()

        self.assertEqual(site.site_key, "xinan_high_school")
        self.assertTrue(site.site_name)
        self.assertTrue(site.project_name)
        self.assertEqual(site.meter_no, "09001SF00000042508945216")
        self.assertTrue(site.company_name)
        self.assertEqual(site.multiplier, Decimal("150"))
        self.assertIn("福域", site.issuer_aliases)
        self.assertGreaterEqual(len(site.aliases), 1)
        self.assertEqual(len(site.meters), 1)
        self.assertEqual(site.meters[0].meter_no, "09001SF00000042508945216")
        self.assertEqual(site.school_base_price, Decimal("0.6998"))
        self.assertEqual(site.school_discount_rate, Decimal("0.95"))
        self.assertEqual(site.template_dir, str(Path("config/templates/xinan_high_school_blank").resolve()))
        self.assertTrue(site.detail_workbook.endswith(".xlsx"))
        self.assertEqual(site.detail_sheet, "1")
        self.assertTrue(site.summary_workbook.endswith(".xlsx"))
        self.assertEqual(site.summary_sheet, "Sheet1")

    def test_sites_json_defines_all_required_fields(self) -> None:
        payload = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
        site = payload["sites"][0]

        missing = [field for field in REQUIRED_SITE_FIELDS if field not in site]

        self.assertEqual(missing, [])

    def test_output_dir_uses_site_name_and_year_month(self) -> None:
        site = load_site_config()

        self.assertEqual(site.output_dir("2026-05"), Path("outputs") / site.site_name / "2026-05")

    def test_unknown_site_raises_clear_config_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "was not found"):
            load_site_config("missing_site")

    def test_missing_required_field_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_config = Path(tmpdir) / "sites.json"
            payload = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
            del payload["sites"][0]["meters"]
            del payload["sites"][0]["meter_no"]
            bad_config.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "missing fields: meter_no"):
                load_site_config(config_path=bad_config)

    def test_invalid_month_for_output_dir_raises_clear_error(self) -> None:
        site = load_site_config()

        with self.assertRaisesRegex(ConfigError, "expected YYYY-MM"):
            site.output_dir("202605")

    def test_load_xixiang_site_config_contains_two_meters(self) -> None:
        site = load_site_config("xixiang_middle_school")

        self.assertTrue(site.site_name)
        self.assertGreaterEqual(len(site.aliases), 2)
        self.assertEqual([meter.meter_no[-3:] for meter in site.meters], ["216", "217"])
        self.assertIn("华盛腾", site.issuer_aliases)
        self.assertEqual([meter.meter_no[-3:] for meter in site.detail_meters], ["216", "217"])
        self.assertEqual(site.school_discount_rate, Decimal("0.95"))
        self.assertEqual(site.summary_start_row, 4)

    def test_list_sites_returns_gateway_contract_shape(self) -> None:
        payload = list_sites()

        xixiang = next(site for site in payload["sites"] if site["site_key"] == "xixiang_middle_school")
        self.assertTrue(xixiang["site_name"])
        self.assertEqual(xixiang["template_profile_key"], "xixiang_meter_rows_v1")
        self.assertEqual(xixiang["meter_topology_profile"], "dual_meter_school")
        self.assertIn("xixiang_middle_school", xixiang["resolution_aliases"])
        self.assertGreaterEqual(len(xixiang["aliases"]), 2)

    def test_retired_site_remains_key_addressable_but_is_not_listed_or_alias_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "sites.json"
            payload = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
            payload["sites"][0]["retired"] = True
            payload["sites"][4]["site_name"] = "新安高中部"
            config.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            self.assertEqual(load_site_config("xinan_high_school", config).site_key, "xinan_high_school")
            self.assertEqual(load_site_config("新安高中部", config).site_key, "site_5f14cc7ab0e2")
            self.assertIn("xinan_high_school", list_site_keys(config))
            self.assertNotIn("xinan_high_school", [item["site_key"] for item in list_sites(config)["sites"]])


if __name__ == "__main__":
    unittest.main()
