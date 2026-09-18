"""Site configuration loading for the local MVP pipeline."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/sites.json")

REQUIRED_SITE_FIELDS = (
    "site_key",
    "site_name",
    "project_name",
    "company_name",
    "school_base_price",
    "school_discount_rate",
    "template_dir",
    "detail_workbook",
    "detail_sheet",
    "summary_workbook",
    "summary_sheet",
)

SUPPORTED_DETAIL_PROFILES = {"single_detail_row", "multi_detail_rows"}
SUPPORTED_SUMMARY_PROFILES = {"meter_rows_summary", "two_meter_ak_summary"}
SUPPORTED_METER_TOPOLOGY_PROFILES = {"single_meter_school", "dual_meter_school"}
GRID_EXPORT_SITE_TOTAL_ALLOCATIONS = {"site_total", "all"}
GRID_EXPORT_ALLOCATIONS = {*GRID_EXPORT_SITE_TOTAL_ALLOCATIONS, "none"}
OUTPUT_WORKBOOK_LABELS = {
    "detail": "统计表",
    "summary": "汇总表格",
}


class ConfigError(RuntimeError):
    """Raised when site configuration cannot be loaded or validated."""


@dataclass(frozen=True)
class MeterConfig:
    meter_no: str
    multiplier: Decimal
    role: str = "primary"
    detail_enabled: bool = True
    grid_export_allocation: str = "site_total"


@dataclass(frozen=True)
class SiteConfig:
    site_key: str
    site_name: str
    project_name: str
    company_name: str
    school_base_price: Decimal
    school_discount_rate: Decimal
    effective_school_price: Decimal | None
    template_dir: str
    detail_workbook: str
    detail_sheet: str
    summary_workbook: str
    summary_sheet: str
    template_profile_key: str | None
    detail_profile_key: str | None
    summary_profile_key: str | None
    meter_topology_profile: str
    aliases: tuple[str, ...]
    issuer_aliases: tuple[str, ...]
    meters: tuple[MeterConfig, ...]
    detail_profile: str = "single_detail_row"
    summary_profile: str = "meter_rows_summary"
    detail_start_row: int = 5
    summary_start_row: int = 4
    detail_company_cell: str | None = "A6"
    detail_statement_date_cell: str | None = "A7"
    summary_end_column: int = 13
    summary_merge_columns: tuple[str, ...] = ("I", "M")
    summary_site_name_cell: str | None = None
    summary_group_spacer_rows: int = 0
    summary_month_cell_alignment: str | None = None
    validation_not_applicable_fields: tuple[str, ...] = ()
    dynamic_profile_key: str | None = None
    dynamic_site_signature: str | None = None
    pricing_required: bool = False
    summary_materialize_calculations: bool = False
    summary_retroactive_full_history: bool = False

    def output_dir(self, month: str, base_dir: Path | str = "outputs") -> Path:
        if not _is_year_month(month):
            raise ConfigError(f"Invalid month '{month}', expected YYYY-MM.")
        return Path(base_dir) / self.site_name / month

    def output_workbook_name(self, role: str, month: str) -> str:
        return site_output_workbook_name(self.site_name, role, month)

    @property
    def primary_meter(self) -> MeterConfig:
        for meter in self.meters:
            if meter.role == "primary":
                return meter
        return self.meters[0]

    @property
    def detail_meters(self) -> tuple[MeterConfig, ...]:
        return tuple(meter for meter in self.meters if meter.detail_enabled)

    @property
    def meter_no(self) -> str:
        return self.primary_meter.meter_no

    @property
    def multiplier(self) -> Decimal:
        return self.primary_meter.multiplier


def site_output_workbook_name(site_name: str, role: str, month: str) -> str:
    """Return the site-owned delivery name, independent of the template filename."""

    if role not in OUTPUT_WORKBOOK_LABELS:
        raise ConfigError(f"Unsupported output workbook role '{role}'.")
    if not _is_year_month(month):
        raise ConfigError(f"Invalid month '{month}', expected YYYY-MM.")
    clean_name = str(site_name).strip().rstrip(" .")
    if not clean_name or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in clean_name):
        raise ConfigError("Site name contains characters that are unsafe in output workbook filenames.")
    return f"{clean_name}{OUTPUT_WORKBOOK_LABELS[role]}_{month}.xlsx"


def load_site_config(
    site_key: str = "xinan_high_school",
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> SiteConfig:
    """Load and validate one site config by key, canonical name, or alias."""

    config_file = Path(config_path)
    payload = _read_json(config_file)
    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise ConfigError(f"{config_file} must contain a 'sites' list.")

    wanted = _site_identity_key(site_key)
    key_matches = [
        raw_site
        for raw_site in sites
        if isinstance(raw_site, dict) and _site_identity_key(raw_site.get("site_key")) == wanted
    ]
    if len(key_matches) == 1:
        return _parse_site(key_matches[0], config_file)
    if len(key_matches) > 1:
        raise ConfigError(f"Site key '{site_key}' is duplicated in {config_file}.")

    matches: list[dict[str, Any]] = []
    for raw_site in sites:
        if not isinstance(raw_site, dict):
            raise ConfigError(f"{config_file} contains a non-object site entry.")
        if not _site_is_retired(raw_site) and wanted and wanted in _site_identity_keys(raw_site):
            matches.append(raw_site)

    if len(matches) == 1:
        return _parse_site(matches[0], config_file)
    if len(matches) > 1:
        raise ConfigError(f"Site identity '{site_key}' is ambiguous in {config_file}.")

    raise ConfigError(f"Site config '{site_key}' was not found in {config_file}.")

def list_site_keys(config_path: Path | str = DEFAULT_CONFIG_PATH) -> list[str]:
    config_file = Path(config_path)
    payload = _read_json(config_file)
    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise ConfigError(f"{config_file} must contain a 'sites' list.")
    # Retired keys remain reserved even though retired sites are omitted from
    # the user-facing registry returned by list_sites().
    keys = [site.get("site_key") for site in sites if isinstance(site, dict)]
    return [key for key in keys if isinstance(key, str) and key]


def list_sites(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    include_retired: bool = False,
) -> dict[str, list[dict[str, object]]]:
    config_file = Path(config_path)
    payload = _read_json(config_file)
    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise ConfigError(f"{config_file} must contain a 'sites' list.")
    parsed = [
        _parse_site(site, config_file)
        for site in sites
        if isinstance(site, dict) and (include_retired or not _site_is_retired(site))
    ]
    return {
        "sites": [
            {
                "site_key": site.site_key,
                "site_name": site.site_name,
                "template_profile_key": site.template_profile_key,
                "detail_profile_key": site.detail_profile_key,
                "summary_profile_key": site.summary_profile_key,
                "meter_topology_profile": site.meter_topology_profile,
                "summary_site_name_cell": site.summary_site_name_cell,
                "summary_group_spacer_rows": site.summary_group_spacer_rows,
                "summary_month_cell_alignment": site.summary_month_cell_alignment,
                "validation_not_applicable_fields": list(site.validation_not_applicable_fields),
                "dynamic_profile_key": site.dynamic_profile_key,
                "dynamic_site_signature": site.dynamic_site_signature,
                "pricing_required": site.pricing_required,
                "summary_materialize_calculations": site.summary_materialize_calculations,
                "summary_retroactive_full_history": site.summary_retroactive_full_history,
                "aliases": _display_aliases(site),
                "resolution_aliases": _resolution_aliases(site),
            }
            for site in parsed
        ]
    }


def _read_json(config_file: Path) -> dict[str, Any]:
    if not config_file.exists():
        raise ConfigError(f"Site config file does not exist: {config_file}")

    try:
        data = json.loads(config_file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Site config file is not valid JSON: {config_file}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{config_file} must contain a JSON object.")
    return data


def _parse_site(raw_site: dict[str, Any], config_file: Path) -> SiteConfig:
    missing = [field for field in REQUIRED_SITE_FIELDS if _is_blank(raw_site.get(field))]
    if "meters" not in raw_site:
        missing.extend(
            field
            for field in ("meter_no", "multiplier")
            if field not in missing and _is_blank(raw_site.get(field))
        )
    if missing:
        joined = ", ".join(missing)
        raise ConfigError(f"Site config in {config_file} is missing fields: {joined}.")

    detail_profile = str(raw_site.get("detail_profile") or "single_detail_row")
    summary_profile = str(raw_site.get("summary_profile") or "meter_rows_summary")
    if detail_profile not in SUPPORTED_DETAIL_PROFILES:
        raise ConfigError(f"Unsupported detail_profile '{detail_profile}' in {config_file}.")
    if summary_profile not in SUPPORTED_SUMMARY_PROFILES:
        raise ConfigError(f"Unsupported summary_profile '{summary_profile}' in {config_file}.")

    meters = _parse_meters(raw_site, config_file)
    meter_topology_profile = _meter_topology_profile(raw_site, meters, config_file)

    return SiteConfig(
        site_key=str(raw_site["site_key"]),
        site_name=str(raw_site["site_name"]),
        project_name=str(raw_site["project_name"]),
        company_name=str(raw_site["company_name"]),
        school_base_price=_decimal_field(raw_site, "school_base_price", config_file),
        school_discount_rate=_decimal_field(raw_site, "school_discount_rate", config_file),
        effective_school_price=_optional_decimal_field(raw_site, "effective_school_price", config_file),
        template_dir=str(_template_dir(raw_site, config_file)),
        detail_workbook=str(raw_site["detail_workbook"]),
        detail_sheet=str(raw_site["detail_sheet"]),
        summary_workbook=str(raw_site["summary_workbook"]),
        summary_sheet=str(raw_site["summary_sheet"]),
        template_profile_key=_optional_key(raw_site, "template_profile_key"),
        detail_profile_key=_optional_key(raw_site, "detail_profile_key"),
        summary_profile_key=_optional_key(raw_site, "summary_profile_key"),
        meter_topology_profile=meter_topology_profile,
        aliases=_parse_aliases(raw_site),
        issuer_aliases=_parse_string_list(raw_site, "issuer_aliases", "Site config issuer_aliases"),
        meters=meters,
        detail_profile=detail_profile,
        summary_profile=summary_profile,
        detail_start_row=_int_field(raw_site, "detail_start_row", config_file, default=5),
        summary_start_row=_int_field(raw_site, "summary_start_row", config_file, default=4),
        detail_company_cell=_optional_cell(raw_site, "detail_company_cell", default="A6"),
        detail_statement_date_cell=_optional_cell(raw_site, "detail_statement_date_cell", default="A7"),
        summary_end_column=_summary_end_column(raw_site, config_file),
        summary_merge_columns=_summary_merge_columns(raw_site),
        summary_site_name_cell=_optional_cell(raw_site, "summary_site_name_cell", default=None),
        summary_group_spacer_rows=_nonnegative_int_field(raw_site, "summary_group_spacer_rows", config_file, default=0),
        summary_month_cell_alignment=_summary_month_cell_alignment(raw_site, config_file),
        validation_not_applicable_fields=_validation_not_applicable_fields(raw_site),
        dynamic_profile_key=_optional_key(raw_site, "dynamic_profile_key"),
        dynamic_site_signature=_optional_key(raw_site, "dynamic_site_signature"),
        pricing_required=_bool_field(raw_site, "pricing_required", config_file, default=False),
        summary_materialize_calculations=_bool_field(
            raw_site,
            "summary_materialize_calculations",
            config_file,
            default=False,
        ),
        summary_retroactive_full_history=_bool_field(
            raw_site,
            "summary_retroactive_full_history",
            config_file,
            default=False,
        ),
    )


def _parse_aliases(raw_site: dict[str, Any]) -> tuple[str, ...]:
    return _parse_string_list_value(raw_site.get("aliases", []), "Site config aliases")


def _display_aliases(site: SiteConfig) -> list[str]:
    return _dedup_labels((site.site_name, *site.aliases))


def _resolution_aliases(site: SiteConfig) -> list[str]:
    return _dedup_labels((site.site_key, site.site_name, *site.aliases))


def _dedup_labels(values: tuple[str, ...]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = _site_identity_key(text)
        if key in seen:
            continue
        seen.add(key)
        labels.append(text)
    return labels


def _site_identity_keys(raw_site: dict[str, Any]) -> set[str]:
    raw_aliases = raw_site.get("aliases") or []
    aliases = raw_aliases if isinstance(raw_aliases, list) else []
    values = [raw_site.get("site_key"), raw_site.get("site_name"), *aliases]
    return {key for key in (_site_identity_key(value) for value in values) if key}


def _site_is_retired(raw_site: dict[str, Any]) -> bool:
    return raw_site.get("retired") is True


def _site_identity_key(value: Any) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    return "".join(normalized.split()).casefold()


def _parse_string_list(raw_site: dict[str, Any], field: str, label: str) -> tuple[str, ...]:
    return _parse_string_list_value(raw_site.get(field, []), label)


def _parse_string_list_value(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{label} must be a list of non-empty strings.")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _parse_meters(raw_site: dict[str, Any], config_file: Path) -> tuple[MeterConfig, ...]:
    raw_meters = raw_site.get("meters")
    if raw_meters is None:
        return (
            MeterConfig(
                meter_no=str(raw_site["meter_no"]),
                multiplier=_decimal_field(raw_site, "multiplier", config_file),
                role="primary",
                detail_enabled=True,
                grid_export_allocation="site_total",
            ),
        )
    if not isinstance(raw_meters, list) or not raw_meters:
        raise ConfigError(f"Site config field 'meters' in {config_file} must be a non-empty list.")

    meters: list[MeterConfig] = []
    seen: set[str] = set()
    for index, meter in enumerate(raw_meters, start=1):
        if not isinstance(meter, dict):
            raise ConfigError(f"Site config meter entry #{index} in {config_file} must be an object.")
        missing = [field for field in ("meter_no", "multiplier") if _is_blank(meter.get(field))]
        if missing:
            raise ConfigError(f"Site config meter entry #{index} is missing fields: {', '.join(missing)}.")
        meter_no = str(meter["meter_no"])
        if meter_no in seen:
            raise ConfigError(f"Duplicate meter_no '{meter_no}' in {config_file}.")
        seen.add(meter_no)
        allocation = str(meter.get("grid_export_allocation") or "none")
        if allocation not in GRID_EXPORT_ALLOCATIONS:
            raise ConfigError(f"Unsupported grid_export_allocation '{allocation}' in {config_file}.")
        meters.append(
            MeterConfig(
                meter_no=meter_no,
                multiplier=_decimal_field(meter, "multiplier", config_file),
                role=str(meter.get("role") or "meter"),
                detail_enabled=bool(meter.get("detail_enabled", False)),
                grid_export_allocation=allocation,
            )
        )

    if not any(meter.role == "primary" for meter in meters):
        raise ConfigError(f"Site config in {config_file} must contain one primary meter.")
    if not any(meter.detail_enabled for meter in meters):
        raise ConfigError(f"Site config in {config_file} must enable at least one detail meter.")
    _validate_site_total_grid_export_recipient(meters, config_file)
    return tuple(meters)


def _validate_site_total_grid_export_recipient(meters: list[MeterConfig], config_file: Path) -> None:
    recipients = [meter for meter in meters if meter.grid_export_allocation in GRID_EXPORT_SITE_TOTAL_ALLOCATIONS]
    if len(recipients) != 1:
        raise ConfigError(
            "Site config must assign the site-level grid_export_kwh value to exactly one meter "
            f"using grid_export_allocation=site_total in {config_file}."
        )


def _meter_topology_profile(raw_site: dict[str, Any], meters: tuple[MeterConfig, ...], config_file: Path) -> str:
    value = raw_site.get("meter_topology_profile")
    if _is_blank(value):
        profile = "single_meter_school" if len(meters) == 1 else "dual_meter_school" if len(meters) == 2 else None
        if profile is None:
            raise ConfigError(
                f"Site config in {config_file} must declare meter_topology_profile for {len(meters)} meters."
            )
        return profile
    profile = str(value).strip()
    if profile not in SUPPORTED_METER_TOPOLOGY_PROFILES:
        raise ConfigError(f"Unsupported meter_topology_profile '{profile}' in {config_file}.")
    if profile == "single_meter_school" and len(meters) != 1:
        raise ConfigError("single_meter_school requires exactly one configured meter.")
    if profile == "dual_meter_school" and len(meters) != 2:
        raise ConfigError("dual_meter_school requires exactly two configured meters.")
    return profile


def _template_dir(raw_site: dict[str, Any], config_file: Path) -> Path:
    configured = Path(str(raw_site["template_dir"]))
    return configured if configured.is_absolute() else (config_file.parent / configured).resolve()


def _decimal_field(raw_site: dict[str, Any], field: str, config_file: Path) -> Decimal:
    try:
        return Decimal(str(raw_site[field]))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"Site config field '{field}' in {config_file} must be numeric.") from exc


def _optional_decimal_field(raw_site: dict[str, Any], field: str, config_file: Path) -> Decimal | None:
    if _is_blank(raw_site.get(field)):
        return None
    try:
        return Decimal(str(raw_site[field]))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"Site config field '{field}' in {config_file} must be numeric.") from exc


def _summary_end_column(raw_site: dict[str, Any], config_file: Path) -> int:
    value = raw_site.get("summary_end_column", 13)
    if isinstance(value, str) and value.strip().isalpha():
        return _column_index(value.strip())
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Site config field 'summary_end_column' in {config_file} must be a column letter or integer.") from exc
    if parsed <= 0:
        raise ConfigError(f"Site config field 'summary_end_column' in {config_file} must be positive.")
    return parsed


def _summary_merge_columns(raw_site: dict[str, Any]) -> tuple[str, ...]:
    value = raw_site.get("summary_merge_columns")
    if value is None:
        return ("I", "M")
    columns = _parse_string_list_value(value, "Site config summary_merge_columns")
    return tuple(column.upper() for column in columns)


def _summary_month_cell_alignment(raw_site: dict[str, Any], config_file: Path) -> str | None:
    value = raw_site.get("summary_month_cell_alignment")
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text != "center":
        raise ConfigError(f"Site config field 'summary_month_cell_alignment' in {config_file} must be 'center'.")
    return text


def _validation_not_applicable_fields(raw_site: dict[str, Any]) -> tuple[str, ...]:
    fields = _parse_string_list(raw_site, "validation_not_applicable_fields", "Site config validation_not_applicable_fields")
    return tuple(dict.fromkeys(fields))


def _column_index(column: str) -> int:
    total = 0
    for char in column.upper():
        if char < "A" or char > "Z":
            raise ConfigError(f"Invalid column letter: {column}")
        total = total * 26 + ord(char) - ord("A") + 1
    return total

def _int_field(raw_site: dict[str, Any], field: str, config_file: Path, *, default: int) -> int:
    value = raw_site.get(field, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Site config field '{field}' in {config_file} must be an integer.") from exc
    if parsed <= 0:
        raise ConfigError(f"Site config field '{field}' in {config_file} must be positive.")
    return parsed


def _nonnegative_int_field(raw_site: dict[str, Any], field: str, config_file: Path, *, default: int) -> int:
    value = raw_site.get(field, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Site config field '{field}' in {config_file} must be an integer.") from exc
    if parsed < 0:
        raise ConfigError(f"Site config field '{field}' in {config_file} must be non-negative.")
    return parsed


def _optional_cell(raw_site: dict[str, Any], field: str, *, default: str | None) -> str | None:
    value = raw_site.get(field, default)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_field(raw_site: dict[str, Any], field: str, config_file: Path, *, default: bool) -> bool:
    value = raw_site.get(field, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no", ""}:
            return False
    raise ConfigError(f"Site config field '{field}' in {config_file} must be boolean.")

def _optional_key(raw_site: dict[str, Any], field: str) -> str | None:
    value = raw_site.get(field)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ConfigError(f"Site config field '{field}' must be a single safe key segment.")
    return text


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_year_month(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    if not year.isdigit() or not month.isdigit():
        return False
    return 1 <= int(month) <= 12
