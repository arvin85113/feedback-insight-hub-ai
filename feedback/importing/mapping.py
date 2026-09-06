import json
import re
from dataclasses import dataclass
from pathlib import Path

from feedback.models import Question


ALLOWED_NORMALIZERS = {
    "strip",
    "integer",
    "decimal",
    "boolean",
    "datetime",
    "text_length",
    "safe_truncate",
}
SENSITIVE_FIELDS = {"user_id", "email", "name", "phone", "address"}


class MappingConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizerSpec:
    name: str
    options: dict


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    version: str
    source_url: str
    license_name: str


@dataclass(frozen=True)
class SurveySpec:
    title: str
    slug: str
    description: str
    is_active: bool


@dataclass(frozen=True)
class QuestionSpec:
    source_field: str
    title: str
    kind: str
    data_type: str
    required: bool
    normalizers: tuple[NormalizerSpec, ...]
    options: tuple[str, ...]
    enable_keyword_tracking: bool


@dataclass(frozen=True)
class HuggingFacePreparationSpec:
    config: str
    split: str
    output_fields: tuple[str, ...]
    required_fields: tuple[str, ...]
    identity_hash_source_fields: tuple[str, ...]
    identity_hash_output_field: str


@dataclass(frozen=True)
class ImportMapping:
    mapping_version: str
    dataset: DatasetSpec
    survey: SurveySpec
    timestamp_field: str
    deduplication_fields: tuple[str, ...]
    source_item_id_field: str
    metadata_fields: tuple[str, ...]
    questions: tuple[QuestionSpec, ...]
    huggingface_preparation: HuggingFacePreparationSpec | None


def _required_text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise MappingConfigError(f"{path} 必須是非空字串")
    return value.strip()


def _optional_text(value, path):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MappingConfigError(f"{path} 必須是字串")
    return value.strip()


def _text_list(value, path, *, required=False):
    if not isinstance(value, list):
        raise MappingConfigError(f"{path} 必須是字串陣列")
    result = []
    for index, item in enumerate(value):
        result.append(_required_text(item, f"{path}[{index}]"))
    if required and not result:
        raise MappingConfigError(f"{path} 至少需要一個欄位")
    if len(result) != len(set(result)):
        raise MappingConfigError(f"{path} 不得包含重複欄位")
    return tuple(result)


def _safe_field_name(value, path):
    name = _required_text(value, path)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise MappingConfigError(f"{path} 必須是安全的欄位名稱")
    return name


