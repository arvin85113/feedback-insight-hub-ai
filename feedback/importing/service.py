import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from feedback.models import (
    Answer,
    DatasetImportBatch,
    FeedbackSubmission,
    ImportedSubmissionSource,
    Question,
    Survey,
)
from feedback.text_pipeline import ANALYSIS_VERSION, build_analysis_text, estimate_sentiment_score
from feedback.analysis_jobs import schedule_survey_analysis, suppress_analysis_scheduling

from .mapping import ImportMapping
from .normalizers import (
    RowValidationError,
    is_missing,
    normalize_hash_component,
    normalize_value,
    parse_datetime,
    sanitize_metadata,
)
from .readers import DatasetReaderError, file_sha256, iter_records


SAMPLING_METHOD = "reservoir_without_replacement"
ALL_ROWS_METHOD = "all_valid_rows"


@dataclass(frozen=True)
class PreparedRow:
    row_number: int
    source_record_key: str
    content_sha256: str
    source_item_id: str
    source_timestamp: object
    metadata: dict
    answers: tuple[str | None, ...]


@dataclass
class ImportResult:
    read_count: int = 0
    valid_count: int = 0
    sampled_count: int = 0
    imported_count: int = 0
    skipped_count: int = 0
    duplicate_count: int = 0
    conflict_count: int = 0
    skip_reasons: Counter = field(default_factory=Counter)
    missing_answer_counts: Counter = field(default_factory=Counter)
    input_duplicate_count: int = 0
    batch: DatasetImportBatch | None = None
    survey: Survey | None = None
    input_file_sha256: str = ""

    def summary(self):
        return {
            "valid_count": self.valid_count,
            "sampled_count": self.sampled_count,
            "input_duplicate_count": self.input_duplicate_count,
            "conflict_count": self.conflict_count,
            "missing_answer_counts": dict(sorted(self.missing_answer_counts.items())),
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
        }


def _source_record_key(mapping, row):
    digest = hashlib.sha256()
    parts = [
        ("dataset_name", mapping.dataset.name),
    ]
    for field_name in mapping.deduplication_fields:
        if field_name not in row:
            raise RowValidationError("missing_deduplication_field", "去重欄位不存在")
        value = normalize_hash_component(row.get(field_name))
        if not value:
            raise RowValidationError("missing_deduplication_value", "去重欄位沒有值")
        parts.append((field_name, value))
    for name, value in parts:
        encoded_name = name.encode("utf-8")
        encoded_value = normalize_hash_component(value).encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


