import json
import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .mapping import NormalizerSpec


DEFAULT_TEXT_LIMIT = 50000
METADATA_TEXT_LIMIT = 500


class RowValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def is_missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _decimal(value):
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RowValidationError("invalid_decimal", "數值格式不正確") from exc
    if not result.is_finite():
        raise RowValidationError("invalid_decimal", "數值必須是有限值")
    return result


def parse_datetime(value):
    if is_missing(value):
        return None
    try:
        if isinstance(value, (int, float, Decimal)) or str(value).strip().lstrip("-").isdigit():
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError
            if abs(numeric) >= 100_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, tz=UTC)
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (OverflowError, OSError, ValueError, TypeError) as exc:
        raise RowValidationError("invalid_datetime", "日期時間格式不正確") from exc


def normalize_value(value, specs, *, required):
    if is_missing(value):
        if required:
            raise RowValidationError("missing_required", "必要欄位沒有值")
        return None

    result = value
    explicitly_truncated = False
    for spec in specs:
        result = _apply_normalizer(result, spec)
        explicitly_truncated = explicitly_truncated or spec.name == "safe_truncate"

    if isinstance(result, str):
        if required and not result.strip():
            raise RowValidationError("missing_required", "必要欄位正規化後為空")
        if not explicitly_truncated and len(result) > DEFAULT_TEXT_LIMIT:
            raise RowValidationError(
                "text_too_long",
                f"文字超過 {DEFAULT_TEXT_LIMIT} 字元限制",
            )
    return result


def _apply_normalizer(value, spec: NormalizerSpec):
    if spec.name == "strip":
        return str(value).strip()
    if spec.name == "integer":
        try:
            number = _decimal(value)
        except RowValidationError as exc:
            raise RowValidationError("invalid_integer", "整數格式不正確") from exc
        if number != number.to_integral_value():
            raise RowValidationError("invalid_integer", "整數欄位含有小數")
        return str(int(number))
    if spec.name == "decimal":
        number = _decimal(value)
        text = format(number, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    if spec.name == "boolean":
        normalized = str(value).strip().casefold()
        true_values = {str(item).strip().casefold() for item in spec.options["true_values"]}
        false_values = {str(item).strip().casefold() for item in spec.options["false_values"]}
        if normalized in true_values:
            return str(spec.options.get("true_label", "true"))
        if normalized in false_values:
            return str(spec.options.get("false_label", "false"))
        raise RowValidationError("invalid_boolean", "布林欄位無法對應")
    if spec.name == "datetime":
        parsed = parse_datetime(value)
        return parsed.isoformat() if parsed else None
    if spec.name == "text_length":
        return str(len(str(value)))
    if spec.name == "safe_truncate":
        return str(value)[: spec.options["max_length"]]
    raise RowValidationError("unsupported_normalizer", "不受支援的正規化規則")


def normalize_hash_component(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def sanitize_metadata(row, fields):
    metadata = {}
    for field in fields:
        value = row.get(field)
        if value is None:
            metadata[field] = None
        elif isinstance(value, (bool, int, float)):
            metadata[field] = value
        elif isinstance(value, str):
            metadata[field] = value[:METADATA_TEXT_LIMIT]
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            metadata[field] = text[:METADATA_TEXT_LIMIT]
    return metadata
