from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.site_registration_drafts import (
    SiteRegistrationDraftError,
    _apply_metadata_ocr_fallback,
    _coordinate_table_meter_candidates,
    _extract_metadata_review,
    _metadata_unresolved_keys,
    collect_site_registration_draft_pdf_batch,
    confirm_site_registration_draft_pricing,
    finalize_site_registration_draft,
    prepare_site_registration_draft_pricing,
    prepare_site_registration_draft,
)


class SiteRegistrationDraftTests(unittest.TestCase):
    def test_draft_pricing_removes_binary_float_artifact_from_discount_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = prepare_site_registration_draft(
                root,
                conversation_key="trusted-session-float-price",
                site_name="Future School",
                month="2026-07",
                meter_topology_profile="single_meter_school",
            )

            proposal = prepare_site_registration_draft_pricing(
                root,
                str(draft["draft_id"]),
                pricing={"discount_rate": "0.8800000000000001", "effective_unit_price": "0.61424"},
            )

            self.assertEqual(proposal["pricing_state"]["pricing"]["discount_rate"], "0.88")
            self.assertEqual(proposal["pricing_state"]["pricing"]["effective_unit_price"], "0.61424")

    def test_prepare_creates_non_site_draft_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = prepare_site_registration_draft(
                Path(tmpdir),
                conversation_key="trusted-session-1",
                site_name="Future School",
                month="2026-07",
                meter_topology_profile="dual_meter_school",
            )

            self.assertEqual(payload["identity_type"], "site_registration_draft")
            self.assertEqual(payload["status"], "collecting_pdfs")
            self.assertEqual(payload["batch_state"], "staging")
            self.assertEqual(payload["received_count"], 0)
            self.assertTrue((Path(tmpdir) / "site_registration_drafts" / payload["draft_id"] / "draft.json").is_file())

    def test_collect_draft_batch_classifies_roles_and_requests_only_unconfirmed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = prepare_site_registration_draft(
                root,
                conversation_key="trusted-session-2",
                site_name="Future School",
                month="2026-07",
                meter_topology_profile="dual_meter_school",
            )
            _confirm_draft_pricing(root, draft)
            generation = _write_pdf(
                root / "opaque-a.pdf",
                "发电单 公司名称:深圳测试电力有限公司 电表编号:MT000001 倍率:1 电表编号:MT000002 倍率:1",
            )
            fee = _write_pdf(root / "opaque-b.pdf", "电费结算单 公司名称:深圳测试电力有限公司 上网电费 结算小计")
            energy = _write_pdf(root / "opaque-c.pdf", "电量结算单 公司名称:深圳测试电力有限公司 上网电量 电量结算")

            payload = collect_site_registration_draft_pdf_batch(root, f"draft:{draft['draft_id']}", [generation, fee, energy])

            self.assertEqual(payload["batch_state"], "ready")
            self.assertEqual(len(payload["classified_files"]), 3)
            self.assertEqual({item["file_type"] for item in payload["classified_files"]}, {"generation_statement", "fee_statement", "energy_statement"})
            review = payload["metadata_review"]
            self.assertEqual(review["resolved_metadata"]["company_name"], "深圳测试电力有限公司")
            self.assertEqual(len(review["resolved_metadata"]["meters"]), 2)
            self.assertEqual(
                [item["key"] for item in review["missing_fields"]],
                ["project_name", "grid_export_recipient_meter"],
            )

    def test_recollecting_identical_materials_persists_reclassification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = prepare_site_registration_draft(
                root,
                conversation_key="trusted-session-reclassify",
                site_name="Future School",
                month="2026-07",
                meter_topology_profile="single_meter_school",
            )
            _confirm_draft_pricing(root, draft)
            sources = [
                _write_pdf(root / "generation.pdf", "发电单 电表编号:MT000001 倍率:1"),
                _write_pdf(root / "fee.pdf", "电费结算单 上网电费 结算小计"),
                _write_pdf(root / "energy.pdf", "电量结算单 上网电量 电量结算"),
            ]
            collect_site_registration_draft_pdf_batch(root, str(draft["draft_id"]), sources)
            draft_path = root / "site_registration_drafts" / str(draft["draft_id"]) / "draft.json"
            stored = json.loads(draft_path.read_text(encoding="utf-8"))
            stored["pdf_intake"]["batch_state"] = "need_review"
            draft_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

            payload = collect_site_registration_draft_pdf_batch(root, str(draft["draft_id"]), sources)
            persisted = json.loads(draft_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["batch_state"], "ready")
            self.assertEqual(persisted["pdf_intake"]["batch_state"], "ready")

    def test_finalize_fails_closed_without_required_confirmed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = _ready_single_meter_draft(root)

            with patch("submit_flow_agent.site_registration_drafts.auto_register_site_from_profile") as register:
                with self.assertRaises(SiteRegistrationDraftError):
                    finalize_site_registration_draft(root, draft["draft_id"], metadata={})

            register.assert_not_called()

    def test_finalize_routes_through_controlled_registration_pricing_task_and_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = _ready_single_meter_draft(root)
            metadata = {
                "project_name": "Future School PV Project",
                "company_name": "深圳测试电力有限公司",
                "meters": [{"meter_no": "MT000001", "multiplier": "1"}],
            }
            snapshot = {"pricing_version": 1, "pricing_source": "operator_confirmed"}

            with patch(
                "submit_flow_agent.site_registration_drafts.auto_register_site_from_profile",
                return_value={"status": "registered", "site_key": "future_school"},
            ) as register, patch(
                "submit_flow_agent.site_registration_drafts._commit_confirmed_draft_pricing",
                return_value=snapshot,
            ) as pricing, patch(
                "submit_flow_agent.task_service.resolve_task",
                return_value={"task_id": "pv_future_school_2026_07_r0001"},
            ) as resolve, patch(
                "submit_flow_agent.task_service.collect_task_pdf_batch",
                return_value={
                    "task_id": "pv_future_school_2026_07_r0001",
                    "site_key": "future_school",
                    "month": "2026-07",
                    "task_status": "ready_to_run",
                    "batch_state": "ready",
                    "received_count": 3,
                    "required_count": 3,
                    "classified_files": [
                        {"file_type": "generation_statement", "original_filename": "generation.pdf"},
                        {"file_type": "fee_statement", "original_filename": "fee.pdf"},
                        {"file_type": "energy_statement", "original_filename": "energy.pdf"},
                    ],
                },
            ) as collect:
                payload = finalize_site_registration_draft(
                    root,
                    draft["draft_id"],
                    metadata=metadata,
                )

            self.assertEqual(payload["status"], "finalized")
            register.assert_called_once()
            pricing.assert_called_once()
            resolve.assert_called_once()
            collect.assert_called_once()
            self.assertEqual(payload["formal_task"]["task_status"], "ready_to_run")
            self.assertEqual(payload["formal_task"]["pdf_batch_state"], "ready")
            self.assertEqual(payload["formal_task"]["carried_file_state"], "accepted_batch_carried")
            self.assertEqual(payload["formal_task"]["next_allowed_action"], "run_task")
            self.assertEqual(payload["task_pdf_batch"]["carried_file_state"], "accepted_batch_carried")

    def test_coordinate_meter_pairing_uses_page_scoped_x_anchors_not_column_indexes(self) -> None:
        record = {"role": "generation_statement", "stored_file": "inputs/generation.pdf", "classification_source": "ocr"}
        blocks = [
            _ocr_block("电表编号", 1, 95, 10, 125, 25),
            _ocr_block("倍率", 1, 195, 10, 225, 25),
            _ocr_block("extra", 1, 45, 40, 75, 55),
            _ocr_block("MT000001", 1, 96, 40, 126, 55),
            _ocr_block("1", 1, 196, 40, 226, 55),
            _ocr_block("MT999999", 2, 96, 12, 126, 27),
            _ocr_block("9", 2, 196, 12, 226, 27),
        ]

        candidates = _coordinate_table_meter_candidates(blocks, record)

        self.assertEqual([(item["meter_no"], item["multiplier"]) for item in candidates], [("MT000001", "1")])
        self.assertIn("page 1", candidates[0]["evidence"])

    def test_coordinate_meter_pairing_rejects_ambiguous_or_missing_cells(self) -> None:
        record = {"role": "generation_statement", "stored_file": "inputs/generation.pdf", "classification_source": "ocr"}
        ambiguous = [
            _ocr_block("电表编号", 1, 95, 10, 125, 25),
            _ocr_block("倍率", 1, 195, 10, 225, 25),
            _ocr_block("MT000001", 1, 84, 40, 114, 55),
            _ocr_block("MT000002", 1, 106, 40, 136, 55),
            _ocr_block("1", 1, 196, 40, 226, 55),
        ]
        missing_multiplier = [
            _ocr_block("电表编号", 1, 95, 10, 125, 25),
            _ocr_block("倍率", 1, 195, 10, 225, 25),
            _ocr_block("MT000001", 1, 96, 40, 126, 55),
            _ocr_block("1", 1, 240, 40, 270, 55),
        ]

        self.assertEqual(_coordinate_table_meter_candidates(ambiguous, record), [])
        self.assertEqual(_coordinate_table_meter_candidates(missing_multiplier, record), [])

    def test_scanned_generation_layout_recovers_wrapped_asset_numbers_and_school_project(self) -> None:
        generation = _metadata_record(
            "generation_statement",
            "尊敬的深圳市华盛腾新能源科技有限公司用户，您好！\n"
            "深圳市华盛腾新能源科技有限公司\n结算月份:2026年5月",
        )
        generation["classification_source"] = "ocr"
        generation["blocks"] = [
            _ocr_block("项目名称", 1, 1026, 468, 1167, 510),
            _ocr_block("深圳市华盛腾新能源科技有限公司", 1, 1563, 472, 2061, 506),
            _ocr_block("联系电话", 1, 2457, 468, 2597, 511),
            _ocr_block("深圳市沙井中学", 1, 653, 673, 890, 710),
            _ocr_block("项目类别", 1, 371, 718, 511, 761),
            _ocr_block("980Kwp分布式屋顶光", 1, 603, 718, 943, 758),
            _ocr_block("伏项目", 1, 716, 764, 824, 808),
            _ocr_block("资产编号", 1, 701, 1198, 843, 1241),
            _ocr_block("倍率", 1, 2202, 1196, 2281, 1244),
            _ocr_block("09001SF000000425", 1, 598, 1259, 944, 1293),
            _ocr_block("200", 1, 2202, 1279, 2280, 1322),
            _ocr_block("08945245", 1, 680, 1305, 860, 1339),
            _ocr_block("09001SF000000425", 1, 599, 1357, 943, 1388),
            _ocr_block("150", 1, 2203, 1377, 2280, 1418),
            _ocr_block("08945244", 1, 681, 1404, 861, 1435),
        ]

        review = _extract_metadata_review([generation], "dual_meter_school", "2026-05")

        self.assertEqual(
            review["resolved_metadata"]["project_name"],
            "深圳市沙井中学980Kwp分布式屋顶光伏项目",
        )
        self.assertEqual(review["resolved_metadata"]["company_name"], "深圳市华盛腾新能源科技有限公司")
        self.assertEqual(
            [(item["meter_no"], item["multiplier"]) for item in review["resolved_metadata"]["meters"]],
            [
                ("09001SF00000042508945245", "200"),
                ("09001SF00000042508945244", "150"),
            ],
        )

    def test_metadata_unresolved_after_high_role_triggers_bounded_ocr_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "inputs").mkdir()
            (root / "inputs" / "generation.pdf").write_text("pdf", encoding="utf-8")
            records = [
                {
                    "stored_file": "inputs/generation.pdf",
                    "original_filename": "generation.pdf",
                    "role": "generation_statement",
                    "classification_source": "pdf_text",
                    "confidence": "high",
                    "text": "发电单 公司名称:深圳测试电力有限公司",
                    "blocks": [],
                }
            ]
            role_results = [{"role": "generation_statement", "confidence": "high"}]
            review = _extract_metadata_review(records, "single_meter_school", "2026-07")

            with patch(
                "submit_flow_agent.site_registration_drafts._extract_ocr_text_and_blocks",
                return_value=(
                    "项目名称:Future School 项目地址:深圳 本期抄表时间 2026.07.01",
                    "metadata_fallback",
                    [
                        _ocr_block("电表编号", 1, 95, 10, 125, 25),
                        _ocr_block("倍率", 1, 195, 10, 225, 25),
                        _ocr_block("MT000001", 1, 96, 40, 126, 55),
                        _ocr_block("1", 1, 196, 40, 226, 55),
                    ],
                ),
            ):
                changed = _apply_metadata_ocr_fallback(root, records, role_results)

            updated = _extract_metadata_review(records, "single_meter_school", "2026-07")

        self.assertTrue(changed)
        self.assertEqual(role_results[0]["ocr_status"], "metadata_fallback")
        self.assertIn("meters", [item["key"] for item in review["unresolved_fields"]["missing"]])
        self.assertEqual(updated["resolved_metadata"]["meters"][0]["meter_no"], "MT000001")

    def test_role_aware_metadata_ocr_recovers_project_company_month_from_fee_statement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "inputs").mkdir()
            (root / "inputs" / "generation.pdf").write_text("generation", encoding="utf-8")
            (root / "inputs" / "fee.pdf").write_text("fee", encoding="utf-8")
            records = [
                {
                    "stored_file": "inputs/generation.pdf",
                    "original_filename": "generation.pdf",
                    "role": "generation_statement",
                    "classification_source": "pdf_text",
                    "confidence": "high",
                    "text": "",
                    "blocks": [
                        _ocr_block("电表编号", 1, 95, 10, 125, 25),
                        _ocr_block("倍率", 1, 195, 10, 225, 25),
                        _ocr_block("MT000001", 1, 96, 40, 126, 55),
                        _ocr_block("1", 1, 196, 40, 226, 55),
                    ],
                },
                {
                    "stored_file": "inputs/fee.pdf",
                    "original_filename": "fee.pdf",
                    "role": "fee_statement",
                    "classification_source": "pdf_text",
                    "confidence": "high",
                    "text": "电费结算单",
                    "blocks": [],
                },
            ]
            role_results = [
                {"role": "generation_statement", "confidence": "high"},
                {"role": "fee_statement", "confidence": "high"},
            ]
            review = _extract_metadata_review(records, "single_meter_school", "2026-07")
            ocr_sources: list[str] = []

            def fee_ocr(root_arg: Path, source: Path) -> tuple[str, str, list[dict[str, object]]]:
                ocr_sources.append(source.name)
                return (
                    "项目名称:Future School PV Project 项目地址:深圳 "
                    "公司名称:深圳测试电力有限公司 结算月份:2026年7月",
                    "metadata_fallback",
                    [_ocr_block("项目名称", 1, 10, 10, 40, 25)],
                )

            with patch("submit_flow_agent.site_registration_drafts._extract_ocr_text_and_blocks", side_effect=fee_ocr):
                changed = _apply_metadata_ocr_fallback(root, records, role_results, _metadata_unresolved_keys(review))

            updated = _extract_metadata_review(records, "single_meter_school", "2026-07")

        self.assertTrue(changed)
        self.assertEqual(ocr_sources, ["fee.pdf"])
        self.assertNotIn("ocr_status", role_results[0])
        self.assertEqual(role_results[1]["ocr_status"], "metadata_fallback")
        self.assertEqual(updated["resolved_metadata"]["project_name"], "FutureSchoolPVProject")
        self.assertEqual(updated["resolved_metadata"]["company_name"], "深圳测试电力有限公司")
        self.assertEqual(updated["month_review"]["status"], "matched")

    def test_pdf_first_high_confidence_single_meter_metadata_is_resolved_without_repeat_input(self) -> None:
        records = [
            _metadata_record(
                "generation_statement",
                "发电单\n项目名称:Future School PV Project 项目地址:深圳\n公司名称:深圳测试电力有限公司\n"
                "电表编号:MT000001 倍率:1 学校用电\n结算月份:2026年7月",
            )
        ]

        review = _extract_metadata_review(records, "single_meter_school", "2026-07")

        self.assertTrue(review["ready_for_registration"])
        self.assertEqual(review["resolved_metadata"]["project_name"], "FutureSchoolPVProject")
        self.assertEqual(review["resolved_metadata"]["company_name"], "深圳测试电力有限公司")
        self.assertEqual(
            review["resolved_metadata"]["meters"],
            [
                {
                    "meter_no": "MT000001",
                    "multiplier": "1",
                    "source": review["resolved_metadata"]["meters"][0]["source"],
                    "confidence": "high",
                    "evidence": "电表编号:MT000001 倍率:1 学校用电",
                    "detail_evidence": True,
                    "grid_export_evidence": False,
                    "detail_enabled": True,
                    "grid_export_allocation": "site_total",
                }
            ],
        )
        self.assertEqual(review["allowed_metadata_keys"], [])
        self.assertEqual(review["unresolved_fields"], {"missing": [], "conflicting": [], "low_confidence": []})

    def test_conflicting_or_low_confidence_project_names_stay_unresolved(self) -> None:
        conflicting = _extract_metadata_review(
            [
                _metadata_record("generation_statement", "项目名称:Alpha Project 项目地址:深圳\n结算月份:2026年7月"),
                _metadata_record("fee_statement", "项目名称:Beta Project 项目地址:深圳"),
            ],
            "single_meter_school",
            "2026-07",
        )
        low_confidence = _extract_metadata_review(
            [
                _metadata_record(
                    "generation_statement",
                    "项目名称:Future School PV Project 项目地址:深圳\n结算月份:2026年7月",
                    confidence="low",
                )
            ],
            "single_meter_school",
            "2026-07",
        )

        self.assertNotIn("project_name", conflicting["resolved_metadata"])
        self.assertIn("project_name", [item["key"] for item in conflicting["unresolved_fields"]["conflicting"]])
        self.assertIn("project_name", conflicting["allowed_metadata_keys"])
        self.assertNotIn("project_name", low_confidence["resolved_metadata"])
        self.assertIn("project_name", [item["key"] for item in low_confidence["unresolved_fields"]["low_confidence"]])
        self.assertIn("project_name", low_confidence["allowed_metadata_keys"])

    def test_coordinate_project_name_joins_wrapped_value_and_rejects_adjacent_header(self) -> None:
        record = _metadata_record(
            "generation_statement",
            "分布式光伏发电补贴核算单\n项目名称\n项目地址\n结算月份:2026年7月",
        )
        record["blocks"] = [
            _ocr_block("项目名称", 1, 131, 182, 239, 217),
            _ocr_block("未来学校271kw分布式屋", 1, 266, 167, 811, 197),
            _ocr_block("项目地址", 1, 866, 181, 974, 216),
            _ocr_block("顶光伏项目", 1, 474, 199, 601, 230),
        ]

        review = _extract_metadata_review([record], "dual_meter_school", "2026-07")

        self.assertEqual(review["resolved_metadata"]["project_name"], "未来学校271kw分布式屋顶光伏项目")
        self.assertNotEqual(review["resolved_metadata"]["project_name"], "项目地址")

    def test_project_header_without_value_is_not_accepted_as_project_name(self) -> None:
        review = _extract_metadata_review(
            [_metadata_record("generation_statement", "项目名称 项目地址\n结算月份:2026年7月")],
            "single_meter_school",
            "2026-07",
        )

        self.assertNotIn("project_name", review["resolved_metadata"])
        self.assertIn("project_name", review["allowed_metadata_keys"])

    def test_project_name_ignores_numeric_noise_and_stops_at_plant_account_header(self) -> None:
        generation = _metadata_record(
            "generation_statement",
            "项目名称\n深圳市宝安兴围小学300kwp分布式屋顶光伏项目\n电厂户编号\n结算月份:2026年7月",
        )
        generation["blocks"] = [
            _ocr_block("项目名称", 1, 131, 182, 239, 217),
            _ocr_block("深圳市宝安兴围小学300kwp分布式屋顶光伏项目", 1, 266, 167, 811, 197),
            _ocr_block("电厂户编号", 1, 866, 181, 1010, 216),
        ]
        fee = _metadata_record("fee_statement", "项目名称\n12439\n结算月份:2026年7月")

        review = _extract_metadata_review([generation, fee], "single_meter_school", "2026-07")

        self.assertEqual(
            review["resolved_metadata"]["project_name"],
            "深圳市宝安兴围小学300kwp分布式屋顶光伏项目",
        )
        self.assertNotIn("project_name", [item["key"] for item in review["unresolved_fields"]["conflicting"]])

    def test_pdf_first_dual_meter_role_evidence_uniquely_resolves_both_roles(self) -> None:
        records = [
            _metadata_record(
                "generation_statement",
                "项目名称:Dual Meter PV Project 项目地址:深圳\n公司名称:深圳测试电力有限公司\n"
                "电表编号:MT000001 倍率:1 学校用电\n"
                "电表编号:MT000002 倍率:1 上网电量\n"
                "结算月份:2026年7月",
            )
        ]

        review = _extract_metadata_review(records, "dual_meter_school", "2026-07")
        meters = review["resolved_metadata"]["meters"]

        self.assertTrue(review["ready_for_registration"])
        self.assertEqual([meter["meter_no"] for meter in meters], ["MT000001", "MT000002"])
        self.assertEqual([meter["detail_enabled"] for meter in meters], [True, True])
        self.assertEqual([meter["grid_export_allocation"] for meter in meters], ["none", "site_total"])
        self.assertNotIn("detail_meter", review["allowed_metadata_keys"])
        self.assertNotIn("grid_export_recipient_meter", review["allowed_metadata_keys"])

    def test_ambiguous_dual_meter_detail_evidence_still_enables_both_detail_rows(self) -> None:
        records = [
            _metadata_record(
                "generation_statement",
                "项目名称:Dual Meter PV Project 项目地址:深圳\n公司名称:深圳测试电力有限公司\n"
                "电表编号:MT000001 倍率:1 学校用电 上网电量\n"
                "电表编号:MT000002 倍率:1 学校用电\n"
                "结算月份:2026年7月",
            )
        ]

        review = _extract_metadata_review(records, "dual_meter_school", "2026-07")
        meters = review["resolved_metadata"]["meters"]

        self.assertTrue(review["ready_for_registration"])
        self.assertNotIn("detail_meter", [item["key"] for item in review["unresolved_fields"]["missing"]])
        self.assertNotIn("grid_export_recipient_meter", [item["key"] for item in review["unresolved_fields"]["missing"]])
        self.assertEqual(review["allowed_metadata_keys"], [])
        self.assertEqual([meter["detail_enabled"] for meter in meters], [True, True])
        self.assertEqual([meter["grid_export_allocation"] for meter in meters], ["site_total", "none"])


