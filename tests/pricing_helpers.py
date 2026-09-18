from __future__ import annotations

from pathlib import Path
from typing import Any

from submit_flow_agent.pricing import _snapshot_hash
from submit_flow_agent.task_service import confirm_task_pricing, create_task as raw_create_task, propose_task_pricing


def static_pricing_snapshot(site_key: str = "xinan_high_school", month: str = "2026-05") -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "site_key": site_key,
        "month": month,
        "discount_rate": "0.95",
        "effective_unit_price": "0.6998",
        "base_unit_price": None,
        "pricing_version": 1,
        "pricing_source": "test_confirmed",
        "confirmed_at": "2026-07-29T00:00:00+00:00",
        "effective_month": month,
        "proposal_id": f"test-{site_key}-{month}",
        "proposal_hash": "test-proposal-hash",
    }
    snapshot["pricing_hash"] = _snapshot_hash(snapshot)
    return snapshot


def committed_pricing_snapshot(
    runtime: Path,
    *,
    site_key: str = "xinan_high_school",
    month: str = "2026-05",
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    proposal = propose_task_pricing(
        runtime,
        site_key=site_key,
        month=month,
        pricing={"discount_rate": "0.95", "effective_unit_price": "0.6998"},
        config_path=config_path,
    )
    if not proposal.get("ready_for_confirmation"):
        raise AssertionError(proposal)
    return confirm_task_pricing(runtime, proposal_id=str(proposal["proposal_id"]), config_path=config_path)


def create_task_with_pricing(runtime: Path, **kwargs: Any) -> dict[str, Any]:
    site_key = str(kwargs.get("site_key") or "xinan_high_school")
    month = str(kwargs["month"])
    config_path = kwargs.get("config_path", "config/sites.json")
    kwargs.setdefault(
        "pricing_snapshot",
        committed_pricing_snapshot(runtime, site_key=site_key, month=month, config_path=config_path),
    )
    return raw_create_task(runtime, **kwargs)
