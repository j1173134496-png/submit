"""Command line entrypoint for the local MVP pipeline."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from submit_flow_agent.config import ConfigError, SiteConfig, list_site_keys, load_site_config
from submit_flow_agent.excel_writer import ExcelWriteError, write_detail_excel, write_summary_excel
from submit_flow_agent.field_mapper import FieldMappingError, map_fields_to_file
from submit_flow_agent.historical_backfill import HistoricalBackfillError, backfill_school_history
from submit_flow_agent.portfolio_historical_backfill import (
    PortfolioHistoricalBackfillError,
    backfill_portfolio_history,
)
from submit_flow_agent.file_scanner import FileScanError, scan_input_files
from submit_flow_agent.factory_settlement import (
    FactorySettlementError,
    collect_factory_settlement_source,
    confirm_factory_settlement,
    confirm_factory_settlement_price,
    get_factory_settlement,
    list_factory_settlement_outputs,
    prepare_factory_settlement,
    run_factory_settlement,
    update_factory_settlement_price,
)
from submit_flow_agent.monthly_regression import MonthlyRegressionError, run_monthly_regression
from submit_flow_agent.monthly_summary import (
    MonthlySummaryError,
    collect_monthly_summary_source,
    confirm_monthly_summary,
    get_monthly_summary,
    list_monthly_summary_outputs,
    prepare_monthly_summary,
    run_monthly_summary,
    update_monthly_summary,
)
from submit_flow_agent.huizhou_zexin import (
    HuizhouZexinError,
    collect_huizhou_zexin_source,
    confirm_huizhou_zexin,
    get_huizhou_zexin,
    list_huizhou_zexin_outputs,
    prepare_huizhou_zexin,
    record_huizhou_zexin_generation,
    run_huizhou_zexin,
    update_huizhou_zexin,
)
from submit_flow_agent.yangjiang_hongneng import (
    YangjiangHongnengError,
    collect_yangjiang_hongneng_source,
    confirm_yangjiang_hongneng,
    get_yangjiang_hongneng,
    list_yangjiang_hongneng_outputs,
    prepare_yangjiang_hongneng,
    run_yangjiang_hongneng,
)
from submit_flow_agent.nanke import (
    NankeError,
    collect_nanke_source,
    confirm_nanke,
    get_nanke,
    list_nanke_outputs,
    prepare_nanke,
    run_nanke,
)
from submit_flow_agent.normalizer import NormalizationError
from submit_flow_agent.pipeline import PipelineError, run_month
from submit_flow_agent.pricing import PricingError, snapshot_from_json
from submit_flow_agent.site_registration_drafts import (
    SiteRegistrationDraftError,
    cancel_site_registration_draft,
    collect_site_registration_draft_pdf_batch,
    finalize_site_registration_draft,
    is_draft_identity,
    prepare_site_registration_draft,
    prepare_site_registration_draft_pricing,
    update_site_registration_draft_pricing,
    confirm_site_registration_draft_pricing,
    restart_site_registration_draft,
)
from submit_flow_agent.site_package import (
    SitePackageError,
    inspect_site_package,
    list_registered_sites,
    register_site_package,
    validate_site_package,
)
from submit_flow_agent.template_profiles import (
    TemplateProfileError,
    auto_register_site_from_profile,
    list_template_profiles,
    register_site_from_profile,
)
from submit_flow_agent.task_service import (
    TaskServiceError,
    add_task_file,
    cancel_task,
    collect_task_pdf_batch,
    confirm_task,
    propose_task_pricing,
    confirm_task_pricing,
    create_task,
    get_task,
    list_task_outputs,
    retry_failed_task,
    retry_missing_field_review,
    retry_review_task,
    retry_profile_exempt_review,
    resolve_task,
    resolve_task_state,
    run_task,
)
from submit_flow_agent.task_store import TaskStoreError
from submit_flow_agent.validator import validate_data_to_file
from submit_flow_agent.worker_contract import (
    WorkerContractError,
    load_json,
    validate_worker_result,
    validate_worker_task,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "sites":
            for site_key in list_site_keys(args.config):
                print(site_key)
            return 0

        if args.command == "list-sites":
            print(json.dumps(list_registered_sites(args.config), ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-site":
            site = load_site_config(args.site_key, args.config)
            payload = _site_config_payload(site)
            if args.month:
                payload["output_dir"] = str(site.output_dir(args.month, args.output_base))
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
            return 0

        if args.command == "inspect-site-package":
            print(json.dumps(inspect_site_package(args.package_dir), ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate-site-package":
            print(json.dumps(validate_site_package(args.package_dir, config_path=args.config), ensure_ascii=False, indent=2))
            return 0

        if args.command == "register-site":
            print(json.dumps(register_site_package(args.package_dir, config_path=args.config), ensure_ascii=False, indent=2))
            return 0


        if args.command == "list-template-profiles":
            print(json.dumps(list_template_profiles(args.profiles), ensure_ascii=False, indent=2))
            return 0

        if args.command == "auto-register-site":
            payload = auto_register_site_from_profile(
                site_name=args.site_name,
                profile_key=args.profile_key,
                meter_topology_profile=args.meter_topology_profile,
                aliases=args.alias or [],
                metadata=_metadata_json(args.metadata_json),
                config_path=args.config,
                profiles_path=args.profiles,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "register-site-from-profile":
            payload = register_site_from_profile(
                site_name=args.site_name,
                profile_key=args.profile_key,
                aliases=args.alias or [],
                metadata=_metadata_json(args.metadata_json),
                config_path=args.config,
                profiles_path=args.profiles,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "scan-inputs":
            result = scan_input_files(args.project_dir)
            print(json.dumps(result.to_dict(relative_to=args.project_dir), ensure_ascii=False, indent=2))
            return 0

        if args.command == "map-fields":
            recognized_data = map_fields_to_file(
                args.project_dir,
                args.output,
                site_key=args.site_key,
                config_path=args.config,
                ocr_raw_dir=args.ocr_raw_dir,
            )
            payload = {
                "month": recognized_data.month,
                "output": str(args.output),
                "field_count": len(recognized_data.fields),
                "mapping_mode": recognized_data.mapping_mode,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate-data":
            report = validate_data_to_file(args.recognized_data, args.output)
            payload = {
                "month": report.month,
                "status": report.status,
                "can_write_excel": report.can_write_excel,
                "error_count": len(report.errors),
                "warning_count": len(report.warnings),
                "output": str(args.output),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "write-detail-excel":
            result = write_detail_excel(
                args.validation_report,
                args.project_dir,
                args.output,
                site_key=args.site_key,
                config_path=args.config,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "write-summary-excel":
            result = write_summary_excel(
                args.validation_report,
                args.project_dir,
                args.output,
                site_key=args.site_key,
                config_path=args.config,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-month":
            result = run_month(
                args.project_dir,
                site_key=args.site_key,
                output_base=args.output_base,
                config_path=args.config,
                confirmed_data=args.confirmed_data,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-monthly-regression":
            result = run_monthly_regression(
                args.manifest,
                output_base=args.output_base,
                matrix_path=args.matrix,
                config_path=args.config,
                execute=not args.dry_run,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0


        if args.command in {"propose-task-pricing", "prepare-task-pricing", "update-task-pricing"}:
            payload = propose_task_pricing(
                args.runtime_root,
                site_key=args.site_key,
                month=args.month,
                pricing=snapshot_from_json(args.pricing_json) or {},
                config_path=args.config,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-task-pricing":
            payload = confirm_task_pricing(
                args.runtime_root,
                proposal_id=args.proposal_id,
                config_path=args.config,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0


        if args.command == "prepare-site-registration-draft":
            payload = prepare_site_registration_draft(
                args.runtime_root,
                conversation_key=args.conversation_key,
                site_name=args.site_name,
                month=args.month,
                meter_topology_profile=args.meter_topology_profile,
                recreate_cancelled=args.recreate_cancelled,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "prepare-site-registration-draft-pricing":
            payload = prepare_site_registration_draft_pricing(
                args.runtime_root,
                args.draft_id,
                pricing=snapshot_from_json(args.pricing_json) or {},
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "update-site-registration-draft-pricing":
            payload = update_site_registration_draft_pricing(
                args.runtime_root,
                args.draft_id,
                pricing=snapshot_from_json(args.pricing_json) or {},
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-site-registration-draft-pricing":
            payload = confirm_site_registration_draft_pricing(
                args.runtime_root,
                args.draft_id,
                proposal_id=args.proposal_id,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "finalize-site-registration-draft":
            payload = finalize_site_registration_draft(
                args.runtime_root,
                args.draft_id,
                aliases=args.alias or [],
                metadata=_metadata_json(args.metadata_json),
                pricing_proposal_id=args.pricing_proposal_id,
                pricing_snapshot=snapshot_from_json(args.pricing_snapshot_json),
                pricing=snapshot_from_json(args.pricing_json),
                config_path=args.config,
                profiles_path=args.profiles,
                task_mode=args.mode,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "cancel-site-registration-draft":
            payload = cancel_site_registration_draft(args.runtime_root, args.draft_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "restart-site-registration-draft":
            payload = restart_site_registration_draft(args.runtime_root, args.draft_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "resolve-task-state":
            payload = resolve_task_state(
                args.runtime_root,
                site_key=args.site_key,
                month=args.month,
                config_path=args.config,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "resolve-task":
            payload = resolve_task(
                args.runtime_root,
                site_key=args.site_key,
                month=args.month,
                mode=args.mode,
                config_path=args.config,
                pricing_snapshot=snapshot_from_json(args.pricing_snapshot_json),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "create-task":
            task = create_task(
                args.runtime_root,
                task_id=args.task_id,
                site_key=args.site_key,
                month=args.month,
                config_path=args.config,
                pricing_snapshot=snapshot_from_json(args.pricing_snapshot_json),
            )
            print(json.dumps(task, ensure_ascii=False, indent=2))
            return 0

        if args.command == "add-file":
            task = add_task_file(args.runtime_root, args.task_id, args.file, file_type=args.file_type, config_path=args.config, site_key=args.site_key, month=args.month)
            print(json.dumps(task, ensure_ascii=False, indent=2))
            return 0

        if args.command == "collect-pdf-batch":
            if is_draft_identity(args.task_id):
                payload = collect_site_registration_draft_pdf_batch(
                    args.runtime_root,
                    args.task_id,
                    [args.pdf1, args.pdf2, args.pdf3],
                )
            else:
                payload = collect_task_pdf_batch(
                    args.runtime_root,
                    args.task_id,
                    [args.pdf1, args.pdf2, args.pdf3],
                    config_path=args.config,
                    site_key=args.site_key,
                    month=args.month,
                )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-task":
            result = run_task(args.runtime_root, args.task_id, config_path=args.config, site_key=args.site_key, month=args.month)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-task":
            result = confirm_task(args.runtime_root, args.task_id, args.confirmed_data, config_path=args.config)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "retry-profile-exempt-review":
            result = retry_profile_exempt_review(args.runtime_root, args.task_id, config_path=args.config)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "backfill-school-history":
            result = backfill_school_history(
                args.source,
                site_key=args.site_key,
                through_month=args.through_month,
                runtime_root=args.runtime_root,
                config_path=args.config,
                sheet_name=args.sheet,
                verified_site_key=args.verified_site_key,
                route_path=args.route_path,
                apply=args.apply,
                replace_existing=args.replace_existing,
                rehearsal_root=args.rehearsal_root,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "backfill-portfolio-history":
            result = backfill_portfolio_history(
                args.source_dir,
                runtime_root=args.runtime_root,
                config_path=args.config,
                apply=args.apply,
                rehearsal_root=args.rehearsal_root,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "prepare-monthly-summary":
            payload = prepare_monthly_summary(
                args.runtime_root,
                task_id=args.task_id,
                month=args.month,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "collect-monthly-summary-source":
            payload = collect_monthly_summary_source(args.runtime_root, args.task_id, args.source)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "update-monthly-summary":
            payload = update_monthly_summary(args.runtime_root, args.task_id, _metadata_json(args.values_json))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-monthly-summary":
            payload = confirm_monthly_summary(
                args.runtime_root,
                args.task_id,
                _metadata_json(args.values_json) if args.values_json else None,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-monthly-summary":
            payload = run_monthly_summary(
                args.runtime_root,
                args.task_id,
                template_path=args.template,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-monthly-summary":
            print(json.dumps(get_monthly_summary(args.runtime_root, args.task_id), ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-monthly-summary-outputs":
            outputs = list_monthly_summary_outputs(args.runtime_root, args.task_id)
            print(json.dumps({"task_id": args.task_id, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "prepare-huizhou-zexin":
            payload = prepare_huizhou_zexin(args.runtime_root, task_id=args.task_id, month=args.month)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "collect-huizhou-zexin-source":
            payload = collect_huizhou_zexin_source(args.runtime_root, args.task_id, args.source)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "record-huizhou-zexin-generation":
            payload = record_huizhou_zexin_generation(
                args.runtime_root,
                args.task_id,
                month=args.month,
                total_generation=args.total_generation,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "update-huizhou-zexin":
            payload = update_huizhou_zexin(args.runtime_root, args.task_id, _metadata_json(args.values_json))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-huizhou-zexin":
            payload = confirm_huizhou_zexin(
                args.runtime_root,
                args.task_id,
                _metadata_json(args.values_json) if args.values_json else None,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-huizhou-zexin":
            payload = run_huizhou_zexin(args.runtime_root, args.task_id, template_dir=args.template_dir)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-huizhou-zexin":
            print(json.dumps(get_huizhou_zexin(args.runtime_root, args.task_id), ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-huizhou-zexin-outputs":
            outputs = list_huizhou_zexin_outputs(args.runtime_root, args.task_id)
            print(json.dumps({"task_id": args.task_id, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "prepare-yangjiang-hongneng":
            payload = prepare_yangjiang_hongneng(args.runtime_root, task_id=args.task_id, month=args.month)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "collect-yangjiang-hongneng-source":
            payload = collect_yangjiang_hongneng_source(args.runtime_root, args.task_id, args.source)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-yangjiang-hongneng":
            payload = confirm_yangjiang_hongneng(args.runtime_root, args.task_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-yangjiang-hongneng":
            payload = run_yangjiang_hongneng(args.runtime_root, args.task_id, template_dir=args.template_dir)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-yangjiang-hongneng":
            print(json.dumps(get_yangjiang_hongneng(args.runtime_root, args.task_id), ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-yangjiang-hongneng-outputs":
            outputs = list_yangjiang_hongneng_outputs(args.runtime_root, args.task_id)
            print(json.dumps({"task_id": args.task_id, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "prepare-nanke":
            payload = prepare_nanke(args.runtime_root, task_id=args.task_id, month=args.month)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "collect-nanke-source":
            payload = collect_nanke_source(args.runtime_root, args.task_id, args.source)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-nanke":
            payload = confirm_nanke(args.runtime_root, args.task_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-nanke":
            payload = run_nanke(args.runtime_root, args.task_id, template_dir=args.template_dir)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-nanke":
            print(json.dumps(get_nanke(args.runtime_root, args.task_id), ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-nanke-outputs":
            outputs = list_nanke_outputs(args.runtime_root, args.task_id)
            print(json.dumps({"task_id": args.task_id, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "prepare-factory-settlement":
            payload = prepare_factory_settlement(
                args.runtime_root,
                task_id=args.task_id,
                site_name=args.site_name,
                month=args.month,
                allow_create=args.allow_create,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "update-factory-settlement-price":
            payload = update_factory_settlement_price(
                args.runtime_root,
                args.task_id,
                grid_unit_price=args.grid_unit_price,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-factory-settlement-price":
            payload = confirm_factory_settlement_price(args.runtime_root, args.task_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "collect-factory-settlement-source":
            payload = collect_factory_settlement_source(args.runtime_root, args.task_id, args.source)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "confirm-factory-settlement":
            payload = confirm_factory_settlement(args.runtime_root, args.task_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-factory-settlement":
            payload = run_factory_settlement(
                args.runtime_root,
                args.task_id,
                template_path=args.template,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-factory-settlement":
            print(json.dumps(get_factory_settlement(args.runtime_root, args.task_id), ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-factory-settlement-outputs":
            outputs = list_factory_settlement_outputs(args.runtime_root, args.task_id)
            print(json.dumps({"task_id": args.task_id, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "retry-missing-field-review":
            result = retry_missing_field_review(args.runtime_root, args.task_id, config_path=args.config)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "retry-review-task":
            result = retry_review_task(args.runtime_root, args.task_id, config_path=args.config)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "retry-failed-task":
            result = retry_failed_task(args.runtime_root, args.task_id, config_path=args.config)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "cancel-task":
            task = cancel_task(args.runtime_root, args.task_id)
            print(json.dumps(task, ensure_ascii=False, indent=2))
            return 0

        if args.command == "show-task":
            task = get_task(args.runtime_root, args.task_id, site_key=args.site_key, month=args.month, config_path=args.config)
            print(json.dumps(task, ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-outputs":
            outputs = list_task_outputs(args.runtime_root, args.task_id, site_key=args.site_key, month=args.month, config_path=args.config)
            print(json.dumps({"task_id": args.task_id, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate-worker-task":
            payload = load_json(args.path)
            validate_worker_task(payload)
            print(json.dumps({"valid": True, "kind": "worker_task", "path": str(args.path)}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate-worker-result":
            payload = load_json(args.path)
            validate_worker_result(payload)
            print(json.dumps({"valid": True, "kind": "worker_result", "path": str(args.path)}, ensure_ascii=False, indent=2))
            return 0

        parser.print_help()
        return 0
    except ConfigError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    except FileScanError as exc:
        parser.exit(2, f"File scan error: {exc}\n")
    except FieldMappingError as exc:
        parser.exit(2, f"Field mapping error: {exc}\n")
    except NormalizationError as exc:
        parser.exit(2, f"Validation input error: {exc}\n")
    except ExcelWriteError as exc:
        parser.exit(2, f"Excel write error: {exc}\n")
    except PipelineError as exc:
        parser.exit(2, f"Pipeline error: {exc}\n")
    except MonthlyRegressionError as exc:
        parser.exit(2, f"Monthly regression error: {exc}\n")
    except HistoricalBackfillError as exc:
        parser.exit(2, f"Historical backfill error: {exc}\n")
    except PortfolioHistoricalBackfillError as exc:
        parser.exit(2, f"Portfolio historical backfill error: {exc}\n")
    except MonthlySummaryError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("monthly_summary_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Monthly summary error: {exc}\n")
    except HuizhouZexinError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("huizhou_zexin_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Huizhou Zexin error: {exc}\n")
    except YangjiangHongnengError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("yangjiang_hongneng_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Yangjiang Hongneng error: {exc}\n")
    except NankeError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("nanke_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Nanke error: {exc}\n")
    except FactorySettlementError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("factory_settlement_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Factory settlement error: {exc}\n")
    except SitePackageError as exc:
        parser.exit(2, f"Site package error: {exc}\n")
    except TemplateProfileError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("template_profile_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Template profile error: {exc}\n")
    except PricingError as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("pricing_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Pricing error: {exc}\n")
    except SiteRegistrationDraftError as exc:
        print(json.dumps(_safe_error_payload("site_registration_draft_error", exc), ensure_ascii=False, indent=2))
        return 2
    except (TaskStoreError, TaskServiceError) as exc:
        if _wants_json_error(args):
            print(json.dumps(_safe_error_payload("task_service_error", exc), ensure_ascii=False, indent=2))
            return 2
        parser.exit(2, f"Task service error: {exc}\n")
    except WorkerContractError as exc:
        parser.exit(2, f"Worker contract error: {exc}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="submit-flow-agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/sites.json"),
        help="Path to site configuration JSON.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("sites", help="List configured site keys.")

    subparsers.add_parser("list-sites", help="Print Gateway-readable site registry JSON.")

    show_site = subparsers.add_parser("show-site", help="Print one validated site config.")
    show_site.add_argument("--site-key", default="xinan_high_school")
    show_site.add_argument("--month", help="Optional YYYY-MM month for output directory preview.")
    show_site.add_argument("--output-base", default="outputs")

    inspect_package = subparsers.add_parser("inspect-site-package", help="Inspect a candidate site package without writing files.")
    inspect_package.add_argument("package_dir", type=Path)

    validate_package = subparsers.add_parser("validate-site-package", help="Validate a site package without registering it.")
    validate_package.add_argument("package_dir", type=Path)

    register_package = subparsers.add_parser("register-site", help="Atomically register a confirmed site package.")
    register_package.add_argument("package_dir", type=Path)


    profile_list = subparsers.add_parser("list-template-profiles", help="Print machine-readable template profiles without physical paths.")
    profile_list.add_argument("--profiles", type=Path, default=Path("config/template_profiles.json"))

    auto_register_profile = subparsers.add_parser("auto-register-site", help="Register or reuse a dynamic site from a controlled template profile.")
    auto_register_profile.add_argument("--site-name", required=True)
    auto_register_profile.add_argument("--profile-key")
    auto_register_profile.add_argument("--meter-topology-profile", choices=("single_meter_school", "dual_meter_school"))
    auto_register_profile.add_argument("--alias", action="append")
    auto_register_profile.add_argument("--metadata-json", default="{}")
    auto_register_profile.add_argument("--profiles", type=Path, default=Path("config/template_profiles.json"))

    register_profile = subparsers.add_parser("register-site-from-profile", help="Register a dynamic site from an allowlisted template profile.")
    register_profile.add_argument("--site-name", required=True)
    register_profile.add_argument("--profile-key", required=True)
    register_profile.add_argument("--alias", action="append")
    register_profile.add_argument("--metadata-json", required=True)
    register_profile.add_argument("--profiles", type=Path, default=Path("config/template_profiles.json"))
    scan_inputs = subparsers.add_parser("scan-inputs", help="Scan and classify monthly source PDFs.")
    scan_inputs.add_argument("project_dir", type=Path)

    map_fields = subparsers.add_parser("map-fields", help="Generate recognized_data.json from mapped fields.")
    map_fields.add_argument("project_dir", type=Path)
    map_fields.add_argument("--output", type=Path, required=True)
    map_fields.add_argument("--site-key", default="xinan_high_school")
    map_fields.add_argument("--ocr-raw-dir", type=Path, required=True)

    validate_data = subparsers.add_parser("validate-data", help="Generate validation_report.json.")
    validate_data.add_argument("recognized_data", type=Path)
    validate_data.add_argument("--output", type=Path, required=True)

    write_detail = subparsers.add_parser("write-detail-excel", help="Write school detail workbook copy.")
    write_detail.add_argument("validation_report", type=Path)
    write_detail.add_argument("--project-dir", type=Path, required=True)
    write_detail.add_argument("--output", type=Path, required=True)
    write_detail.add_argument("--site-key", default="xinan_high_school")

    write_summary = subparsers.add_parser("write-summary-excel", help="Write summary workbook copy.")
    write_summary.add_argument("validation_report", type=Path)
    write_summary.add_argument("--project-dir", type=Path, required=True)
    write_summary.add_argument("--output", type=Path, required=True)
    write_summary.add_argument("--site-key", default="xinan_high_school")

    run_month_parser = subparsers.add_parser("run-month", help="Run the monthly end-to-end pipeline.")
    run_month_parser.add_argument("project_dir", type=Path)
    run_month_parser.add_argument("--site-key", default="xinan_high_school")
    run_month_parser.add_argument("--output-base", type=Path, default=Path("outputs"))
    run_month_parser.add_argument("--confirmed-data", type=Path)

    regression_parser = subparsers.add_parser("run-monthly-regression", help="Run the manifest-driven monthly regression matrix.")
    regression_parser.add_argument("--manifest", type=Path, default=Path("config/monthly_regression_manifest.json"))
    regression_parser.add_argument("--output-base", type=Path, default=Path("tmp/mvp022a_multi_month_regression/outputs"))
    regression_parser.add_argument("--matrix", type=Path, default=Path("spec/reports/monthly_regression_matrix.md"))
    regression_parser.add_argument("--dry-run", action="store_true", help="Discover samples and write the matrix without running OCR/pipeline.")

    history_parser = subparsers.add_parser(
        "backfill-school-history",
        help="Stage and optionally apply a cumulative single-meter school history baseline.",
    )
    history_parser.add_argument("source", type=Path)
    history_parser.add_argument("--site-key", required=True)
    history_parser.add_argument("--through-month", required=True)
    history_parser.add_argument("--sheet", default="Sheet1")
    history_parser.add_argument("--verified-site-key")
    history_parser.add_argument("--route-path", type=Path)
    history_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    history_parser.add_argument("--rehearsal-root", type=Path)
    history_parser.add_argument("--apply", action="store_true")
    history_parser.add_argument("--replace-existing", action="store_true")

    portfolio_history_parser = subparsers.add_parser(
        "backfill-portfolio-history",
        help="Stage and optionally apply all historical summary workbooks in one source directory.",
    )
    portfolio_history_parser.add_argument("source_dir", type=Path)
    portfolio_history_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    portfolio_history_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))
    portfolio_history_parser.add_argument("--rehearsal-root", type=Path)
    portfolio_history_parser.add_argument("--apply", action="store_true")

    prepare_monthly = subparsers.add_parser("prepare-monthly-summary", help="Prepare the fixed-site single-voucher monthly summary task.")
    prepare_monthly.add_argument("--task-id", required=True)
    prepare_monthly.add_argument("--month", required=True)
    prepare_monthly.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    collect_monthly = subparsers.add_parser("collect-monthly-summary-source", help="Recognize one JPG, PNG, or PDF monthly voucher.")
    collect_monthly.add_argument("task_id")
    collect_monthly.add_argument("source", type=Path)
    collect_monthly.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    update_monthly = subparsers.add_parser("update-monthly-summary", help="Update recognized monthly-summary values before confirmation.")
    update_monthly.add_argument("task_id")
    update_monthly.add_argument("--values-json", required=True)
    update_monthly.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_monthly = subparsers.add_parser("confirm-monthly-summary", help="Confirm recognized monthly-summary values.")
    confirm_monthly.add_argument("task_id")
    confirm_monthly.add_argument("--values-json")
    confirm_monthly.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    run_monthly = subparsers.add_parser("run-monthly-summary", help="Generate the cumulative nursing-home summary workbook.")
    run_monthly.add_argument("task_id")
    run_monthly.add_argument(
        "--template",
        type=Path,
        default=Path("config/templates/\u6c64\u897f\u656c\u8001\u9662\u6c47\u603b\u8868.xlsx"),
    )
    run_monthly.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    show_monthly = subparsers.add_parser("show-monthly-summary", help="Show one monthly-summary task.")
    show_monthly.add_argument("task_id")
    show_monthly.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    monthly_outputs = subparsers.add_parser("list-monthly-summary-outputs", help="List monthly-summary output workbooks.")
    monthly_outputs.add_argument("task_id")
    monthly_outputs.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    prepare_zexin = subparsers.add_parser("prepare-huizhou-zexin", help="Prepare the fixed Huizhou Zexin monthly task.")
    prepare_zexin.add_argument("--task-id", required=True)
    prepare_zexin.add_argument("--month", required=True)
    prepare_zexin.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    collect_zexin = subparsers.add_parser("collect-huizhou-zexin-source", help="Recognize one Huizhou Zexin PDF.")
    collect_zexin.add_argument("task_id")
    collect_zexin.add_argument("source", type=Path)
    collect_zexin.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    record_zexin_generation = subparsers.add_parser(
        "record-huizhou-zexin-generation",
        help="Record operator-provided Huizhou Zexin monthly generation.",
    )
    record_zexin_generation.add_argument("task_id")
    record_zexin_generation.add_argument("--month", required=True)
    record_zexin_generation.add_argument("--total-generation", required=True)
    record_zexin_generation.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    update_zexin = subparsers.add_parser("update-huizhou-zexin", help="Update Huizhou Zexin values before confirmation.")
    update_zexin.add_argument("task_id")
    update_zexin.add_argument("--values-json", required=True)
    update_zexin.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_zexin = subparsers.add_parser("confirm-huizhou-zexin", help="Confirm Huizhou Zexin recognized values.")
    confirm_zexin.add_argument("task_id")
    confirm_zexin.add_argument("--values-json")
    confirm_zexin.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    run_zexin = subparsers.add_parser("run-huizhou-zexin", help="Generate the two Huizhou Zexin deliverables.")
    run_zexin.add_argument("task_id")
    run_zexin.add_argument("--template-dir", type=Path, default=Path("config/templates/huizhou_zexin"))
    run_zexin.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    show_zexin = subparsers.add_parser("show-huizhou-zexin", help="Show one Huizhou Zexin task.")
    show_zexin.add_argument("task_id")
    show_zexin.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    zexin_outputs = subparsers.add_parser("list-huizhou-zexin-outputs", help="List Huizhou Zexin outputs.")
    zexin_outputs.add_argument("task_id")
    zexin_outputs.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    prepare_yangjiang = subparsers.add_parser("prepare-yangjiang-hongneng", help="Prepare one Yangjiang Hongneng monthly task.")
    prepare_yangjiang.add_argument("--task-id", required=True)
    prepare_yangjiang.add_argument("--month", required=True)
    prepare_yangjiang.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    collect_yangjiang = subparsers.add_parser(
        "collect-yangjiang-hongneng-source",
        help="Collect one Yangjiang Hongneng image, PDF, or meter-export XLSX.",
    )
    collect_yangjiang.add_argument("task_id")
    collect_yangjiang.add_argument("source", type=Path)
    collect_yangjiang.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_yangjiang = subparsers.add_parser("confirm-yangjiang-hongneng", help="Confirm Yangjiang Hongneng recognized data.")
    confirm_yangjiang.add_argument("task_id")
    confirm_yangjiang.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    run_yangjiang = subparsers.add_parser("run-yangjiang-hongneng", help="Generate Yangjiang Hongneng detail and summary workbooks.")
    run_yangjiang.add_argument("task_id")
    run_yangjiang.add_argument("--template-dir", type=Path, default=Path("config/templates/yangjiang_hongneng"))
    run_yangjiang.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    show_yangjiang = subparsers.add_parser("show-yangjiang-hongneng", help="Show one Yangjiang Hongneng task.")
    show_yangjiang.add_argument("task_id")
    show_yangjiang.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    yangjiang_outputs = subparsers.add_parser("list-yangjiang-hongneng-outputs", help="List Yangjiang Hongneng outputs.")
    yangjiang_outputs.add_argument("task_id")
    yangjiang_outputs.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    prepare_nanke_parser = subparsers.add_parser("prepare-nanke", help="Prepare one Nanke monthly task.")
    prepare_nanke_parser.add_argument("--task-id", required=True)
    prepare_nanke_parser.add_argument("--month", required=True)
    prepare_nanke_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    collect_nanke_parser = subparsers.add_parser("collect-nanke-source", help="Collect one Nanke PDF or image.")
    collect_nanke_parser.add_argument("task_id")
    collect_nanke_parser.add_argument("source", type=Path)
    collect_nanke_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_nanke_parser = subparsers.add_parser("confirm-nanke", help="Confirm Nanke recognized data.")
    confirm_nanke_parser.add_argument("task_id")
    confirm_nanke_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    run_nanke_parser = subparsers.add_parser("run-nanke", help="Generate the two Nanke workbooks.")
    run_nanke_parser.add_argument("task_id")
    run_nanke_parser.add_argument("--template-dir", type=Path, default=Path("config/templates/nanke"))
    run_nanke_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    show_nanke_parser = subparsers.add_parser("show-nanke", help="Show one Nanke task.")
    show_nanke_parser.add_argument("task_id")
    show_nanke_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    nanke_outputs_parser = subparsers.add_parser("list-nanke-outputs", help="List Nanke outputs.")
    nanke_outputs_parser.add_argument("task_id")
    nanke_outputs_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    prepare_factory = subparsers.add_parser("prepare-factory-settlement", help="Prepare one factory settlement task.")
    prepare_factory.add_argument("--task-id", required=True)
    prepare_factory.add_argument("--site-name", required=True)
    prepare_factory.add_argument("--month", required=True)
    prepare_factory.add_argument("--allow-create", action="store_true")
    prepare_factory.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    update_factory_price = subparsers.add_parser("update-factory-settlement-price", help="Update the task grid unit price.")
    update_factory_price.add_argument("task_id")
    update_factory_price.add_argument("--grid-unit-price", required=True)
    update_factory_price.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_factory_price = subparsers.add_parser("confirm-factory-settlement-price", help="Confirm the task grid unit price.")
    confirm_factory_price.add_argument("task_id")
    confirm_factory_price.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    collect_factory = subparsers.add_parser("collect-factory-settlement-source", help="Collect one factory .xlsx settlement source.")
    collect_factory.add_argument("task_id")
    collect_factory.add_argument("source", type=Path)
    collect_factory.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_factory = subparsers.add_parser("confirm-factory-settlement", help="Confirm recognized factory settlement data.")
    confirm_factory.add_argument("task_id")
    confirm_factory.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    run_factory = subparsers.add_parser("run-factory-settlement", help="Generate the cumulative factory summary workbook.")
    run_factory.add_argument("task_id")
    run_factory.add_argument("--template", type=Path, default=Path("config/templates/factory_meizhou_v1/梅州3号厂房汇总表.xlsx"))
    run_factory.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    show_factory = subparsers.add_parser("show-factory-settlement", help="Show one factory settlement task.")
    show_factory.add_argument("task_id")
    show_factory.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    factory_outputs = subparsers.add_parser("list-factory-settlement-outputs", help="List factory settlement outputs.")
    factory_outputs.add_argument("task_id")
    factory_outputs.add_argument("--runtime-root", type=Path, default=Path("runtime"))


    for pricing_command in ("propose-task-pricing", "prepare-task-pricing", "update-task-pricing"):
        propose_pricing_parser = subparsers.add_parser(pricing_command, help="Normalize site/month pricing proposal without creating a task.")
        propose_pricing_parser.add_argument("--site-key", required=True)
        propose_pricing_parser.add_argument("--month", required=True)
        propose_pricing_parser.add_argument("--pricing-json", default="{}")
        propose_pricing_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_pricing_parser = subparsers.add_parser("confirm-task-pricing", help="Commit a prepared site/month pricing proposal and return a task snapshot.")
    confirm_pricing_parser.add_argument("--proposal-id", required=True)
    confirm_pricing_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))


    prepare_draft_parser = subparsers.add_parser(
        "prepare-site-registration-draft",
        help="Prepare a persistent non-site onboarding draft for a trusted conversation/site/month.",
    )
    prepare_draft_parser.add_argument("--conversation-key", required=True)
    prepare_draft_parser.add_argument("--site-name", required=True)
    prepare_draft_parser.add_argument("--month", required=True)
    prepare_draft_parser.add_argument("--meter-topology-profile", choices=("single_meter_school", "dual_meter_school"), required=True)
    prepare_draft_parser.add_argument(
        "--recreate-cancelled",
        action="store_true",
        help="Explicitly restart the matched cancelled draft instead of rejecting it.",
    )
    prepare_draft_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    for draft_pricing_command in ("prepare-site-registration-draft-pricing", "update-site-registration-draft-pricing"):
        draft_pricing_parser = subparsers.add_parser(
            draft_pricing_command,
            help="Persist draft-local pricing input before new-site PDF collection.",
        )
        draft_pricing_parser.add_argument("draft_id")
        draft_pricing_parser.add_argument("--pricing-json", default="{}")
        draft_pricing_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    confirm_draft_pricing_parser = subparsers.add_parser(
        "confirm-site-registration-draft-pricing",
        help="Confirm a prepared draft-local pricing proposal before PDF collection.",
    )
    confirm_draft_pricing_parser.add_argument("draft_id")
    confirm_draft_pricing_parser.add_argument("--proposal-id", required=True)
    confirm_draft_pricing_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    finalize_draft_parser = subparsers.add_parser(
        "finalize-site-registration-draft",
        help="Finalize a ready onboarding draft through controlled registration, pricing, task, and PDF intake.",
    )
    finalize_draft_parser.add_argument("draft_id")
    finalize_draft_parser.add_argument("--metadata-json", required=True)
    finalize_draft_parser.add_argument("--pricing-json")
    finalize_draft_parser.add_argument("--pricing-proposal-id")
    finalize_draft_parser.add_argument("--pricing-snapshot-json")
    finalize_draft_parser.add_argument("--alias", action="append")
    finalize_draft_parser.add_argument("--mode", choices=("locate", "restart"), default="locate")
    finalize_draft_parser.add_argument("--profiles", type=Path, default=Path("config/template_profiles.json"))
    finalize_draft_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    cancel_draft_parser = subparsers.add_parser("cancel-site-registration-draft", help="Cancel an onboarding draft without touching production data.")
    cancel_draft_parser.add_argument("draft_id")
    cancel_draft_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    restart_draft_parser = subparsers.add_parser("restart-site-registration-draft", help="Restart draft-only PDF intake without deleting prior evidence.")
    restart_draft_parser.add_argument("draft_id")
    restart_draft_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    resolve_state_parser = subparsers.add_parser("resolve-task-state", help="Read canonical site/month task state without creating revisions or reopening pricing.")
    resolve_state_parser.add_argument("--site-key", required=True)
    resolve_state_parser.add_argument("--month", required=True)
    resolve_state_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    resolve_state_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))
    resolve_task_parser = subparsers.add_parser("resolve-task", help="Resolve a shared site/month revision task.")
    resolve_task_parser.add_argument("--site-key", required=True)
    resolve_task_parser.add_argument("--month", required=True)
    resolve_task_parser.add_argument("--mode", choices=("locate", "restart"), default="locate")
    resolve_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    resolve_task_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))
    resolve_task_parser.add_argument("--pricing-snapshot-json")
    create_task_parser = subparsers.add_parser("create-task", help="Create a file-backed task.")
    create_task_parser.add_argument("task_id")
    create_task_parser.add_argument("--month", required=True)
    create_task_parser.add_argument("--site-key", default="xinan_high_school")
    create_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    create_task_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))
    create_task_parser.add_argument("--pricing-snapshot-json")

    add_file_parser = subparsers.add_parser("add-file", help="Register one input file for a task.")
    add_file_parser.add_argument("task_id")
    add_file_parser.add_argument("file", type=Path)
    add_file_parser.add_argument("--file-type")
    add_file_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    add_file_parser.add_argument("--site-key")
    add_file_parser.add_argument("--month")
    add_file_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    collect_pdf_batch_parser = subparsers.add_parser("collect-pdf-batch", help="Atomically stage and classify exactly three task or draft PDFs.")
    collect_pdf_batch_parser.add_argument("task_id", help="Task id, or draft:<draft_id> for a site-registration draft.")
    collect_pdf_batch_parser.add_argument("pdf1", type=Path)
    collect_pdf_batch_parser.add_argument("pdf2", type=Path)
    collect_pdf_batch_parser.add_argument("pdf3", type=Path)
    collect_pdf_batch_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    collect_pdf_batch_parser.add_argument("--site-key")
    collect_pdf_batch_parser.add_argument("--month")
    collect_pdf_batch_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    run_task_parser = subparsers.add_parser("run-task", help="Run a ready file-backed task.")
    run_task_parser.add_argument("task_id")
    run_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    run_task_parser.add_argument("--site-key")
    run_task_parser.add_argument("--month")
    run_task_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    confirm_task_parser = subparsers.add_parser("confirm-task", help="Apply confirmed_data.json and rerun a task.")
    confirm_task_parser.add_argument("task_id")
    confirm_task_parser.add_argument("confirmed_data", type=Path)
    confirm_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    retry_profile_review_parser = subparsers.add_parser(
        "retry-profile-exempt-review",
        help="Rerun an existing need_review task only when all review issues are profile-exempt missing fields.",
    )
    retry_profile_review_parser.add_argument("task_id")
    retry_profile_review_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    retry_profile_review_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    retry_missing_field_parser = subparsers.add_parser(
        "retry-missing-field-review",
        help="Rerun a need_review task after mapping support is added for all missing fields.",
    )
    retry_missing_field_parser.add_argument("task_id")
    retry_missing_field_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    retry_missing_field_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    retry_review_task_parser = subparsers.add_parser(
        "retry-review-task",
        help="Rerun a need_review task after deterministic mapping or validation code is repaired.",
    )
    retry_review_task_parser.add_argument("task_id")
    retry_review_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    retry_review_task_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    retry_failed_task_parser = subparsers.add_parser(
        "retry-failed-task",
        help="Rerun an existing failed task with its already registered files and frozen pricing snapshot.",
    )
    retry_failed_task_parser.add_argument("task_id")
    retry_failed_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    retry_failed_task_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    cancel_task_parser = subparsers.add_parser("cancel-task", help="Cancel a file-backed task without deleting data.")
    cancel_task_parser.add_argument("task_id")
    cancel_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))

    show_task_parser = subparsers.add_parser("show-task", help="Print task.json.")
    show_task_parser.add_argument("task_id")
    show_task_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    show_task_parser.add_argument("--site-key")
    show_task_parser.add_argument("--month")
    show_task_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    list_outputs_parser = subparsers.add_parser("list-outputs", help="List task output files.")
    list_outputs_parser.add_argument("task_id")
    list_outputs_parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    list_outputs_parser.add_argument("--site-key")
    list_outputs_parser.add_argument("--month")
    list_outputs_parser.add_argument("--config", type=Path, default=Path("config/sites.json"))

    validate_worker_task_parser = subparsers.add_parser("validate-worker-task", help="Validate a worker_task JSON payload.")
    validate_worker_task_parser.add_argument("path", type=Path)

    validate_worker_result_parser = subparsers.add_parser("validate-worker-result", help="Validate a worker_result JSON payload.")
    validate_worker_result_parser.add_argument("path", type=Path)

    return parser


def _metadata_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TemplateProfileError(f"metadata-json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TemplateProfileError("metadata-json must be a JSON object.")
    return payload


def _safe_error_payload(error_code: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": error_code,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "next_allowed_action": "operator_review_request",
    }


def _wants_json_error(args: argparse.Namespace) -> bool:
    return getattr(args, "command", None) in {
        "collect-pdf-batch",
        "prepare-site-registration-draft",
        "prepare-site-registration-draft-pricing",
        "update-site-registration-draft-pricing",
        "confirm-site-registration-draft-pricing",
        "finalize-site-registration-draft",
        "cancel-site-registration-draft",
        "restart-site-registration-draft",
        "auto-register-site",
        "register-site-from-profile",
        "prepare-task-pricing",
        "update-task-pricing",
        "propose-task-pricing",
        "confirm-task-pricing",
        "resolve-task-state",
        "resolve-task",
        "create-task",
        "prepare-monthly-summary",
        "collect-monthly-summary-source",
        "update-monthly-summary",
        "confirm-monthly-summary",
        "run-monthly-summary",
        "show-monthly-summary",
        "list-monthly-summary-outputs",
        "prepare-huizhou-zexin",
        "collect-huizhou-zexin-source",
        "record-huizhou-zexin-generation",
        "update-huizhou-zexin",
        "confirm-huizhou-zexin",
        "run-huizhou-zexin",
        "show-huizhou-zexin",
        "list-huizhou-zexin-outputs",
        "prepare-yangjiang-hongneng",
        "collect-yangjiang-hongneng-source",
        "confirm-yangjiang-hongneng",
        "run-yangjiang-hongneng",
        "show-yangjiang-hongneng",
        "list-yangjiang-hongneng-outputs",
        "prepare-nanke",
        "collect-nanke-source",
        "confirm-nanke",
        "run-nanke",
        "show-nanke",
        "list-nanke-outputs",
        "prepare-factory-settlement",
        "update-factory-settlement-price",
        "confirm-factory-settlement-price",
        "collect-factory-settlement-source",
        "confirm-factory-settlement",
        "run-factory-settlement",
        "show-factory-settlement",
        "list-factory-settlement-outputs",
    }


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _site_config_payload(site: SiteConfig) -> dict[str, Any]:
    return {
        "site_key": site.site_key,
        "site_name": site.site_name,
        "template_profile_key": site.template_profile_key,
        "detail_profile_key": site.detail_profile_key,
        "summary_profile_key": site.summary_profile_key,
        "meter_topology_profile": site.meter_topology_profile,
        "project_name": site.project_name,
        "aliases": list(site.aliases),
        "meter_no": site.meter_no,
        "company_name": site.company_name,
        "multiplier": site.multiplier,
        "meters": [
            {
                "meter_no": meter.meter_no,
                "multiplier": meter.multiplier,
                "role": meter.role,
                "detail_enabled": meter.detail_enabled,
                "grid_export_allocation": meter.grid_export_allocation,
            }
            for meter in site.meters
        ],
        "school_base_price": site.school_base_price,
        "school_discount_rate": site.school_discount_rate,
        "effective_school_price": site.effective_school_price,
        "template_dir": site.template_dir,
        "detail_workbook": site.detail_workbook,
        "detail_sheet": site.detail_sheet,
        "summary_workbook": site.summary_workbook,
        "summary_sheet": site.summary_sheet,
        "detail_profile": site.detail_profile,
        "summary_profile": site.summary_profile,
        "detail_start_row": site.detail_start_row,
        "summary_start_row": site.summary_start_row,
        "detail_company_cell": site.detail_company_cell,
        "detail_statement_date_cell": site.detail_statement_date_cell,
        "summary_end_column": site.summary_end_column,
        "summary_merge_columns": list(site.summary_merge_columns),
        "summary_site_name_cell": site.summary_site_name_cell,
        "summary_group_spacer_rows": site.summary_group_spacer_rows,
        "summary_month_cell_alignment": site.summary_month_cell_alignment,
        "validation_not_applicable_fields": list(site.validation_not_applicable_fields),
        "dynamic_profile_key": site.dynamic_profile_key,
        "dynamic_site_signature": site.dynamic_site_signature,
        "pricing_required": site.pricing_required,
    }


if __name__ == "__main__":
    raise SystemExit(main())