def _ready_single_meter_draft(root: Path) -> dict[str, object]:
    draft = prepare_site_registration_draft(
        root,
        conversation_key="trusted-session-3",
        site_name="Future School",
        month="2026-07",
        meter_topology_profile="single_meter_school",
    )
    _confirm_draft_pricing(root, draft)
    generation = _write_pdf(root / "generation.pdf", "发电单 公司名称:深圳测试电力有限公司 电表编号:MT000001 倍率:1")
    fee = _write_pdf(root / "fee.pdf", "电费结算单 公司名称:深圳测试电力有限公司 上网电费 结算小计")
    energy = _write_pdf(root / "energy.pdf", "电量结算单 公司名称:深圳测试电力有限公司 上网电量 电量结算")
    collect_site_registration_draft_pdf_batch(root, f"draft:{draft['draft_id']}", [generation, fee, energy])
    return draft


def _confirm_draft_pricing(root: Path, draft: dict[str, object]) -> None:
    proposal = prepare_site_registration_draft_pricing(
        root,
        str(draft["draft_id"]),
        pricing={"discount_rate": "0.95", "effective_unit_price": "0.6998"},
    )
    pricing_state = proposal["pricing_state"]
    confirm_site_registration_draft_pricing(
        root,
        str(draft["draft_id"]),
        proposal_id=str(pricing_state["proposal_id"]),
    )


def _write_pdf(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _metadata_record(role: str, text: str, *, confidence: str = "high") -> dict[str, object]:
    return {
        "stored_file": f"inputs/{role}.pdf",
        "original_filename": f"{role}.pdf",
        "role": role,
        "classification_source": "pdf_text",
        "confidence": confidence,
        "text": text,
        "blocks": [],
    }


def _ocr_block(text: str, page: int, x0: float, y0: float, x1: float, y1: float) -> dict[str, object]:
    return {"text": text, "page": page, "bbox": [x0, y0, x1, y1], "confidence": 0.99}


if __name__ == "__main__":
    unittest.main()