def _content_sha256(*, answers, source_item_id, source_timestamp, metadata):
    payload = {
        "answers": list(answers),
        "source_item_id": source_item_id,
        "source_timestamp": source_timestamp.isoformat() if source_timestamp else None,
        "metadata": metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_row(mapping, source_record):
    row = source_record.data
    if row is None:
        raise RowValidationError(source_record.error_code or "invalid_row", "來源列格式不正確")

    answers = []
    for question in mapping.questions:
        value = normalize_value(
            row.get(question.source_field),
            question.normalizers,
            required=question.required,
        )
        if value is not None:
            value = str(value)
            if question.options and value not in question.options:
                raise RowValidationError("invalid_choice", "值不在題目允許選項內")
        answers.append(value)

    source_timestamp = None
    if mapping.timestamp_field:
        source_timestamp = parse_datetime(row.get(mapping.timestamp_field))
        if source_timestamp is None:
            raise RowValidationError("missing_timestamp", "來源時間沒有值")

    source_item_id = ""
    if mapping.source_item_id_field:
        source_item_id = normalize_hash_component(row.get(mapping.source_item_id_field))[:255]

    metadata = sanitize_metadata(row, mapping.metadata_fields)
    answers = tuple(answers)
    return PreparedRow(
        row_number=source_record.row_number,
        source_record_key=_source_record_key(mapping, row),
        content_sha256=_content_sha256(
            answers=answers,
            source_item_id=source_item_id,
            source_timestamp=source_timestamp,
            metadata=metadata,
        ),
        source_item_id=source_item_id,
        source_timestamp=source_timestamp,
        metadata=metadata,
        answers=answers,
    )


def _iter_prepared_rows(input_path, mapping, result):
    seen_records = {}
    for source_record in iter_records(input_path):
        result.read_count += 1
        if source_record.data is not None:
            optional_fields = {
                question.source_field for question in mapping.questions if not question.required
            }
            for field_name in optional_fields:
                if is_missing(source_record.data.get(field_name)):
                    result.missing_answer_counts[field_name] += 1
        try:
            prepared = _prepare_row(mapping, source_record)
        except RowValidationError as exc:
            result.skipped_count += 1
            result.skip_reasons[exc.code] += 1
            continue

        previous_content = seen_records.get(prepared.source_record_key)
        if previous_content is not None:
            if previous_content == prepared.content_sha256:
                result.duplicate_count += 1
                result.input_duplicate_count += 1
            else:
                result.conflict_count += 1
                result.skipped_count += 1
                result.skip_reasons["deduplication_conflict"] += 1
            continue
        seen_records[prepared.source_record_key] = prepared.content_sha256
        result.valid_count += 1

        yield prepared


def _prepare_sample(input_path, mapping, *, limit, seed):
    if limit is not None and limit < 1:
        raise ValueError("limit 必須 >= 1")
    result = ImportResult(input_file_sha256=file_sha256(input_path))
    if limit is None:
        for _prepared in _iter_prepared_rows(input_path, mapping, result):
            result.sampled_count += 1
        return result, ()

    rng = random.Random(seed)
    sample = []
    for prepared in _iter_prepared_rows(input_path, mapping, result):

        if len(sample) < limit:
            sample.append(prepared)
        else:
            replacement_index = rng.randrange(result.valid_count)
            if replacement_index < limit:
                sample[replacement_index] = prepared

    result.sampled_count = len(sample)
    return result, sample


def preview_dataset(input_path, mapping, *, limit, seed):
    result, _sample = _prepare_sample(input_path, mapping, limit=limit, seed=seed)
    return result


def _survey_slug(mapping):
    configured = slugify(mapping.survey.slug)
    if configured:
        return configured[:50]
    base = slugify(mapping.survey.title)[:50] or "imported-feedback"
    existing = Survey.objects.filter(slug=base).first()
    if not existing or existing.title == mapping.survey.title:
        return base
    suffix = hashlib.sha256(mapping.survey.title.encode("utf-8")).hexdigest()[:7]
    return f"{base[:42]}-{suffix}"


def _question_codes(mapping):
    codes = []
    used = set()
    for order, spec in enumerate(mapping.questions, start=1):
        base = slugify(f"{spec.source_field}-{spec.kind}")[:72] or f"field-{order}"
        code = base
        suffix = 2
        while code in used:
            code = f"{base[:72]}-{suffix}"
            suffix += 1
        used.add(code)
        codes.append(code)
    return codes


def _ensure_survey_and_questions(mapping):
    slug = _survey_slug(mapping)
    survey = Survey.objects.filter(slug=slug).first()
    if survey is None:
        survey = Survey.objects.create(
            title=mapping.survey.title,
            slug=slug,
            description=mapping.survey.description,
            thank_you_email_enabled=False,
            is_active=mapping.survey.is_active,
        )
    elif survey.title != mapping.survey.title:
        raise ValueError(f"問卷 slug {slug!r} 已被其他問卷使用")

    questions = []
    for order, (spec, code) in enumerate(zip(mapping.questions, _question_codes(mapping)), start=1):
        question = survey.questions.filter(code=code).first()
        matched_by_title = False
        if question is None:
            title_matches = survey.questions.filter(title=spec.title)
            if title_matches.count() > 1:
                raise ValueError(f"問卷中有重複題目名稱：{spec.title}")
            question = title_matches.first()
            matched_by_title = question is not None
        expected_options = "\n".join(spec.options)
        expected = {
            "code": code,
            "title": spec.title,
            "kind": spec.kind,
            "data_type": spec.data_type,
            "options_text": expected_options,
            "is_required": spec.required,
            "enable_keyword_tracking": spec.enable_keyword_tracking,
            "is_active": True,
            "order": order,
        }
        if question is None:
            question = Question.objects.create(survey=survey, **expected)
        else:
            if matched_by_title and question.code != code:
                if survey.questions.filter(code=code).exclude(pk=question.pk).exists():
                    raise ValueError(f"題目代碼 {code!r} 已被其他題目使用")
                question.code = code
                question.save(update_fields=("code",))
            conflicts = [name for name, value in expected.items() if getattr(question, name) != value]
            if conflicts:
                raise ValueError(f"既有題目 {spec.title!r} 與 mapping 不相容：{', '.join(conflicts)}")
        questions.append(question)
    return survey, questions


def _build_answer(submission, question, value):
    analysis_text = None
    sentiment_score = None
    analysis_version = None
    if question.kind in {Question.Kind.SHORT_TEXT, Question.Kind.LONG_TEXT}:
        analysis_text = build_analysis_text(value)
        if analysis_text:
            analysis_version = ANALYSIS_VERSION
            sentiment_score = estimate_sentiment_score(value)
    return Answer(
        submission=submission,
        question=question,
        value=value,
        analysis_text=analysis_text,
        sentiment_score=sentiment_score,
        analysis_version=analysis_version,
    )


def _chunks(values, size):
    chunk = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _import_chunk(survey, questions, batch, chunk, result):
    record_keys = [item.source_record_key for item in chunk]
    existing_records = dict(
        ImportedSubmissionSource.objects.filter(
            source_namespace=batch.source_name,
            source_record_key__in=record_keys,
        ).values_list(
            "source_record_key", "content_sha256"
        )
    )
    candidates = []
    for item in chunk:
        previous_content = existing_records.get(item.source_record_key)
        if previous_content is None:
            candidates.append(item)
        elif previous_content == item.content_sha256:
            result.duplicate_count += 1
        else:
            result.conflict_count += 1
            result.skipped_count += 1
            result.skip_reasons["deduplication_conflict"] += 1
    if not candidates:
        return

    submissions = [
        FeedbackSubmission(
            survey=survey,
            user=None,
            respondent_name="",
            respondent_email="",
            consent_follow_up=False,
        )
        for _item in candidates
    ]
    try:
        with transaction.atomic():
            FeedbackSubmission.objects.bulk_create(submissions, batch_size=len(submissions))
            timestamped = []
            for submission, item in zip(submissions, candidates):
                if item.source_timestamp is not None:
                    submission.submitted_at = item.source_timestamp
                    timestamped.append(submission)
            if timestamped:
                FeedbackSubmission.objects.bulk_update(
                    timestamped, ("submitted_at",), batch_size=len(timestamped)
                )
            answers = [
                _build_answer(submission, question, value)
                for submission, item in zip(submissions, candidates)
                for question, value in zip(questions, item.answers)
                if value is not None
            ]
            Answer.objects.bulk_create(answers, batch_size=5000)
            ImportedSubmissionSource.objects.bulk_create(
                [
                    ImportedSubmissionSource(
                        submission=submission,
                        batch=batch,
                        source_namespace=batch.source_name,
                        source_record_key=item.source_record_key,
                        content_sha256=item.content_sha256,
                        source_version=batch.source_version,
                        source_item_id=item.source_item_id,
                        source_timestamp=item.source_timestamp,
                        metadata=item.metadata,
                    )
                    for submission, item in zip(submissions, candidates)
                ],
                batch_size=len(submissions),
            )
    except IntegrityError:
        # A concurrent importer may win a source hash race. Retry only this small
        # chunk row-by-row so already committed source rows remain idempotent.
        for item in candidates:
            try:
                with transaction.atomic():
                    if ImportedSubmissionSource.objects.filter(
                        source_namespace=batch.source_name,
                        source_record_key=item.source_record_key,
                    ).exists():
                        existing_content = ImportedSubmissionSource.objects.filter(
                            source_namespace=batch.source_name,
                            source_record_key=item.source_record_key,
                        ).values_list("content_sha256", flat=True).first()
                        if existing_content == item.content_sha256:
                            result.duplicate_count += 1
                        else:
                            result.conflict_count += 1
                            result.skipped_count += 1
                            result.skip_reasons["deduplication_conflict"] += 1
                        continue
                    with suppress_analysis_scheduling():
                        submission = FeedbackSubmission.objects.create(
                            survey=survey,
                            user=None,
                            respondent_name="",
                            respondent_email="",
                            consent_follow_up=False,
                        )
                    if item.source_timestamp is not None:
                        FeedbackSubmission.objects.filter(pk=submission.pk).update(
                            submitted_at=item.source_timestamp
                        )
                    Answer.objects.bulk_create(
                        [
                            _build_answer(submission, question, value)
                            for question, value in zip(questions, item.answers)
                            if value is not None
                        ]
                    )
                    ImportedSubmissionSource.objects.create(
                        submission=submission,
                        batch=batch,
                        source_namespace=batch.source_name,
                        source_record_key=item.source_record_key,
                        content_sha256=item.content_sha256,
                        source_version=batch.source_version,
                        source_item_id=item.source_item_id,
                        source_timestamp=item.source_timestamp,
                        metadata=item.metadata,
                    )
            except IntegrityError:
                result.duplicate_count += 1
                continue
            result.imported_count += 1
        return
    result.imported_count += len(candidates)


def import_dataset(input_path, mapping, *, limit, seed, batch_size=100):
    if batch_size < 1 or batch_size > 5000:
        raise ValueError("batch_size 必須介於 1 到 5000")
    if limit is None:
        result = ImportResult(input_file_sha256=file_sha256(input_path))

        def prepared_rows():
            for prepared in _iter_prepared_rows(input_path, mapping, result):
                result.sampled_count += 1
                yield prepared

        sample = prepared_rows()
        sampling_method = ALL_ROWS_METHOD
    else:
        result, sample = _prepare_sample(input_path, mapping, limit=limit, seed=seed)
        sampling_method = SAMPLING_METHOD

    with transaction.atomic():
        with suppress_analysis_scheduling():
            survey, questions = _ensure_survey_and_questions(mapping)
        batch = DatasetImportBatch.objects.create(
            survey=survey,
            source_name=mapping.dataset.name,
            source_version=mapping.dataset.version,
            source_url=mapping.dataset.source_url,
            license_name=mapping.dataset.license_name,
            input_file_sha256=result.input_file_sha256,
            mapping_version=mapping.mapping_version,
            sampling_method=sampling_method,
            random_seed=None if limit is None else seed,
            requested_limit=limit,
            read_count=result.read_count,
            skipped_count=result.skipped_count,
            duplicate_count=result.duplicate_count,
            conflict_count=result.conflict_count,
            status=DatasetImportBatch.Status.RUNNING,
            summary=result.summary(),
        )
    result.batch = batch
    result.survey = survey

    try:
        for chunk in _chunks(sample, batch_size):
            with transaction.atomic():
                _import_chunk(survey, questions, batch, chunk, result)
                batch.imported_count = result.imported_count
                batch.read_count = result.read_count
                batch.skipped_count = result.skipped_count
                batch.duplicate_count = result.duplicate_count
                batch.conflict_count = result.conflict_count
                batch.summary = result.summary()
                batch.save(
                    update_fields=(
                        "read_count",
                        "imported_count",
                        "skipped_count",
                        "duplicate_count",
                        "conflict_count",
                        "summary",
                    )
                )
    except Exception:
        with transaction.atomic():
            batch.status = DatasetImportBatch.Status.FAILED
            batch.completed_at = timezone.now()
            batch.imported_count = result.imported_count
            batch.read_count = result.read_count
            batch.skipped_count = result.skipped_count
            batch.duplicate_count = result.duplicate_count
            batch.conflict_count = result.conflict_count
            batch.summary = result.summary()
            batch.save(
                update_fields=(
                    "status",
                    "completed_at",
                    "read_count",
                    "imported_count",
                    "skipped_count",
                    "duplicate_count",
                    "conflict_count",
                    "summary",
                )
            )
            if result.imported_count:
                schedule_survey_analysis(survey.pk, change="both")
        raise

    with transaction.atomic():
        batch.status = DatasetImportBatch.Status.COMPLETED
        batch.completed_at = timezone.now()
        batch.imported_count = result.imported_count
        batch.read_count = result.read_count
        batch.skipped_count = result.skipped_count
        batch.duplicate_count = result.duplicate_count
        batch.conflict_count = result.conflict_count
        batch.summary = result.summary()
        batch.save(
            update_fields=(
                "status",
                "completed_at",
                "read_count",
                "imported_count",
                "skipped_count",
                "duplicate_count",
                "conflict_count",
                "summary",
            )
        )
        if result.imported_count:
            schedule_survey_analysis(survey.pk, change="both")
    return result
