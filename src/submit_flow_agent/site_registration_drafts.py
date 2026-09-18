"""Persistent non-site registration drafts for new school onboarding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from submit_flow_agent.file_scanner import REQUIRED_FILE_TYPES, classify_pdf_text_content, classify_source_pdf_by_content, extract_pdf_text
from submit_flow_agent.ocr.base import write_ocr_raw_result
from submit_flow_agent.ocr.factory import create_ocr_adapter
from submit_flow_agent.pdf_renderer import (
    MONTHLY_SOURCE_EXTENSIONS,
    PdfRenderError,
    render_monthly_source_to_images,
    validate_monthly_source_file,
)
from submit_flow_agent.pricing import canonicalize_discount_rate, confirm_site_pricing, propose_site_pricing, verify_committed_task_pricing_snapshot
from submit_flow_agent.task_store import utc_now_iso
from submit_flow_agent.template_profiles import auto_register_site_from_profile


DRAFT_STATUS_COLLECTING = "collecting_pdfs"
DRAFT_STATUS_READY = "ready_for_metadata_confirmation"
DRAFT_STATUS_NEED_REVIEW = "need_metadata_review"
DRAFT_STATUS_FINALIZED = "finalized"
DRAFT_STATUS_CANCELLED = "cancelled"
DRAFT_ID_PREFIX = "draft:"


class SiteRegistrationDraftError(RuntimeError):
    """Raised when a site registration draft cannot be advanced safely."""


@dataclass(frozen=True)
class DraftIdentity:
    draft_id: str
    conversation_hash: str


def prepare_site_registration_draft(
    runtime_root: Path | str,
    *,
    conversation_key: str,
    site_name: str,
    month: str,
    meter_topology_profile: str,
    recreate_cancelled: bool = False,
) -> dict[str, Any]:
    _validate_month(month)
    if meter_topology_profile not in {"single_meter_school", "dual_meter_school"}:
        raise SiteRegistrationDraftError("meter_topology_profile must be single_meter_school or dual_meter_school.")
    clean_site_name = _clean_text(site_name, "site_name")
    identity = _draft_identity(conversation_key, clean_site_name, month)
    root = _draft_root(runtime_root, identity.draft_id)
    with _draft_lock(root):
        draft = _load_draft(root)
        prepare_result = "prepared"
        if draft is None:
            draft = _new_draft(identity, clean_site_name, month, meter_topology_profile)
            _write_draft(root, draft)
            prepare_result = "created"
        elif draft.get("status") == DRAFT_STATUS_CANCELLED:
            if not recreate_cancelled:
                raise SiteRegistrationDraftError("Site registration draft is cancelled; restart the draft explicitly.")
            _validate_prepare_recreate_match(draft, clean_site_name, month, meter_topology_profile)
            _restart_site_registration_draft_locked(draft)
            _write_draft(root, draft)
            prepare_result = "restarted_cancelled"
        elif draft.get("meter_topology_profile") != meter_topology_profile:
            raise SiteRegistrationDraftError("Existing draft topology does not match requested topology.")
        response = _draft_response(draft)
        response["prepare_result"] = prepare_result
        response["recreate_cancelled"] = bool(recreate_cancelled)
        return response


def prepare_site_registration_draft_pricing(
    runtime_root: Path | str,
    draft_id: str,
    *,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        _ensure_active_draft(draft)
        supplied, changed = _normalize_draft_pricing_input(pricing or {})
        draft["pricing_state"] = _new_draft_pricing_state(draft, supplied, changed_fields=changed)
        draft.pop("final_pricing_snapshot", None)
        draft.pop("final_pricing_source", None)
        draft["updated_at"] = utc_now_iso()
        _write_draft(root, draft)
        return _draft_response(draft)

def update_site_registration_draft_pricing(
    runtime_root: Path | str,
    draft_id: str,
    *,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        _ensure_active_draft(draft)
        current = _pricing_state(draft)
        merged: dict[str, Any] = {}
        if isinstance(current.get("partial_pricing"), dict):
            merged.update(current["partial_pricing"])
        if isinstance(current.get("pricing"), dict):
            merged.update(current["pricing"])
        supplied, changed = _normalize_draft_pricing_input(pricing or {})
        merged.update(supplied)
        pricing_state = _new_draft_pricing_state(draft, merged, changed_fields=changed)
        draft["pricing_state"] = pricing_state
        draft.pop("final_pricing_snapshot", None)
        draft.pop("final_pricing_source", None)
        draft["updated_at"] = utc_now_iso()
        _write_draft(root, draft)
        return _draft_response(draft)

def confirm_site_registration_draft_pricing(
    runtime_root: Path | str,
    draft_id: str,
    *,
    proposal_id: str,
) -> dict[str, Any]:
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        _ensure_active_draft(draft)
        pricing_state = _pricing_state(draft)
        if pricing_state.get("status") == "confirmed" and pricing_state.get("proposal_id") == proposal_id:
            return _draft_response(draft)
        if pricing_state.get("proposal_id") != proposal_id:
            raise SiteRegistrationDraftError("Draft pricing proposal_id does not match the current draft proposal.")
        if pricing_state.get("status") != "ready_for_confirmation" or not pricing_state.get("ready_for_confirmation"):
            raise SiteRegistrationDraftError("Draft pricing is not ready for confirmation.")
        if pricing_state.get("proposal_hash") != _draft_pricing_hash(pricing_state):
            raise SiteRegistrationDraftError("Draft pricing proposal hash does not match proposal contents.")
        confirmed = {
            "status": "confirmed",
            "ready_for_confirmation": True,
            "pricing": pricing_state["pricing"],
            "partial_pricing": pricing_state["pricing"],
            "proposal_id": pricing_state["proposal_id"],
            "proposal_hash": pricing_state["proposal_hash"],
            "confirmed_at": utc_now_iso(),
            "source_metadata": pricing_state["source_metadata"],
        }
        draft["pricing_state"] = confirmed
        draft["updated_at"] = utc_now_iso()
        _write_draft(root, draft)
        return _draft_response(draft)

def collect_site_registration_draft_pdf_batch(
    runtime_root: Path | str,
    draft_id: str,
    pdf_paths: list[Path | str] | tuple[Path | str, ...],
) -> dict[str, Any]:
    if len(pdf_paths) != len(REQUIRED_FILE_TYPES):
        raise SiteRegistrationDraftError("collect-pdf-batch draft intake requires exactly three PDF paths.")
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        _ensure_collectable_draft(draft)
        _require_confirmed_draft_pricing(draft)
        prepared = [_prepare_pdf_source(source) for source in (Path(path) for path in pdf_paths)]
        intake = _normalize_pdf_intake(draft.get("pdf_intake"))
        staged_files = intake["staged_files"]
        existing_hashes = {str(item.get("sha256")) for item in staged_files if isinstance(item.get("sha256"), str)}
        new_records: list[dict[str, Any]] = []
        seen_hashes = set(existing_hashes)
        for record in prepared:
            if record["sha256"] in seen_hashes:
                continue
            seen_hashes.add(str(record["sha256"]))
            new_records.append(record)
        if len(staged_files) + len(new_records) > len(REQUIRED_FILE_TYPES):
            raise SiteRegistrationDraftError("Draft PDF intake must contain exactly three unique PDFs.")
        if intake.get("batch_state") in {"ready", "need_review", "failed"} and new_records:
            raise SiteRegistrationDraftError("Draft PDF intake already contains three unique PDFs; restart the draft for different files.")

        staged_dir = root / "inputs" / "staged"
        staged_dir.mkdir(parents=True, exist_ok=True)
        for record in new_records:
            target = _unique_input_path(staged_dir, f"{str(draft['month']).replace('-', '')}_{record['original_filename']}")
            shutil.copy2(record["source"], target)
            staged_files.append(
                {
                    "stored_file": str(target.relative_to(root)).replace("\\", "/"),
                    "original_filename": record["original_filename"],
                    "sha256": record["sha256"],
                    "received_at": utc_now_iso(),
                }
            )

        intake["staged_files"] = staged_files
        intake["received_count"] = len(staged_files)
        intake["required_count"] = len(REQUIRED_FILE_TYPES)
        classification_ran = False
        if len(staged_files) == len(REQUIRED_FILE_TYPES):
            batch = _classify_draft_batch(root, staged_files, str(draft["meter_topology_profile"]), str(draft["month"]))
            classification_ran = True
            intake.update(batch["pdf_intake"])
            draft["metadata_review"] = batch["metadata_review"]
            draft["status"] = DRAFT_STATUS_READY if batch["metadata_review"]["ready_for_registration"] else DRAFT_STATUS_NEED_REVIEW
        else:
            intake["batch_state"] = "staging"
            intake["role_results"] = []
            intake["missing_roles"] = list(REQUIRED_FILE_TYPES)
            intake["conflicting_roles"] = []
            intake["next_allowed_action"] = "upload_remaining_pdfs"
            draft["status"] = DRAFT_STATUS_COLLECTING
        draft["pdf_intake"] = intake
        draft["updated_at"] = utc_now_iso()
        if new_records or classification_ran:
            _write_draft(root, draft)
        return _draft_batch_response(draft)


def finalize_site_registration_draft(
    runtime_root: Path | str,
    draft_id: str,
    *,
    aliases: list[str] | tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    pricing_proposal_id: str | None = None,
    pricing_snapshot: dict[str, Any] | None = None,
    pricing: dict[str, Any] | None = None,
    config_path: Path | str = "config/sites.json",
    profiles_path: Path | str = "config/template_profiles.json",
    task_mode: str = "locate",
) -> dict[str, Any]:
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        if draft.get("status") == DRAFT_STATUS_FINALIZED:
            return _draft_response(draft)
        if draft.get("status") == DRAFT_STATUS_CANCELLED:
            raise SiteRegistrationDraftError("Cannot finalize a cancelled draft.")
        if pricing_proposal_id or pricing_snapshot is not None or pricing is not None:
            raise SiteRegistrationDraftError("Draft finalization uses persisted confirmed draft pricing only.")
        _require_confirmed_draft_pricing(draft)
        intake = _normalize_pdf_intake(draft.get("pdf_intake"))
        if intake.get("batch_state") != "ready":
            raise SiteRegistrationDraftError("Draft PDFs must classify successfully before finalization.")
        review = _metadata_review(draft)
        merged_metadata = _merge_confirmed_metadata(review, metadata or {}, str(draft["meter_topology_profile"]))
        if not _metadata_complete(merged_metadata, str(draft["meter_topology_profile"])):
            raise SiteRegistrationDraftError("Draft metadata is incomplete; provide only the missing/conflicting fields requested by metadata_review.")
        registration = auto_register_site_from_profile(
            site_name=str(draft["site_name"]),
            meter_topology_profile=str(draft["meter_topology_profile"]),
            aliases=list(aliases),
            metadata=merged_metadata,
            config_path=config_path,
            profiles_path=profiles_path,
        )
        if registration.get("status") not in {"registered", "reused"}:
            if registration.get("status") == "conflict":
                raise SiteRegistrationDraftError(
                    "新站点登记冲突：填写的站点名称、别名或电表号已归属其他站点，请核对后重新填写。"
                )
            raise SiteRegistrationDraftError(f"Draft site registration did not complete: {registration.get('status')}.")
        site_key = str(registration.get("site_key") or "")
        if not site_key:
            raise SiteRegistrationDraftError("Draft site registration did not return site_key.")
        verified_snapshot = _commit_confirmed_draft_pricing(
            runtime_root,
            draft,
            site_key=site_key,
            month=str(draft["month"]),
            config_path=config_path,
        )

        from submit_flow_agent.task_service import collect_task_pdf_batch, resolve_task

        task_payload = resolve_task(
            runtime_root,
            site_key=site_key,
            month=str(draft["month"]),
            mode=task_mode,
            config_path=config_path,
            pricing_snapshot=verified_snapshot,
        )
        task_id = str(task_payload["task_id"])
        staged_paths = [_task_owned_regular_file(root, str(item["stored_file"]), "draft_pdf") for item in intake["staged_files"]]
        batch_payload = collect_task_pdf_batch(runtime_root, task_id, staged_paths, config_path=config_path)
        draft["status"] = DRAFT_STATUS_FINALIZED
        draft["finalized_at"] = utc_now_iso()
        draft["final_site_key"] = site_key
        draft["final_task_id"] = task_id
        draft["registration_result"] = registration
        draft["task_pdf_batch"] = batch_payload
        draft["updated_at"] = utc_now_iso()
        _write_draft(root, draft)
        return _draft_response(draft)


def cancel_site_registration_draft(runtime_root: Path | str, draft_id: str) -> dict[str, Any]:
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        if draft.get("status") != DRAFT_STATUS_FINALIZED:
            draft["status"] = DRAFT_STATUS_CANCELLED
            draft["cancelled_at"] = utc_now_iso()
            draft["updated_at"] = utc_now_iso()
            _write_draft(root, draft)
        return _draft_response(draft)


def restart_site_registration_draft(runtime_root: Path | str, draft_id: str) -> dict[str, Any]:
    root = _draft_root(runtime_root, _strip_draft_prefix(draft_id))
    with _draft_lock(root):
        draft = _require_draft(root)
        if draft.get("status") == DRAFT_STATUS_FINALIZED:
            raise SiteRegistrationDraftError("Cannot restart a finalized draft.")
        _restart_site_registration_draft_locked(draft)
        _write_draft(root, draft)
        return _draft_response(draft)


def _restart_site_registration_draft_locked(draft: dict[str, Any]) -> None:
    draft.setdefault("restart_history", []).append(
        {
            "restarted_at": utc_now_iso(),
            "previous_status": draft.get("status"),
            "previous_pdf_intake": draft.get("pdf_intake"),
            "previous_metadata_review": draft.get("metadata_review"),
            "previous_pricing_state": draft.get("pricing_state"),
        }
    )
    draft["status"] = DRAFT_STATUS_COLLECTING
    draft["pdf_intake"] = _empty_pdf_intake()
    draft["metadata_review"] = _empty_metadata_review(str(draft["meter_topology_profile"]))
    draft["pricing_state"] = _empty_pricing_state()
    draft["updated_at"] = utc_now_iso()


def _validate_prepare_recreate_match(draft: dict[str, Any], site_name: str, month: str, meter_topology_profile: str) -> None:
    if draft.get("status") != DRAFT_STATUS_CANCELLED:
        raise SiteRegistrationDraftError("Draft recreate is allowed only for cancelled drafts.")
    if draft.get("site_name") != site_name or draft.get("month") != month:
        raise SiteRegistrationDraftError("Cancelled draft recreate request does not match the existing draft identity.")
    if draft.get("meter_topology_profile") != meter_topology_profile:
        raise SiteRegistrationDraftError("Existing draft topology does not match requested topology.")


def is_draft_identity(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(DRAFT_ID_PREFIX)


def _new_draft(identity: DraftIdentity, site_name: str, month: str, meter_topology_profile: str) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "draft_id": identity.draft_id,
        "conversation_hash": identity.conversation_hash,
        "site_name": site_name,
        "month": month,
        "meter_topology_profile": meter_topology_profile,
        "status": DRAFT_STATUS_COLLECTING,
        "pdf_intake": _empty_pdf_intake(),
        "metadata_review": _empty_metadata_review(meter_topology_profile),
        "pricing_state": _empty_pricing_state(),
        "created_at": now,
        "updated_at": now,
    }


def _draft_identity(conversation_key: str, site_name: str, month: str) -> DraftIdentity:
    clean_conversation = _clean_text(conversation_key, "conversation_key")
    conversation_hash = hashlib.sha256(clean_conversation.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(f"{conversation_hash}|{site_name}|{month}".encode("utf-8")).hexdigest()[:24]
    return DraftIdentity(draft_id=f"site_draft_{digest}", conversation_hash=conversation_hash)


def _classify_draft_batch(root: Path, staged_files: list[dict[str, Any]], topology: str, expected_month: str) -> dict[str, Any]:
    roles: dict[str, list[str]] = {role: [] for role in REQUIRED_FILE_TYPES}
    role_results: list[dict[str, Any]] = []
    text_records: list[dict[str, Any]] = []
    conflicting_roles: list[str] = []
    for staged in staged_files:
        stored_file = str(staged.get("stored_file") or "")
        source = _task_owned_regular_file(root, stored_file, "draft_pdf")
        result, text, ocr_status, blocks = _classify_and_extract_text(root, source)
        text_record = {
            "stored_file": stored_file,
            "original_filename": staged.get("original_filename"),
            "sha256": staged.get("sha256"),
            "role": result.role,
            "classification_source": result.source,
            "confidence": result.confidence,
            "text": text,
            "blocks": blocks,
        }
        text_records.append(text_record)
        role_result = {
            "original_filename": staged.get("original_filename"),
            "sha256": staged.get("sha256"),
            "role": result.role,
            "classification_source": result.source,
            "confidence": result.confidence,
        }
        if ocr_status is not None:
            role_result["ocr_status"] = ocr_status
        role_results.append(role_result)
        if result.role in REQUIRED_FILE_TYPES and result.confidence == "high":
            roles[str(result.role)].append(stored_file)
        elif result.role in REQUIRED_FILE_TYPES:
            conflicting_roles.append(str(result.role))
    metadata_review = _extract_metadata_review(text_records, topology, expected_month)
    unresolved_metadata_keys = _metadata_unresolved_keys(metadata_review)
    if unresolved_metadata_keys and _apply_metadata_ocr_fallback(root, text_records, role_results, unresolved_metadata_keys):
        metadata_review = _extract_metadata_review(text_records, topology, expected_month)
    missing_roles = [role for role in REQUIRED_FILE_TYPES if len(roles[role]) != 1]
    duplicated_roles = [role for role in REQUIRED_FILE_TYPES if len(roles[role]) > 1]
    unknown_files = [str(item.get("original_filename") or "") for item in role_results if item.get("role") not in REQUIRED_FILE_TYPES]
    conflicts = list(dict.fromkeys([*conflicting_roles, *duplicated_roles, *unknown_files]))
    ready = not missing_roles and not conflicts
    return {
        "pdf_intake": {
            "batch_state": "ready" if ready else "need_review",
            "role_results": role_results,
            "missing_roles": missing_roles,
            "conflicting_roles": conflicts,
            "next_allowed_action": "confirm_site_metadata_and_pricing" if ready else "operator_review_pdf_roles",
            "files": {role: roles[role][0] if len(roles[role]) == 1 else None for role in REQUIRED_FILE_TYPES},
            "text_records": [_safe_text_record(record) for record in text_records],
        },
        "metadata_review": metadata_review,
    }


def _classify_and_extract_text(root: Path, source: Path) -> tuple[Any, str, str | None, list[dict[str, Any]]]:
    text = extract_pdf_text(source)
    text_result = classify_pdf_text_content(text, source="pdf_text")
    if text_result.confidence == "high":
        return text_result, text, None, []
    hint_result = classify_source_pdf_by_content(source)
    ocr_text, ocr_status, blocks = _extract_ocr_text_and_blocks(root, source)
    if ocr_status == "unavailable":
        return hint_result, text, "unavailable", []
    ocr_result = classify_pdf_text_content(ocr_text, source="ocr")
    if ocr_result.confidence == "high":
        return ocr_result, ocr_text, "classified", blocks
    return hint_result, "\n".join((text, ocr_text)), "unclassified", blocks


def _extract_ocr_text_and_blocks(root: Path, source: Path) -> tuple[str, str, list[dict[str, Any]]]:
    try:
        ocr_dir = root / "inputs" / "ocr_intake" / source.stem
        pages = render_monthly_source_to_images(source, ocr_dir / "pages")
        raw_result = create_ocr_adapter().recognize(source, pages)
        write_ocr_raw_result(raw_result, ocr_dir / "ocr_raw.json", relative_to=root)
    except Exception:
        return "", "unavailable", []
    blocks: list[dict[str, Any]] = []
    for page in raw_result.pages:
        for block in page.blocks:
            blocks.append({"text": block.text, "bbox": list(block.bbox), "confidence": block.confidence, "page": page.page})
    ocr_text = "\n".join(block.text for page in raw_result.pages for block in page.blocks)
    return ocr_text, "metadata_fallback", blocks


def _metadata_needs_bounded_ocr(review: dict[str, Any]) -> bool:
    return bool(_metadata_unresolved_keys(review))


def _metadata_unresolved_keys(review: dict[str, Any]) -> set[str]:
    unresolved = {
        str(item.get("key") or "")
        for bucket in ("missing_fields", "conflicting_fields", "low_confidence_fields")
        for item in review.get(bucket) or []
        if isinstance(item, dict)
    }
    return unresolved & {"project_name", "company_name", "meters", "grid_export_recipient_meter", "month", "document_text"}


def _apply_metadata_ocr_fallback(
    root: Path,
    text_records: list[dict[str, Any]],
    role_results: list[dict[str, Any]],
    unresolved_keys: set[str] | None = None,
) -> bool:
    changed = False
    targets = unresolved_keys or {"project_name", "company_name", "meters", "grid_export_recipient_meter", "month", "document_text"}
    ocr_attempted: set[str] = set()
    for index, record in enumerate(text_records):
        role = str(record.get("role") or "")
        if role not in REQUIRED_FILE_TYPES or record.get("confidence") != "high" or record.get("blocks"):
            continue
        if not (targets & _metadata_ocr_role_keys(role)):
            continue
        stored_file = str(record.get("stored_file") or "")
        if not stored_file or stored_file in ocr_attempted:
            continue
        ocr_attempted.add(stored_file)
        source = _task_owned_regular_file(root, stored_file, "draft_pdf")
        ocr_text, ocr_status, blocks = _extract_ocr_text_and_blocks(root, source)
        if ocr_status == "unavailable" or not blocks:
            continue
        record["blocks"] = blocks
        record["text"] = "\n".join(part for part in (str(record.get("text") or ""), ocr_text) if part)
        if index < len(role_results):
            role_results[index]["ocr_status"] = ocr_status
        changed = True
    return changed


def _metadata_ocr_role_keys(role: str) -> set[str]:
    shared = {"project_name", "company_name", "month", "document_text"}
    if role == "generation_statement":
        return shared | {"meters", "grid_export_recipient_meter"}
    if role in REQUIRED_FILE_TYPES:
        return shared
    return set()


def _extract_metadata_review(text_records: list[dict[str, Any]], topology: str, expected_month: str) -> dict[str, Any]:
    expected_meters = 2 if topology == "dual_meter_school" else 1
    resolved: dict[str, Any] = {}
    resolved_fields: dict[str, Any] = {}
    missing: list[dict[str, Any]] = []
    conflicting: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []

    project_field = _resolve_candidate_field("project_name", _project_name_candidates(text_records), missing, conflicting, low_confidence)
    if project_field is not None:
        resolved["project_name"] = project_field["value"]
        resolved_fields["project_name"] = project_field

    company_field = _resolve_candidate_field("company_name", _company_candidates(text_records), missing, conflicting, low_confidence)
    if company_field is not None:
        resolved["company_name"] = company_field["value"]
        resolved_fields["company_name"] = company_field

    meter_candidates = _paired_meter_candidates(text_records)
    meters_field = _resolve_meter_field(meter_candidates, expected_meters, missing, conflicting, low_confidence)
    if meters_field is not None:
        meters = [dict(item) for item in meters_field["value"]]
        if topology == "dual_meter_school":
            for meter in meters:
                meter["detail_enabled"] = True
            grid_recipient = _grid_export_recipient_meter(meter_candidates)
            if grid_recipient is None:
                missing.append(
                    {
                        **_field_issue("grid_export_recipient_meter", "grid export recipient meter is not explicit in PDF evidence"),
                        "candidates": [_meter_candidate_summary(item) for item in meters],
                        "confidence": "none",
                    }
                )
            else:
                _apply_grid_export_recipient(meters, grid_recipient)
                meters_field["grid_export_recipient_meter"] = grid_recipient
        else:
            meters[0]["detail_enabled"] = True
            meters[0]["grid_export_allocation"] = "site_total"
        resolved["meters"] = meters
        resolved_fields["meters"] = {**meters_field, "value": meters}

    month_review = _month_review(text_records, expected_month)
    if month_review["status"] == "conflict":
        conflicting.append(
            {
                **_field_issue("month", "PDF month evidence conflicts with expected draft month"),
                "expected": expected_month,
                "detected": month_review["detected"],
                "source": month_review["source"],
                "candidates": month_review["candidates"],
            }
        )
    elif month_review["status"] in {"missing", "supporting_only"}:
        low_confidence.append(
            {
                **_field_issue("month", "authoritative PDF statement-month evidence was unavailable; filename evidence is supporting only"),
                "expected": expected_month,
                "detected": month_review.get("detected") or [],
                "source": month_review.get("source"),
                "candidates": month_review["candidates"],
            }
        )

    if not any(str(record.get("text") or "").strip() for record in text_records):
        low_confidence.append(_field_issue("document_text", "PDF text/OCR content was unavailable"))

    missing_keys = {item["key"] for item in missing}
    conflict_keys = {item["key"] for item in conflicting}
    low_confidence_visible = [item for item in low_confidence if item["key"] not in missing_keys and item["key"] not in conflict_keys]
    allowed_metadata_keys = _review_allowed_metadata_keys(
        [item for item in missing if item["key"] not in conflict_keys],
        conflicting,
        low_confidence_visible,
    )
    return {
        "ready_for_registration": not missing and not conflicting and not low_confidence,
        "resolved_metadata": resolved,
        "resolved_fields": resolved_fields,
        "unresolved_fields": {
            "missing": [item for item in missing if item["key"] not in conflict_keys],
            "conflicting": conflicting,
            "low_confidence": low_confidence_visible,
        },
        "missing_fields": [item for item in missing if item["key"] not in conflict_keys],
        "conflicting_fields": conflicting,
        "low_confidence_fields": low_confidence_visible,
        "month_review": month_review,
        "allowed_metadata_keys": allowed_metadata_keys,
    }


def _review_allowed_metadata_keys(
    missing: list[dict[str, Any]],
    conflicting: list[dict[str, Any]],
    low_confidence: list[dict[str, Any]],
) -> list[str]:
    allowed = {"project_name", "company_name", "meters", "grid_export_recipient_meter"}
    keys: list[str] = []
    for item in [*missing, *conflicting, *low_confidence]:
        key = str(item.get("key") or "")
        if key in allowed and key not in keys:
            keys.append(key)
    return keys


def _resolve_candidate_field(
    key: str,
    candidates: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    conflicting: list[dict[str, Any]],
    low_confidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    unique = _unique_candidates(candidates)
    if not unique:
        missing.append({**_field_issue(key, "document extraction did not find this field"), "confidence": "none"})
        return None
    if len(unique) > 1:
        conflicting.append({**_field_issue(key, "multiple distinct document values found"), "candidates": unique})
        return None
    field = unique[0]
    if field.get("confidence") != "high":
        low_confidence.append({**_field_issue(key, "field evidence is not high confidence"), "candidates": unique})
        return None
    return field


def _resolve_meter_field(
    candidates: list[dict[str, Any]],
    expected_meters: int,
    missing: list[dict[str, Any]],
    conflicting: list[dict[str, Any]],
    low_confidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    unique = _unique_meter_candidates(candidates)
    if len(unique) < expected_meters:
        missing.append(
            {
                **_field_issue("meters", f"document extraction found {len(unique)} paired meter record(s), expected {expected_meters}"),
                "candidates": [_meter_candidate_summary(item) for item in candidates],
                "confidence": "none",
            }
        )
        return None
    meter_numbers = [item["meter_no"] for item in unique]
    if len(set(meter_numbers)) != len(meter_numbers):
        conflicting.append(
            {
                **_field_issue("meters", "duplicate meter numbers found with conflicting paired evidence"),
                "candidates": [_meter_candidate_summary(item) for item in candidates],
            }
        )
        return None
    if len(unique) > expected_meters:
        conflicting.append(
            {
                **_field_issue("meters", f"document extraction found more than {expected_meters} paired meter record(s)"),
                "candidates": [_meter_candidate_summary(item) for item in candidates],
            }
        )
        return None
    if any(item.get("confidence") != "high" for item in unique):
        low_confidence.append(
            {
                **_field_issue("meters", "one or more paired meter records are not high confidence"),
                "candidates": [_meter_candidate_summary(item) for item in candidates],
            }
        )
        return None
    return {
        "value": [
            {
                "meter_no": item["meter_no"],
                "multiplier": item["multiplier"],
                "source": item["source"],
                "confidence": item["confidence"],
                "evidence": item.get("evidence"),
                "detail_evidence": bool(item.get("detail_evidence")),
                "grid_export_evidence": bool(item.get("grid_export_evidence")),
            }
            for item in unique
        ],
        "source": "paired_pdf_row",
        "confidence": "high",
        "candidates": [_meter_candidate_summary(item) for item in unique],
    }


def _project_name_candidates(text_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    bounded_patterns = (r"项目名称\s*[:：]?\s*(.+?)\s*(?:项目地址|电厂户编号)",)
    fallback_patterns = (r"项目名称\s*[:：]?\s*([^\r\n，,；;。]{4,80})",)
    for record in text_records:
        if record.get("role") not in REQUIRED_FILE_TYPES:
            continue
        school_candidate = _coordinate_school_project_candidate(record.get("blocks"), record)
        if school_candidate is not None:
            candidates.append(school_candidate)
            continue
        coordinate_candidate = _coordinate_project_name_candidate(record.get("blocks"), record)
        if coordinate_candidate is not None:
            candidates.append(coordinate_candidate)
            continue
        text = str(record.get("text") or "")
        bounded = _regex_candidate_pairs(text, bounded_patterns)
        pairs = bounded if bounded else _regex_candidate_pairs(text, fallback_patterns)
        for value, evidence in pairs:
            clean = _clean_project_name_candidate(value)
            if _valid_project_name_candidate(clean):
                candidates.append(_text_candidate(clean, record, "project_name", evidence, confidence=_record_metadata_confidence(record)))
    return candidates


def _coordinate_school_project_candidate(blocks: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _parsed_coordinate_blocks(blocks)
    category_labels = [item for item in parsed if _clean_candidate_value(item["text"]) == "项目类别"]
    if len(category_labels) != 1:
        return None
    label = category_labels[0]
    school_blocks = [
        item
        for item in parsed
        if item["page"] == label["page"]
        and 0 < label["ycenter"] - item["ycenter"] <= 100
        and re.search(r"(?:学校|中学|小学)$", _clean_candidate_value(item["text"]))
    ]
    category_blocks = [
        item
        for item in parsed
        if item["page"] == label["page"]
        and item["x0"] >= label["x1"] - 5
        and label["y0"] - 15 <= item["ycenter"] <= label["y1"] + 90
        and any(keyword in _clean_candidate_value(item["text"]) for keyword in ("光伏", "分布式", "屋顶", "项目"))
    ]
    if len(school_blocks) != 1 or not category_blocks:
        return None
    category_blocks.sort(key=lambda item: (item["y0"], item["x0"]))
    value = _clean_project_name_candidate(
        _clean_candidate_value(school_blocks[0]["text"])
        + "".join(_clean_candidate_value(item["text"]) for item in category_blocks)
    )
    if not _valid_project_name_candidate(value) or "项目" not in value:
        return None
    evidence = (
        f"page {label['page']}: {school_blocks[0]['text']} + 项目类别 -> "
        f"{' | '.join(str(item['text']) for item in category_blocks)}"
    )
    return _text_candidate(value, record, "project_name", evidence, confidence=_record_metadata_confidence(record))


def _parsed_coordinate_blocks(blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(blocks, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in blocks:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        bbox = item.get("bbox")
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        parsed.append(
            {
                "text": text,
                "page": str(item.get("page") or item.get("page_number") or 1),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "xcenter": (x0 + x1) / 2,
                "ycenter": (y0 + y1) / 2,
            }
        )
    return parsed


def _coordinate_project_name_candidate(blocks: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _parsed_coordinate_blocks(blocks)
    labels = [item for item in parsed if _clean_candidate_value(item["text"]) == "项目名称"]
    if len(labels) != 1:
        return None
    label = labels[0]
    boundary_labels = [
        item
        for item in parsed
        if item["page"] == label["page"]
        and item["x0"] > label["x1"]
        and abs(item["ycenter"] - label["ycenter"]) <= 40
        and _clean_candidate_value(item["text"]) in {"联系电话", "项目地址", "项目业主", "项目编号", "电厂户编号"}
    ]
    right_boundary = min((item["x0"] for item in boundary_labels), default=float("inf"))
    value_blocks = [
        item
        for item in parsed
        if item is not label
        and item["page"] == label["page"]
        and item["x0"] >= label["x1"] - 5
        and item["x1"] < right_boundary
        and item["y0"] <= label["y1"] + 40
        and item["y1"] >= label["y0"] - 40
        and _valid_project_name_candidate(_clean_candidate_value(item["text"]))
    ]
    if not value_blocks:
        return None
    value_blocks.sort(key=lambda item: (item["y0"], item["x0"]))
    value = _clean_project_name_candidate("".join(str(item["text"]) for item in value_blocks))
    if not _valid_project_name_candidate(value):
        return None
    evidence = f"page {label['page']}: 项目名称 -> {' | '.join(str(item['text']) for item in value_blocks)}"
    return _text_candidate(value, record, "project_name", evidence, confidence=_record_metadata_confidence(record))


def _valid_project_name_candidate(value: str) -> bool:
    if len(value) < 4:
        return False
    if not any(character.isalpha() for character in value):
        return False
    return value not in {"项目地址", "项目业主", "项目编号", "项目名称", "公司名称", "企业名称"}


def _clean_project_name_candidate(value: Any) -> str:
    clean = _clean_candidate_value(value)
    for boundary in ("项目地址", "项目业主", "项目编号", "电厂户编号"):
        position = clean.find(boundary)
        if position >= 4:
            clean = clean[:position]
    return clean


def _company_candidates(text_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    patterns = (
        r"(?:公司名称|发电企业|售电公司|企业名称)\s*[:：]?\s*([^\s，,；;。]{4,60})",
        r"([^\s，,；;。]{4,60}(?:公司|有限公司))",
    )
    for record in text_records:
        for value, evidence in _regex_candidate_pairs(str(record.get("text") or ""), patterns):
            clean = _clean_company_name_candidate(value)
            if clean and ":" not in clean and "：" not in clean:
                candidates.append(_text_candidate(clean, record, "company_name", evidence, confidence=_record_metadata_confidence(record)))
    return candidates


def _clean_company_name_candidate(value: Any) -> str:
    clean = _clean_candidate_value(value)
    clean = re.sub(r"^(?:尊敬的|您好|用户)+", "", clean)
    return clean


def _paired_meter_candidates(text_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in text_records:
        if record.get("role") != "generation_statement":
            continue
        lines = _evidence_lines(str(record.get("text") or ""))
        text_candidates: list[dict[str, Any]] = []
        text_candidates.extend(_generation_table_meter_candidates(lines, record))
        if not text_candidates:
            text_candidates.extend(_labeled_row_meter_candidates(lines, record))
        candidates.extend(text_candidates)
        if not text_candidates:
            block_candidates = _coordinate_table_meter_candidates(record.get("blocks"), record)
            candidates.extend(block_candidates)
    return candidates


def _coordinate_table_meter_candidates(blocks: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair meter_no and multiplier from OCR blocks using bbox row/column alignment.

    OCR output for scanned generation statements often emits each table cell as an
    independent block with a bbox, rather than one text line per table row. A pure
    text parser cannot pair such layouts. This parser clusters blocks into visual
    rows by y-center, finds the header row containing the meter and multiplier
    column labels, and reads the corresponding columns on each data row.
    """
    parsed_blocks = _parsed_coordinate_blocks(blocks)
    if not parsed_blocks:
        return []
    rows = _coordinate_rows(parsed_blocks)
    candidates: list[dict[str, Any]] = []
    seen_meter_multiplier: set[tuple[str, str]] = set()
    for page in sorted({row["page"] for row in rows}, key=str):
        page_rows = [row for row in rows if row["page"] == page]
        header_row = _coordinate_header_row(page_rows)
        if header_row is None:
            continue
        page_blocks = [block for block in parsed_blocks if block["page"] == page]
        wrapped_candidates = _coordinate_wrapped_meter_candidates(page_blocks, header_row, record)
        if wrapped_candidates:
            for candidate in wrapped_candidates:
                key = (str(candidate["meter_no"]), str(candidate["multiplier"]))
                if key not in seen_meter_multiplier:
                    seen_meter_multiplier.add(key)
                    candidates.append(candidate)
            continue
        for row in page_rows:
            if row["row_id"] == header_row["row_id"] or row["ycenter"] <= header_row["ycenter"]:
                continue
            meter_block = _nearest_coordinate_cell(row, header_row["meter_x"], _clean_meter_no)
            multiplier_block = _nearest_coordinate_cell(row, header_row["multiplier_x"], _clean_multiplier)
            if meter_block is None or multiplier_block is None:
                continue
            meter_no = _clean_meter_no(meter_block["text"])
            multiplier = _clean_multiplier(multiplier_block["text"])
            if not meter_no or multiplier is None:
                continue
            key = (meter_no, multiplier)
            if key in seen_meter_multiplier:
                continue
            seen_meter_multiplier.add(key)
            candidate: dict[str, Any] = {
                "meter_no": meter_no,
                "multiplier": multiplier,
                "source": _record_source(record, "meters"),
                "confidence": _record_metadata_confidence(record),
                "evidence": _compact_evidence(f"page {page}: {meter_no} x {multiplier}"),
            }
            detail_block = _nearest_coordinate_cell(row, header_row.get("detail_x"), _clean_optional)
            grid_block = _nearest_coordinate_cell(row, header_row.get("grid_x"), _clean_optional)
            if detail_block is not None and _table_column_has_signal([detail_block["text"]], 0):
                candidate["detail_evidence"] = True
            if grid_block is not None and _table_column_has_signal([grid_block["text"]], 0):
                candidate["grid_export_evidence"] = True
            candidates.append(candidate)
    return candidates