def _preparation_spec(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MappingConfigError("huggingface_preparation 必須是物件")

    output_fields = _text_list(
        value.get("output_fields"),
        "huggingface_preparation.output_fields",
        required=True,
    )
    required_fields = _text_list(
        value.get("required_fields", []),
        "huggingface_preparation.required_fields",
    )
    identity_fields = _text_list(
        value.get("identity_hash_source_fields"),
        "huggingface_preparation.identity_hash_source_fields",
        required=True,
    )
    for path, fields in (
        ("huggingface_preparation.output_fields", output_fields),
        ("huggingface_preparation.required_fields", required_fields),
        ("huggingface_preparation.identity_hash_source_fields", identity_fields),
    ):
        for index, field_name in enumerate(fields):
            _safe_field_name(field_name, f"{path}[{index}]")

    sensitive_outputs = {item.casefold() for item in output_fields} & SENSITIVE_FIELDS
    if sensitive_outputs:
        names = ", ".join(sorted(sensitive_outputs))
        raise MappingConfigError(f"遠端樣本不得輸出敏感欄位：{names}")
    missing_required = set(required_fields) - set(output_fields)
    if missing_required:
        names = ", ".join(sorted(missing_required))
        raise MappingConfigError(f"必要欄位未列入 output_fields：{names}")

    hash_output = _safe_field_name(
        value.get("identity_hash_output_field"),
        "huggingface_preparation.identity_hash_output_field",
    )
    if hash_output.casefold() in SENSITIVE_FIELDS or hash_output in output_fields:
        raise MappingConfigError("identity hash 輸出欄位不安全或與來源欄位重複")
    return HuggingFacePreparationSpec(
        config=_required_text(value.get("config"), "huggingface_preparation.config"),
        split=_required_text(value.get("split"), "huggingface_preparation.split"),
        output_fields=output_fields,
        required_fields=required_fields,
        identity_hash_source_fields=identity_fields,
        identity_hash_output_field=hash_output,
    )


def _normalizer_spec(value, path):
    if isinstance(value, str):
        name = value.strip()
        options = {}
    elif isinstance(value, dict):
        name = _required_text(value.get("name"), f"{path}.name")
        options = {key: item for key, item in value.items() if key != "name"}
    else:
        raise MappingConfigError(f"{path} 必須是字串或物件")

    if name not in ALLOWED_NORMALIZERS:
        raise MappingConfigError(f"{path} 使用不允許的正規化規則：{name}")
    if name == "safe_truncate":
        max_length = options.get("max_length")
        if not isinstance(max_length, int) or not 1 <= max_length <= 50000:
            raise MappingConfigError(f"{path}.max_length 必須介於 1 到 50000")
    if name == "boolean":
        for key in ("true_values", "false_values"):
            values = options.get(key)
            if not isinstance(values, list) or not values:
                raise MappingConfigError(f"{path}.{key} 必須是非空陣列")
        true_set = {str(item).strip().casefold() for item in options["true_values"]}
        false_set = {str(item).strip().casefold() for item in options["false_values"]}
        if true_set & false_set:
            raise MappingConfigError(f"{path} 的 true_values 與 false_values 不得重疊")
    return NormalizerSpec(name=name, options=options)


def _question_spec(value, index):
    path = f"questions[{index}]"
    if not isinstance(value, dict):
        raise MappingConfigError(f"{path} 必須是物件")
    kind = _required_text(value.get("kind"), f"{path}.kind")
    data_type = _required_text(value.get("data_type"), f"{path}.data_type")
    if kind not in Question.Kind.values:
        raise MappingConfigError(f"{path}.kind 不受支援：{kind}")
    if data_type not in Question.DataType.values:
        raise MappingConfigError(f"{path}.data_type 不受支援：{data_type}")
    allowed_types = {
        Question.Kind.SHORT_TEXT: {Question.DataType.TEXT},
        Question.Kind.LONG_TEXT: {Question.DataType.TEXT},
        Question.Kind.SINGLE_CHOICE: {Question.DataType.NOMINAL, Question.DataType.ORDINAL},
        Question.Kind.MULTIPLE_CHOICE: {Question.DataType.NOMINAL},
        Question.Kind.INTEGER: {Question.DataType.DISCRETE},
        Question.Kind.DECIMAL: {Question.DataType.CONTINUOUS},
        Question.Kind.SCALE: {Question.DataType.ORDINAL},
    }
    if data_type not in allowed_types[kind]:
        raise MappingConfigError(f"{path} 的 kind 與 data_type 不相容")
    normalizers_value = value.get("normalizers", [])
    if not isinstance(normalizers_value, list):
        raise MappingConfigError(f"{path}.normalizers 必須是陣列")
    options = _text_list(value.get("options", []), f"{path}.options")
    if kind in {Question.Kind.SINGLE_CHOICE, Question.Kind.MULTIPLE_CHOICE, Question.Kind.SCALE} and not options:
        raise MappingConfigError(f"{path}.options 對選項／量表題不可為空")
    return QuestionSpec(
        source_field=_required_text(value.get("source_field"), f"{path}.source_field"),
        title=_required_text(value.get("title"), f"{path}.title"),
        kind=kind,
        data_type=data_type,
        required=bool(value.get("required", True)),
        normalizers=tuple(
            _normalizer_spec(item, f"{path}.normalizers[{normalizer_index}]")
            for normalizer_index, item in enumerate(normalizers_value)
        ),
        options=options,
        enable_keyword_tracking=bool(value.get("enable_keyword_tracking", False)),
    )


def load_mapping(path):
    mapping_path = Path(path)
    try:
        raw = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MappingConfigError(f"無法讀取 mapping JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise MappingConfigError("mapping 最外層必須是 JSON 物件")

    dataset = raw.get("dataset")
    survey = raw.get("survey")
    questions = raw.get("questions")
    if not isinstance(dataset, dict):
        raise MappingConfigError("dataset 必須是物件")
    if not isinstance(survey, dict):
        raise MappingConfigError("survey 必須是物件")
    if not isinstance(questions, list) or not questions:
        raise MappingConfigError("questions 必須是非空陣列")

    question_specs = tuple(_question_spec(item, index) for index, item in enumerate(questions))
    titles = [item.title for item in question_specs]
    if len(titles) != len(set(titles)):
        raise MappingConfigError("questions.title 不得重複")
    sensitive_answers = {
        item.source_field.casefold()
        for item in question_specs
        if item.source_field.casefold() in SENSITIVE_FIELDS
    }
    if sensitive_answers:
        names = ", ".join(sorted(sensitive_answers))
        raise MappingConfigError(f"敏感欄位不得映射為 Answer：{names}")

    metadata_fields = _text_list(raw.get("metadata_fields", []), "metadata_fields")
    sensitive_metadata = {item.casefold() for item in metadata_fields} & SENSITIVE_FIELDS
    if sensitive_metadata:
        names = ", ".join(sorted(sensitive_metadata))
        raise MappingConfigError(f"敏感欄位不得寫入 metadata：{names}")

    source_item_id_field = _optional_text(raw.get("source_item_id_field"), "source_item_id_field")
    if source_item_id_field.casefold() in SENSITIVE_FIELDS:
        raise MappingConfigError("source_item_id_field 不得使用敏感欄位")

    preparation = _preparation_spec(raw.get("huggingface_preparation"))
    mapping = ImportMapping(
        mapping_version=_required_text(raw.get("mapping_version"), "mapping_version"),
        dataset=DatasetSpec(
            name=_required_text(dataset.get("name"), "dataset.name"),
            version=_required_text(dataset.get("version"), "dataset.version"),
            source_url=_optional_text(dataset.get("source_url"), "dataset.source_url"),
            license_name=_optional_text(dataset.get("license"), "dataset.license"),
        ),
        survey=SurveySpec(
            title=_required_text(survey.get("title"), "survey.title"),
            slug=_optional_text(survey.get("slug"), "survey.slug"),
            description=_optional_text(survey.get("description"), "survey.description"),
            is_active=bool(survey.get("is_active", False)),
        ),
        timestamp_field=_optional_text(raw.get("timestamp_field"), "timestamp_field"),
        deduplication_fields=_text_list(
            raw.get("deduplication_fields"),
            "deduplication_fields",
            required=True,
        ),
        source_item_id_field=source_item_id_field,
        metadata_fields=metadata_fields,
        questions=question_specs,
        huggingface_preparation=preparation,
    )
    if preparation is not None:
        available_fields = set(preparation.output_fields) | {
            preparation.identity_hash_output_field
        }
        referenced_fields = {
            mapping.timestamp_field,
            mapping.source_item_id_field,
            *mapping.deduplication_fields,
            *mapping.metadata_fields,
            *(question.source_field for question in mapping.questions),
        }
        referenced_fields.discard("")
        missing_fields = referenced_fields - available_fields
        if missing_fields:
            names = ", ".join(sorted(missing_fields))
            raise MappingConfigError(f"mapping 引用未輸出的欄位：{names}")
    return mapping