def _coordinate_wrapped_meter_candidates(
    page_blocks: list[dict[str, Any]],
    header_row: dict[str, Any],
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    multiplier_blocks = [
        block
        for block in page_blocks
        if block["ycenter"] > header_row["ycenter"]
        and abs(block["xcenter"] - header_row["multiplier_x"]) <= _ROW_X_TOLERANCE
        and _clean_multiplier(block["text"]) is not None
    ]
    candidates: list[dict[str, Any]] = []
    for multiplier_block in multiplier_blocks:
        meter_fragments = [
            block
            for block in page_blocks
            if abs(block["xcenter"] - header_row["meter_x"]) <= _ROW_X_TOLERANCE
            and abs(block["ycenter"] - multiplier_block["ycenter"]) <= 45
            and _clean_meter_no(block["text"])
        ]
        meter_fragments.sort(key=lambda block: (block["ycenter"], block["x0"]))
        if (
            len(meter_fragments) < 2
            or meter_fragments[-1]["ycenter"] - meter_fragments[0]["ycenter"] <= _ROW_GAP_TOLERANCE
        ):
            continue
        meter_no = _clean_meter_no("".join(str(block["text"]) for block in meter_fragments))
        multiplier = _clean_multiplier(multiplier_block["text"])
        if not meter_no or multiplier is None:
            continue
        candidates.append(
            {
                "meter_no": meter_no,
                "multiplier": multiplier,
                "source": _record_source(record, "meters"),
                "confidence": _record_metadata_confidence(record),
                "evidence": _compact_evidence(
                    f"page {header_row['page']}: {' + '.join(str(block['text']) for block in meter_fragments)} x {multiplier}"
                ),
            }
        )
    return candidates


_ROW_GAP_TOLERANCE = 8.0
_ROW_X_TOLERANCE = 18.0


def _coordinate_rows(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    remaining = sorted(blocks, key=lambda item: (str(item["page"]), item["ycenter"]))
    while remaining:
        first = remaining.pop(0)
        row_blocks = [first]
        keep: list[dict[str, Any]] = []
        for item in remaining:
            if item["page"] == first["page"] and abs(item["ycenter"] - first["ycenter"]) <= _ROW_GAP_TOLERANCE:
                row_blocks.append(item)
            else:
                keep.append(item)
        remaining = keep
        row_blocks.sort(key=lambda item: item["xcenter"])
        rows.append({"row_id": len(rows), "page": first["page"], "ycenter": first["ycenter"], "blocks": row_blocks})
    return rows


def _coordinate_header_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    meter_keywords = ("电表编号", "电表号", "表号", "户号", "资产编号")
    multiplier_keywords = ("倍率", "综合倍率")
    for row in rows:
        blocks = row["blocks"]
        meter_block = _first_coordinate_header_block(blocks, meter_keywords)
        if meter_block is None:
            continue
        multiplier_block = _first_coordinate_header_block(blocks, multiplier_keywords)
        if multiplier_block is None or multiplier_block is meter_block:
            continue
        detail_block = _first_coordinate_header_block(blocks, ("用电", "学校", "自用", "用户"))
        grid_block = _first_coordinate_header_block(blocks, ("上网", "并网", "余电", "电网"))
        return {
            "row_id": row["row_id"],
            "page": row["page"],
            "ycenter": row["ycenter"],
            "meter_x": meter_block["xcenter"],
            "multiplier_x": multiplier_block["xcenter"],
            "detail_x": detail_block["xcenter"] if detail_block is not None else None,
            "grid_x": grid_block["xcenter"] if grid_block is not None else None,
        }
    return None


def _first_coordinate_header_block(blocks: list[dict[str, Any]], keywords: tuple[str, ...]) -> dict[str, Any] | None:
    matches = [block for block in blocks if any(keyword in block["text"] for keyword in keywords)]
    return matches[0] if len(matches) == 1 else None


def _nearest_coordinate_cell(row: dict[str, Any], header_x: Any, parser: Any) -> dict[str, Any] | None:
    if header_x is None:
        return None
    valid: list[tuple[float, dict[str, Any]]] = []
    for block in row["blocks"]:
        distance = abs(float(block["xcenter"]) - float(header_x))
        if distance > _ROW_X_TOLERANCE:
            continue
        if parser(block["text"]) in (None, ""):
            continue
        valid.append((distance, block))
    if not valid:
        return None
    valid.sort(key=lambda item: item[0])
    if len(valid) > 1 and abs(valid[0][0] - valid[1][0]) < 1.0:
        return None
    return valid[0][1]


def _generation_table_meter_candidates(lines: list[str], record: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    header: dict[str, int] | None = None
    for line in lines:
        columns = _split_table_columns(line)
        if header is None:
            header = _generation_table_header(columns)
            continue
        parsed = _parse_generation_table_row(columns, header, record, line)
        if parsed is not None:
            candidates.append(parsed)
        elif _line_looks_like_meter_row(line):
            candidates.append(_unparsed_meter_candidate(record, line))
    return candidates


def _generation_table_header(columns: list[str]) -> dict[str, int] | None:
    meter_index = _first_header_column_index(columns, ("电表编号", "电表号", "表号", "户号"))
    multiplier_index = _first_header_column_index(columns, ("倍率", "综合倍率"))
    if meter_index is None or multiplier_index is None:
        return None
    detail_index = _first_column_index(columns, ("用电", "学校", "自用", "用户"))
    grid_index = _first_column_index(columns, ("上网", "并网", "余电", "电网"))
    return {"meter": meter_index, "multiplier": multiplier_index, "detail": detail_index if detail_index is not None else -1, "grid": grid_index if grid_index is not None else -1}


def _first_header_column_index(columns: list[str], keywords: tuple[str, ...]) -> int | None:
    for index, column in enumerate(columns):
        normalized = re.sub(r"[（(][^）)]*[）)]$", "", column.strip())
        if normalized in keywords:
            return index
    return None


def _parse_generation_table_row(columns: list[str], header: dict[str, int], record: dict[str, Any], evidence: str) -> dict[str, Any] | None:
    meter_index = header["meter"]
    multiplier_index = header["multiplier"]
    if len(columns) <= max(meter_index, multiplier_index):
        return None
    meter_no = _clean_meter_no(columns[meter_index])
    multiplier = _clean_multiplier(columns[multiplier_index])
    if not meter_no or multiplier is None:
        return None
    candidate = {
        "meter_no": meter_no,
        "multiplier": multiplier,
        "source": _record_source(record, "meters"),
        "confidence": _record_metadata_confidence(record),
        "evidence": _compact_evidence(evidence),
    }
    if _table_column_has_signal(columns, header.get("detail", -1)):
        candidate["detail_evidence"] = True
    if _table_column_has_signal(columns, header.get("grid", -1)) or _grid_export_line(evidence):
        candidate["grid_export_evidence"] = True
    return candidate


def _labeled_row_meter_candidates(lines: list[str], record: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line in lines:
        repeated = _repeated_labeled_meter_candidates(line, record)
        if repeated:
            candidates.extend(repeated)
            continue
        meter_values = _meter_values_from_line(line)
        multiplier_values = _multiplier_values_from_line(line)
        if len(meter_values) != 1 or len(multiplier_values) != 1:
            continue
        candidate = {
            "meter_no": meter_values[0],
            "multiplier": multiplier_values[0],
            "source": _record_source(record, "meters"),
            "confidence": _record_metadata_confidence(record),
            "evidence": _compact_evidence(line),
        }
        if _detail_line(line):
            candidate["detail_evidence"] = True
        if _grid_export_line(line):
            candidate["grid_export_evidence"] = True
        candidates.append(candidate)
    return candidates


def _repeated_labeled_meter_candidates(line: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    meter_label = r"(?:电表编号|电表号|表号|户号)"
    multiplier_label = r"(?:倍率|综合倍率)"
    pattern = re.compile(
        meter_label
        + r"\s*[:：]?\s*([A-Za-z0-9\-]{6,40})"
        + r".{0,80}?"
        + multiplier_label
        + r"\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)"
        + r"(.*?)(?="
        + meter_label
        + r"\s*[:：]?|$)",
    )
    matches = list(pattern.finditer(line))
    if len(matches) < 2:
        return []
    candidates: list[dict[str, Any]] = []
    for match in matches:
        meter_no = _clean_meter_no(match.group(1))
        multiplier = _clean_multiplier(match.group(2))
        if not meter_no or multiplier is None:
            return []
        evidence = _compact_evidence(match.group(0))
        candidate = {
            "meter_no": meter_no,
            "multiplier": multiplier,
            "source": _record_source(record, "meters"),
            "confidence": _record_metadata_confidence(record),
            "evidence": evidence,
        }
        if _detail_line(evidence):
            candidate["detail_evidence"] = True
        if _grid_export_line(evidence):
            candidate["grid_export_evidence"] = True
        candidates.append(candidate)
    return candidates


def _meter_values_from_line(line: str) -> list[str]:
    patterns = (r"(?:电表编号|电表号|表号|户号)\s*[:：]?\s*([A-Za-z0-9\-]{6,40})",)
    return list(dict.fromkeys(_clean_meter_no(value) for value in _regex_values(line, patterns) if _clean_meter_no(value)))


def _multiplier_values_from_line(line: str) -> list[str]:
    values = _regex_values(line, (r"(?:倍率|综合倍率)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",))
    return list(dict.fromkeys(value for value in (_clean_multiplier(value) for value in values) if value is not None))


def _split_table_columns(line: str) -> list[str]:
    return [column for column in re.split(r"\s+", line.strip()) if column]


def _first_column_index(columns: list[str], keywords: tuple[str, ...]) -> int | None:
    for index, column in enumerate(columns):
        if any(keyword in column for keyword in keywords):
            return index
    return None


def _table_column_has_signal(columns: list[str], index: int) -> bool:
    if index < 0 or index >= len(columns):
        return False
    return _clean_optional(columns[index]) not in {"", "0", "0.0", "-", "--"}


def _line_looks_like_meter_row(line: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9\-]{6,40}", line) and re.search(r"\d+(?:\.\d+)?", line))


def _unparsed_meter_candidate(record: dict[str, Any], evidence: str) -> dict[str, Any]:
    return {
        "meter_no": None,
        "multiplier": None,
        "source": _record_source(record, "meters"),
        "confidence": "none",
        "evidence": _compact_evidence(evidence),
        "reason": "generation statement row looked like meter data but did not match the parsed header columns",
    }


def _clean_meter_no(value: Any) -> str | None:
    text = str(value or "").strip().strip("：:，,；;。")
    if re.fullmatch(r"[A-Za-z0-9\-]{6,40}", text):
        return text
    return None


def _clean_multiplier(value: Any) -> str | None:
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    if decimal <= 0:
        return None
    return _decimal_text(decimal)


def _grid_export_recipient_meter(candidates: list[dict[str, Any]]) -> str | None:
    meters = {str(item.get("meter_no")) for item in candidates if item.get("grid_export_evidence") and item.get("meter_no")}
    return next(iter(meters)) if len(meters) == 1 else None


def _detail_line(line: str) -> bool:
    return any(keyword in line for keyword in ("用电", "学校", "自用", "用户")) and not _grid_export_line(line)


def _grid_export_line(line: str) -> bool:
    return any(keyword in line for keyword in ("上网", "上网电量", "上网电费", "并网", "余电", "电网"))


def _apply_grid_export_recipient(meters: list[dict[str, Any]], recipient_meter_no: str) -> None:
    for meter in meters:
        meter["grid_export_allocation"] = "site_total" if meter.get("meter_no") == recipient_meter_no else "none"


def _month_review(text_records: list[dict[str, Any]], expected_month: str) -> dict[str, Any]:
    text_candidates: list[dict[str, Any]] = []
    filename_candidates: list[dict[str, Any]] = []
    for record in text_records:
        for month, evidence in _semantic_month_candidate_pairs(str(record.get("text") or "")):
            text_candidates.append({"value": month, "source": _record_source(record, "month"), "confidence": "high", "evidence": evidence})
        filename = str(record.get("original_filename") or "")
        for month, evidence in _month_candidate_pairs(filename):
            filename_candidates.append({"value": month, "source": {"type": "filename", "filename": filename}, "confidence": "supporting", "evidence": evidence})
    authoritative = _unique_candidates(text_candidates)
    supporting = _unique_candidates(filename_candidates)
    conflicts = [item for item in authoritative if item.get("value") != expected_month]
    if conflicts:
        return {
            "status": "conflict",
            "expected": expected_month,
            "detected": [item["value"] for item in conflicts],
            "source": "pdf_text_or_ocr",
            "candidates": [*authoritative, *supporting],
        }
    matches = [item for item in authoritative if item.get("value") == expected_month]
    if matches:
        return {"status": "matched", "expected": expected_month, "detected": [expected_month], "source": "pdf_text_or_ocr", "candidates": [*authoritative, *supporting]}
    if supporting:
        detected = [str(item["value"]) for item in supporting if item.get("value")]
        status = "supporting_only" if expected_month in detected else "missing"
        return {"status": status, "expected": expected_month, "detected": detected, "source": "filename_supporting", "candidates": supporting}
    return {"status": "missing", "expected": expected_month, "detected": [], "source": None, "candidates": supporting}


def _semantic_month_candidate_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    label = _statement_month_label_pattern()
    value = _statement_month_value_pattern()
    for match in re.finditer(label + r"\s*[:：]?\s*" + value, text):
        month = _normalize_month_text(match.group(1))
        if month is not None:
            pairs.append((month, _compact_evidence(match.group(0))))
    pairs.extend(_semantic_month_table_candidate_pairs(text))
    return list(dict.fromkeys(pairs))


def _semantic_month_table_candidate_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    lines = _evidence_lines(text)
    for index, line in enumerate(lines[:-1]):
        header_columns = _split_table_columns(line)
        month_index = _statement_month_column_index(header_columns)
        if month_index is None:
            continue
        for row in lines[index + 1:]:
            row_columns = _split_table_columns(row)
            if not row_columns or _statement_month_column_index(row_columns) is not None:
                break
            if len(row_columns) <= month_index:
                continue
            month = _normalize_month_text(row_columns[month_index])
            if month is not None:
                evidence = f"{header_columns[month_index]} -> {row_columns[month_index]}"
                pairs.append((month, _compact_evidence(evidence)))
                break
    return pairs


def _statement_month_column_index(columns: list[str]) -> int | None:
    for index, column in enumerate(columns):
        if re.search(_statement_month_label_pattern(), column):
            return index
    return None


def _statement_month_label_pattern() -> str:
    return r"(?:发电月份|购电月份|结算月份|账单月份|电费月份|电量月份|所属月份)"


def _statement_month_value_pattern() -> str:
    return r"((?:20\d{2})(?:0[1-9]|1[0-2])|(?:20\d{2})[-.](?:0?[1-9]|1[0-2])|(?:20\d{2})年(?:0?[1-9]|1[0-2])月|(?:20\d{2})\D+(?:0?[1-9]|1[0-2])\D*)"

def _month_candidate_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    patterns = (
        r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)",
        r"(?<!\d)(20\d{2})[-.](0?[1-9]|1[0-2])(?!\d)",
        r"(20\d{2})年(0?[1-9]|1[0-2])月",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            month = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
            pairs.append((month, match.group(0)))
    return list(dict.fromkeys(pairs))


def _normalize_month_text(text: str) -> str | None:
    compact = str(text or "").strip()
    patterns = (
        r"^(20\d{2})(0[1-9]|1[0-2])$",
        r"^(20\d{2})[-.](0?[1-9]|1[0-2])$",
        r"^(20\d{2})年(0?[1-9]|1[0-2])月$",
        r"^(20\d{2})\D+(0?[1-9]|1[0-2])\D*$",
    )
    for pattern in patterns:
        match = re.match(pattern, compact)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    return None

def _regex_candidate_pairs(text: str, patterns: tuple[str, ...]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = str(match.group(1)).strip()
            if value:
                pairs.append((value, match.group(0).strip()))
    return pairs


def _regex_values(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [value for value, _evidence in _regex_candidate_pairs(text, patterns)]


def _evidence_lines(text: str) -> list[str]:
    return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


def _text_candidate(value: str, record: dict[str, Any], field: str, evidence: str, *, confidence: str) -> dict[str, Any]:
    return {
        "value": value,
        "source": _record_source(record, field),
        "confidence": confidence,
        "evidence": _compact_evidence(evidence),
    }


def _record_metadata_confidence(record: dict[str, Any]) -> str:
    confidence = str(record.get("confidence") or "")
    return confidence if confidence in {"high", "supporting", "low", "none"} else "none"


def _record_source(record: dict[str, Any], field: str) -> dict[str, Any]:
    return {
        "field": field,
        "role": record.get("role"),
        "stored_file": record.get("stored_file"),
        "original_filename": record.get("original_filename"),
        "classification_source": record.get("classification_source"),
    }


def _unique_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        value = _canonical_text_value(item.get("value"))
        if not value:
            continue
        current = unique.get(value)
        if current is None or _confidence_rank(item.get("confidence")) > _confidence_rank(current.get("confidence")):
            unique[value] = {**item, "value": str(item.get("value") or "").strip()}
    return list(unique.values())


def _unique_meter_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        meter_no = str(item.get("meter_no") or "").strip()
        multiplier = str(item.get("multiplier") or "").strip()
        if not meter_no or not multiplier:
            continue
        unique.setdefault((meter_no, multiplier), {**item, "meter_no": meter_no, "multiplier": multiplier})
    return list(unique.values())


def _meter_candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "meter_no": item.get("meter_no"),
        "multiplier": item.get("multiplier"),
        "source": item.get("source"),
        "confidence": item.get("confidence"),
        "evidence": _compact_evidence(item.get("evidence") or ""),
    }


def _clean_candidate_value(value: str) -> str:
    return re.sub(r"\s+", "", value).strip("：:，,；;。")


def _canonical_text_value(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().casefold()


def _confidence_rank(value: Any) -> int:
    return {"none": 0, "low": 1, "supporting": 2, "high": 3}.get(str(value or ""), 0)


def _compact_evidence(value: Any, *, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."

def _safe_text_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "stored_file": record.get("stored_file"),
        "original_filename": record.get("original_filename"),
        "sha256": record.get("sha256"),
        "role": record.get("role"),
        "classification_source": record.get("classification_source"),
        "confidence": record.get("confidence"),
        "text_snippet": _compact_evidence(record.get("text") or record.get("text_snippet") or ""),
    }


def _merge_confirmed_metadata(review: dict[str, Any], metadata: dict[str, Any], topology: str) -> dict[str, Any]:
    resolved = dict(review.get("resolved_metadata") or {})
    issue_keys = {
        str(item.get("key"))
        for bucket in ("missing_fields", "conflicting_fields", "low_confidence_fields")
        for item in (review.get(bucket) or [])
        if isinstance(item, dict) and item.get("key")
    }
    resolved = {key: value for key, value in resolved.items() if key not in issue_keys}
    allowed = {"project_name", "company_name", "meters", "detail_meter", "grid_export_recipient_meter"}
    unexpected = sorted(set(metadata) - allowed)
    if unexpected:
        raise SiteRegistrationDraftError(f"Draft metadata contains disallowed fields: {', '.join(unexpected)}.")
    supplied = dict(metadata)
    detail_meter = str(supplied.pop("detail_meter", "") or "").strip()
    grid_export_recipient_meter = str(supplied.pop("grid_export_recipient_meter", "") or "").strip()
    merged = {**resolved, **supplied}
    expected = 2 if topology == "dual_meter_school" else 1
    meters = merged.get("meters")
    if isinstance(meters, list):
        if len(meters) != expected:
            raise SiteRegistrationDraftError(f"Draft metadata requires exactly {expected} meter(s) for the selected topology.")
        merged["meters"] = [dict(meter) for meter in meters]
        if topology == "single_meter_school":
            merged["meters"][0]["detail_enabled"] = True
            merged["meters"][0]["grid_export_allocation"] = "site_total"
        else:
            meter_numbers = {str(meter.get("meter_no") or "").strip() for meter in merged["meters"] if isinstance(meter, dict)}
            if detail_meter:
                if detail_meter not in meter_numbers:
                    raise SiteRegistrationDraftError("detail_meter must match one of the confirmed meter numbers.")
            for meter in merged["meters"]:
                meter["detail_enabled"] = True
            if grid_export_recipient_meter:
                if grid_export_recipient_meter not in meter_numbers:
                    raise SiteRegistrationDraftError("grid_export_recipient_meter must match one of the confirmed meter numbers.")
                _apply_grid_export_recipient(merged["meters"], grid_export_recipient_meter)
    return merged


def _metadata_complete(metadata: dict[str, Any], topology: str | None = None) -> bool:
    if not _clean_optional(metadata.get("project_name")) or not _clean_optional(metadata.get("company_name")):
        return False
    meters = metadata.get("meters")
    if not isinstance(meters, list) or not meters:
        return False
    if topology in {"single_meter_school", "dual_meter_school"}:
        expected = 2 if topology == "dual_meter_school" else 1
        if len(meters) != expected:
            return False
    for meter in meters:
        if not isinstance(meter, dict) or not _clean_optional(meter.get("meter_no")) or not _clean_optional(meter.get("multiplier")):
            return False
        if topology == "dual_meter_school" and "detail_enabled" not in meter:
            return False
        if topology == "dual_meter_school" and not _clean_optional(meter.get("grid_export_allocation")):
            return False
    if topology == "dual_meter_school":
        if sum(1 for meter in meters if meter.get("detail_enabled") is True) != 2:
            return False
        if sum(1 for meter in meters if meter.get("grid_export_allocation") == "site_total") != 1:
            return False
    return True



def _draft_response(draft: dict[str, Any]) -> dict[str, Any]:
    response = _draft_batch_response(draft)
    response.update(
        {
            "draft_id": draft.get("draft_id"),
            "site_name": draft.get("site_name"),
            "month": draft.get("month"),
            "meter_topology_profile": draft.get("meter_topology_profile"),
            "status": draft.get("status"),
            "metadata_review": draft.get("metadata_review") or {},
            "pricing_state": _safe_pricing_state(_pricing_state(draft)),
            "final_site_key": draft.get("final_site_key"),
            "final_task_id": draft.get("final_task_id"),
            "final_pricing_source": draft.get("final_pricing_source"),
            "formal_task": _final_formal_task_contract(draft),
            "task_pdf_batch": _safe_task_pdf_batch(draft.get("task_pdf_batch")),
        }
    )
    return response



def _final_formal_task_contract(draft: dict[str, Any]) -> dict[str, Any] | None:
    batch = _safe_task_pdf_batch(draft.get("task_pdf_batch"))
    task_id = draft.get("final_task_id")
    site_key = draft.get("final_site_key")
    if not task_id and batch is None:
        return None
    return {
        "task_id": task_id or (batch or {}).get("task_id"),
        "site_key": site_key or (batch or {}).get("site_key"),
        "month": draft.get("month") or (batch or {}).get("month"),
        "task_status": (batch or {}).get("task_status"),
        "pdf_batch_state": (batch or {}).get("batch_state"),
        "received_count": (batch or {}).get("received_count"),
        "required_count": (batch or {}).get("required_count"),
        "missing_roles": (batch or {}).get("missing_roles") or [],
        "carried_file_state": _carried_file_state(batch),
        "next_allowed_action": _formal_task_next_action(batch),
    }


def _formal_task_next_action(batch: dict[str, Any] | None) -> str | None:
    if not isinstance(batch, dict):
        return None
    action = batch.get("next_allowed_action")
    ready_to_run = batch.get("task_status") == "ready_to_run" and batch.get("batch_state") == "ready"
    if action == "run_task" and not ready_to_run:
        return None
    if action:
        return str(action)
    if ready_to_run:
        return "run_task"
    return None


def _carried_file_state(batch: dict[str, Any] | None) -> str | None:
    if not isinstance(batch, dict):
        return None
    if batch.get("batch_state") != "ready":
        return "not_ready"
    if len(batch.get("classified_files") or []) == len(REQUIRED_FILE_TYPES):
        return "accepted_batch_carried"
    return "ready_missing_carried_file_details"

def _safe_task_pdf_batch(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "task_id": value.get("task_id"),
        "site_key": value.get("site_key"),
        "month": value.get("month"),
        "task_status": value.get("task_status"),
        "batch_state": value.get("batch_state"),
        "received_count": value.get("received_count"),
        "required_count": value.get("required_count"),
        "role_results": value.get("role_results") or [],
        "missing_roles": value.get("missing_roles") or [],
        "conflicting_roles": value.get("conflicting_roles") or [],
        "carried_file_state": _carried_file_state(value),
        "next_allowed_action": _formal_task_next_action(value),
        "classified_files": value.get("classified_files") or [],
    }

def _draft_batch_response(draft: dict[str, Any]) -> dict[str, Any]:
    intake = _normalize_pdf_intake(draft.get("pdf_intake"))
    role_results = _safe_role_results(intake.get("role_results") or [])
    ready_roles = _high_confidence_roles(role_results)
    classified_files: list[dict[str, Any]] = []
    if intake.get("batch_state") == "ready" and len(ready_roles) == len(REQUIRED_FILE_TYPES):
        files = intake.get("files") if isinstance(intake.get("files"), dict) else {}
        for role in REQUIRED_FILE_TYPES:
            stored_file = str(files.get(role) or "")
            if stored_file:
                classified_files.append(
                    {
                        "file_type": role,
                        "original_filename": _original_filename_for_role(intake["staged_files"], stored_file),
                    }
                )
    return {
        "identity_type": "site_registration_draft",
        "draft_id": draft.get("draft_id"),
        "site_name": draft.get("site_name"),
        "month": draft.get("month"),
        "batch_state": intake.get("batch_state"),
        "received_count": intake.get("received_count"),
        "required_count": intake.get("required_count"),
        "role_results": role_results,
        "missing_roles": intake.get("missing_roles") or [],
        "conflicting_roles": intake.get("conflicting_roles") or [],
        "pdf_intake": _safe_pdf_intake(intake, role_results=role_results),
        "next_allowed_action": intake.get("next_allowed_action"),
        "classified_files": classified_files if len(classified_files) == len(REQUIRED_FILE_TYPES) else [],
        "metadata_review": draft.get("metadata_review") or {},
        "pricing_state": _safe_pricing_state(_pricing_state(draft)),
    }


def _safe_pricing_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status"),
        "ready_for_confirmation": bool(state.get("ready_for_confirmation")),
        "pricing": state.get("pricing") if isinstance(state.get("pricing"), dict) else None,
        "partial_pricing": state.get("partial_pricing") if isinstance(state.get("partial_pricing"), dict) else None,
        "changed_fields": state.get("changed_fields") or [],
        "missing_fields": state.get("missing_fields") or [],
        "source_metadata": state.get("source_metadata") or {},
        "proposal_id": state.get("proposal_id"),
        "proposal_hash": state.get("proposal_hash"),
        "confirmed_at": state.get("confirmed_at"),
    }


def _safe_role_results(results: list[Any]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        safe.append(
            {
                "original_filename": item.get("original_filename"),
                "sha256": item.get("sha256"),
                "role": item.get("role"),
                "classification_source": item.get("classification_source"),
                "confidence": item.get("confidence"),
                "ocr_status": item.get("ocr_status"),
            }
        )
    return safe


def _high_confidence_roles(role_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    duplicated: set[str] = set()
    for item in role_results:
        role = item.get("role")
        if role in REQUIRED_FILE_TYPES and item.get("confidence") == "high":
            role_key = str(role)
            if role_key in roles:
                duplicated.add(role_key)
            else:
                roles[role_key] = item
    for role in duplicated:
        roles.pop(role, None)
    return roles


def _safe_pdf_intake(intake: dict[str, Any], *, role_results: list[dict[str, Any]]) -> dict[str, Any]:
    files = intake.get("files") if isinstance(intake.get("files"), dict) else {}
    return {
        "batch_state": intake.get("batch_state"),
        "received_count": intake.get("received_count"),
        "required_count": intake.get("required_count"),
        "staged_files": [dict(item) for item in intake.get("staged_files") or [] if isinstance(item, dict)],
        "role_results": role_results,
        "missing_roles": intake.get("missing_roles") or [],
        "conflicting_roles": intake.get("conflicting_roles") or [],
        "next_allowed_action": intake.get("next_allowed_action"),
        "files": {role: files.get(role) for role in REQUIRED_FILE_TYPES},
        "text_records": [_safe_text_record(record) for record in intake.get("text_records") or [] if isinstance(record, dict)],
    }


def _empty_pricing_state() -> dict[str, Any]:
    return {
        "status": "missing_pricing_fields",
        "ready_for_confirmation": False,
        "pricing": None,
        "changed_fields": [],
        "missing_fields": ["discount_rate", "effective_unit_price"],
        "source_metadata": {
            "source_type": "missing",
            "source_site_key": None,
            "source_month": None,
            "pricing_version": None,
            "pricing_hash": None,
        },
    }


def _pricing_state(draft: dict[str, Any]) -> dict[str, Any]:
    state = draft.get("pricing_state")
    return state if isinstance(state, dict) else _empty_pricing_state()


def _require_confirmed_draft_pricing(draft: dict[str, Any]) -> dict[str, Any]:
    state = _pricing_state(draft)
    if state.get("status") != "confirmed" or not isinstance(state.get("pricing"), dict):
        raise SiteRegistrationDraftError("Draft pricing must be confirmed before PDF collection.")
    return state


def _new_draft_pricing_state(draft: dict[str, Any], pricing: dict[str, Any], *, changed_fields: list[str]) -> dict[str, Any]:
    missing = [key for key in ("discount_rate", "effective_unit_price") if key not in pricing or not _clean_optional(pricing.get(key))]
    complete = not missing
    state = {
        "status": "ready_for_confirmation" if complete else "missing_pricing_fields",
        "ready_for_confirmation": complete,
        "pricing": dict(pricing) if complete else None,
        "partial_pricing": dict(pricing),
        "changed_fields": list(changed_fields),
        "missing_fields": missing,
        "source_metadata": {
            "source_type": "draft_operator_input",
            "source_site_key": None,
            "source_month": draft.get("month"),
            "pricing_version": None,
            "pricing_hash": None,
        },
        "proposal_id": f"dpp_{secrets.token_urlsafe(18).replace('-', '_')}",
        "created_at": utc_now_iso(),
    }
    state["proposal_hash"] = _draft_pricing_hash(state)
    return state


def _normalize_draft_pricing_input(pricing: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    unsupported = sorted(key for key in pricing if key not in {"discount_rate", "effective_unit_price"})
    if unsupported:
        raise SiteRegistrationDraftError(
            "Draft pricing accepts only discount_rate and effective_unit_price; "
            f"unsupported fields: {', '.join(unsupported)}."
        )
    normalized: dict[str, Any] = {}
    changed: list[str] = []
    if "discount_rate" in pricing and _clean_optional(pricing.get("discount_rate")):
        normalized["discount_rate"] = _decimal_text(_parse_draft_discount(pricing["discount_rate"]))
        changed.append("discount_rate")
    if "effective_unit_price" in pricing and _clean_optional(pricing.get("effective_unit_price")):
        normalized["effective_unit_price"] = _decimal_text(_parse_draft_effective_price(pricing["effective_unit_price"]))
        changed.append("effective_unit_price")
    return normalized, changed


def _parse_draft_discount(value: Any) -> Decimal:
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise SiteRegistrationDraftError("discount_rate must be a decimal number.") from exc
    normalized = canonicalize_discount_rate(decimal)
    if normalized <= 0 or normalized > 1:
        raise SiteRegistrationDraftError("discount_rate must be greater than 0 and no more than 1.")
    return normalized


def _parse_draft_effective_price(value: Any) -> Decimal:
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise SiteRegistrationDraftError("effective_unit_price must be a decimal number.") from exc
    if decimal <= 0:
        raise SiteRegistrationDraftError("effective_unit_price must be greater than 0.")
    return decimal


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _draft_pricing_hash(state: dict[str, Any]) -> str:
    payload = {
        "draft_pricing_contract": 1,
        "proposal_id": state.get("proposal_id"),
        "pricing": state.get("pricing"),
        "partial_pricing": state.get("partial_pricing"),
        "changed_fields": state.get("changed_fields") or [],
        "missing_fields": state.get("missing_fields") or [],
        "source_metadata": state.get("source_metadata") or {},
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _commit_confirmed_draft_pricing(
    runtime_root: Path | str,
    draft: dict[str, Any],
    *,
    site_key: str,
    month: str,
    config_path: Path | str,
) -> dict[str, Any]:
    state = _require_confirmed_draft_pricing(draft)
    pricing = dict(state["pricing"])
    existing = draft.get("final_pricing_snapshot")
    if isinstance(existing, dict):
        return verify_committed_task_pricing_snapshot(runtime_root, existing, site_key=site_key, month=month)
    recovered = _matching_committed_site_pricing_snapshot(runtime_root, site_key=site_key, month=month, pricing=pricing)
    if recovered is not None:
        draft["final_pricing_snapshot"] = recovered
        draft["final_pricing_source"] = _final_pricing_source(state, recovered, recovered=True)
        return recovered
    proposal = propose_site_pricing(runtime_root, site_key=site_key, month=month, proposal=pricing, config_path=config_path)
    if not proposal.ready_for_confirmation or proposal.proposal_id is None:
        raise SiteRegistrationDraftError("Confirmed draft pricing could not prepare a production site pricing proposal.")
    snapshot = confirm_site_pricing(runtime_root, proposal_id=proposal.proposal_id, config_path=config_path, pricing_source="draft_operator_confirmed")
    verified = verify_committed_task_pricing_snapshot(runtime_root, snapshot, site_key=site_key, month=month)
    draft["final_pricing_snapshot"] = verified
    draft["final_pricing_source"] = _final_pricing_source(state, verified, recovered=False)
    return verified

def _matching_committed_site_pricing_snapshot(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    pricing: dict[str, Any],
) -> dict[str, Any] | None:
    history_path = Path(runtime_root) / "site_pricing" / site_key / "pricing.json"
    if not history_path.is_file() or history_path.is_symlink():
        return None
    try:
        history = json.loads(history_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None
    versions = history.get("versions") if isinstance(history, dict) else None
    if not isinstance(versions, list):
        return None
    for item in sorted((entry for entry in versions if isinstance(entry, dict)), key=lambda entry: int(entry.get("pricing_version") or 0), reverse=True):
        if item.get("month") != month:
            continue
        if item.get("discount_rate") != pricing.get("discount_rate") or item.get("effective_unit_price") != pricing.get("effective_unit_price"):
            continue
        try:
            return verify_committed_task_pricing_snapshot(runtime_root, item, site_key=site_key, month=month)
        except Exception:
            return None
    return None


def _final_pricing_source(state: dict[str, Any], snapshot: dict[str, Any], *, recovered: bool) -> dict[str, Any]:
    return {
        "source_type": "confirmed_registration_draft",
        "draft_pricing_proposal_id": state.get("proposal_id"),
        "draft_pricing_hash": state.get("proposal_hash"),
        "pricing_version": snapshot.get("pricing_version"),
        "pricing_hash": snapshot.get("pricing_hash"),
        "recovered_existing_version": recovered,
    }

def _empty_pdf_intake() -> dict[str, Any]:
    return {
        "batch_state": "staging",
        "required_count": len(REQUIRED_FILE_TYPES),
        "received_count": 0,
        "staged_files": [],
        "role_results": [],
        "missing_roles": list(REQUIRED_FILE_TYPES),
        "conflicting_roles": [],
        "next_allowed_action": "upload_remaining_pdfs",
        "files": {role: None for role in REQUIRED_FILE_TYPES},
    }


def _normalize_pdf_intake(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_pdf_intake()
    normalized = {**_empty_pdf_intake(), **value}
    staged = normalized.get("staged_files")
    normalized["staged_files"] = [item for item in staged if isinstance(item, dict)] if isinstance(staged, list) else []
    normalized["received_count"] = len(normalized["staged_files"])
    return normalized


def _empty_metadata_review(topology: str) -> dict[str, Any]:
    expected = 2 if topology == "dual_meter_school" else 1
    return {
        "ready_for_registration": False,
        "resolved_metadata": {},
        "missing_fields": [
            _field_issue("company_name", "waiting for draft PDF extraction"),
            _field_issue("meters", f"waiting for {expected} meter(s) from draft PDF extraction"),
        ],
        "conflicting_fields": [],
        "low_confidence_fields": [],
        "allowed_metadata_keys": ["project_name", "company_name", "meters", "grid_export_recipient_meter"],
    }


def _metadata_review(draft: dict[str, Any]) -> dict[str, Any]:
    review = draft.get("metadata_review")
    if not isinstance(review, dict):
        return _empty_metadata_review(str(draft.get("meter_topology_profile") or "single_meter_school"))
    return review


def _field_issue(key: str, reason: str) -> dict[str, Any]:
    return {"key": key, "reason": reason}


def _prepare_pdf_source(source: Path) -> dict[str, Any]:
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise SiteRegistrationDraftError("Input material must be an existing regular file.")
    if source.suffix.lower() not in MONTHLY_SOURCE_EXTENSIONS:
        raise SiteRegistrationDraftError("Input file extension must be PDF, JPG, JPEG, or PNG.")
    if source.suffix.lower() != ".pdf":
        try:
            validate_monthly_source_file(source)
        except PdfRenderError as exc:
            raise SiteRegistrationDraftError(str(exc)) from exc
    return {"source": source, "original_filename": source.name, "sha256": _hash_file(source)}


def _ensure_active_draft(draft: dict[str, Any]) -> None:
    if draft.get("status") == DRAFT_STATUS_FINALIZED:
        raise SiteRegistrationDraftError("Cannot update a finalized draft.")
    if draft.get("status") == DRAFT_STATUS_CANCELLED:
        raise SiteRegistrationDraftError("Cannot update a cancelled draft; restart the draft explicitly.")

def _ensure_collectable_draft(draft: dict[str, Any]) -> None:
    if draft.get("status") in {DRAFT_STATUS_FINALIZED, DRAFT_STATUS_CANCELLED}:
        raise SiteRegistrationDraftError(f"Draft status {draft.get('status')!r} does not accept PDFs.")


def _require_draft(root: Path) -> dict[str, Any]:
    draft = _load_draft(root)
    if draft is None:
        raise SiteRegistrationDraftError("Site registration draft does not exist.")
    return draft


def _load_draft(root: Path) -> dict[str, Any] | None:
    path = root / "draft.json"
    if not path.is_file() or path.is_symlink():
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise SiteRegistrationDraftError("draft.json must contain an object.")
    return payload


def _write_draft(root: Path, draft: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".draft.", suffix=".json", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(draft, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / "draft.json")
    finally:
        if temporary.exists():
            temporary.unlink()


def _draft_root(runtime_root: Path | str, draft_id: str) -> Path:
    clean_id = _safe_id(_strip_draft_prefix(draft_id))
    return Path(runtime_root) / "site_registration_drafts" / clean_id


def _strip_draft_prefix(draft_id: str) -> str:
    return draft_id[len(DRAFT_ID_PREFIX):] if draft_id.startswith(DRAFT_ID_PREFIX) else draft_id


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,80}", value or ""):
        raise SiteRegistrationDraftError("Invalid draft_id.")
    return value


def _unique_input_path(input_dir: Path, source_name: str) -> Path:
    suffix = Path(source_name).suffix
    stem = Path(source_name).stem or "input"
    return input_dir / f"{stem}.{uuid.uuid4()}{suffix}"


def _task_owned_regular_file(root: Path, relative_path: str, label: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SiteRegistrationDraftError(f"Draft {label} path must stay inside the draft directory.")
    path = root / candidate
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SiteRegistrationDraftError(f"Draft {label} path escapes the draft directory.") from exc
    if path.is_symlink() or not path.is_file():
        raise SiteRegistrationDraftError(f"Draft {label} must be a regular file inside the draft directory.")
    return path


def _original_filename_for_role(staged_files: list[dict[str, Any]], stored_file: str) -> str:
    for staged in staged_files:
        if staged.get("stored_file") == stored_file:
            return str(staged.get("original_filename") or Path(stored_file).name)
    return Path(stored_file).name


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_month(month: str) -> None:
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
        raise SiteRegistrationDraftError("month must be YYYY-MM.")
    month_number = int(month[5:])
    if month_number < 1 or month_number > 12:
        raise SiteRegistrationDraftError("month must be YYYY-MM.")


def _clean_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SiteRegistrationDraftError(f"{field} is required.")
    return text


def _clean_optional(value: Any) -> str:
    return str(value or "").strip()


@contextmanager
def _draft_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".draft.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)






















