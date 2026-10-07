"""Portable, bounded history copies. No cloud ownership change, ACK or publication.

The exporter reads one database snapshot. The importer uses the existing generic
import provenance models, not inbox sequence numbers or cloud definition revisions.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import uuid
import zipfile

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cloudapi.definition import QUESTION_FIELDS, apply_definition, serialize_definition, validate_definition
from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.analysis_sources import resolve_analysis_source
from feedback.models import Answer, DatasetImportBatch, FeedbackSubmission, ImportedSubmissionSource, Survey

FORMAT = "feedback-history-v1"
MAX_BYTES = 256 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_SURVEYS = 20
MAX_RECORDS = 200000
COPY_NAMESPACE = uuid.UUID("7f5f48ea-00e2-44e8-a360-eef7a921aa11")


class HistoryError(ValueError):
    """Safe messages only: never include answers, contacts, credentials or paths."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def origin_name(value):
    url = urlsplit(value)
    if (len(value) > 500 or url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")):
        raise HistoryError("來源須為不含帳密、路徑或查詢參數的 HTTPS 網址")
    return f"https://{url.netloc.lower()}"


def _time(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise HistoryError("原始時間格式錯誤")
    result = parse_datetime(value)
    if result is None or timezone.is_naive(result):
        raise HistoryError("原始時間必須包含時區")
    return result


def _iso(value):
    return value.isoformat() if value else None


def _record(submission, answers, *, contacts):
    source = getattr(submission, "imported_source", None)
    return {
        "submission_uuid": str(submission.idempotency_key),
        "submitted_at": _iso(submission.submitted_at),
        "definition_version": submission.definition_version,
        "consent_follow_up": submission.consent_follow_up,
        "is_complete": submission.is_complete,
        "voided_at": _iso(submission.voided_at),
        # Cloud User primary keys never become local accounts or export metadata.
        "respondent_name": submission.respondent_name if contacts else "",
        "respondent_email": submission.respondent_email if contacts else "",
        "provenance": ({
            "source_namespace": source.source_namespace,
            "source_record_key": source.source_record_key,
            "content_sha256": source.content_sha256,
            "source_version": source.source_version,
            "source_timestamp": _iso(source.source_timestamp),
        } if source else None),
        "answers": [{"question_uuid": str(answer.question.uuid), "value": answer.value,
                     "choice_codes": answer.choice_codes} for answer in answers],
    }


def export_history(slugs, destination, *, origin, contacts=False):
    """Explicit CLI operation: SELECTs only, one snapshot, never overwrite a file.

    PostgreSQL enforces READ ONLY / REPEATABLE READ before the first SELECT.
    SQLite's first read pins its transaction snapshot (isolated fixtures only).
    """
    if settings.IS_NODE:
        raise HistoryError("請在雲端模式匯出，不能把節點副本當作雲端正本")
    if connection.in_atomic_block:
        raise HistoryError("匯出須在獨立交易執行，不能沿用不明的讀取快照")
    slugs = list(dict.fromkeys(slugs))
    if not slugs or len(slugs) > MAX_SURVEYS:
        raise HistoryError("每份資料包須明確選擇 1–20 份問卷")
    origin = origin_name(origin)
    destination = Path(destination)
    if destination.exists():
        raise HistoryError("目的檔已存在，不會覆蓋")
    fd, temporary = tempfile.mkstemp(prefix=".history-", suffix=".part", dir=destination.parent)
    os.close(fd)
    manifest = {"format": FORMAT, "origin": origin, "contacts_included": bool(contacts), "surveys": []}
    total_bytes = 0
    try:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            surveys = list(Survey.objects.filter(slug__in=slugs).select_related("category").order_by("uuid"))
            if len(surveys) != len(slugs):
                raise HistoryError("部分指定問卷不存在，未產生資料包")
            manifest["captured_at"] = timezone.now().isoformat()
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for survey in surveys:
                    if resolve_analysis_source(survey).is_external:
                        raise HistoryError("外部資料問卷請使用資料集頁接回既有 Parquet，不從雲端匯出評論")
                    definition = serialize_definition(survey)
                    name = f"{survey.uuid}.jsonl"
                    checksum, count, answer_count = hashlib.sha256(), 0, 0
                    queryset = survey.submissions.select_related("imported_source").order_by("pk")
                    # Bounded related reads, not one query per submission.
                    after = 0
                    with archive.open(name, "w") as stream:
                        while True:
                            batch = list(queryset.filter(pk__gt=after)[:500])
                            if not batch:
                                break
                            grouped = {sub.pk: [] for sub in batch}
                            for answer in Answer.objects.filter(submission_id__in=grouped).select_related("question").order_by("question__uuid"):
                                if answer.question.survey_id != survey.pk:
                                    raise HistoryError("回答與題目的問卷關聯不一致，匯出中止")
                                grouped[answer.submission_id].append(answer)
                            for sub in batch:
                                raw = encoded(_record(sub, grouped[sub.pk], contacts=contacts)) + b"\n"
                                total_bytes += len(raw)
                                count += 1
                                answer_count += len(grouped[sub.pk])
                                if len(raw) > MAX_LINE or count > MAX_RECORDS or total_bytes > MAX_BYTES:
                                    raise HistoryError("資料包超過安全上限，請縮小問卷範圍")
                                stream.write(raw)
                                checksum.update(raw)
                            after = batch[-1].pk
                    manifest["surveys"].append({"definition": definition, "definition_sha256": digest(definition),
                        "records_file": name, "records_sha256": checksum.hexdigest(),
                        "submission_count": count, "answer_count": answer_count})
                raw_manifest = encoded(manifest)
                if total_bytes + len(raw_manifest) > MAX_BYTES:
                    raise HistoryError("資料包超過安全上限")
                archive.writestr("manifest.json", raw_manifest)
        # Atomic publish without replacing an existing file, including a race at destination.
        os.link(temporary, destination)
        return {"surveys": len(surveys), "submissions": sum(s["submission_count"] for s in manifest["surveys"]),
                "answers": sum(s["answer_count"] for s in manifest["surveys"])}
    finally:
        Path(temporary).unlink(missing_ok=True)


def _validate_record(record, question_ids):
    try:
        if str(uuid.UUID(record["submission_uuid"])) != record["submission_uuid"]:
            raise HistoryError("回覆識別必須使用標準 UUID")
        _time(record["submitted_at"])
        _time(record["voided_at"], optional=True)
        for field in ("consent_follow_up", "is_complete"):
            if type(record[field]) is not bool:
                raise HistoryError("回覆狀態格式錯誤")
        version = record["definition_version"]
        if version is not None and (type(version) is not int or version < 0):
            raise HistoryError("填答定義版本格式錯誤")
        for field, length in (("respondent_name", 120), ("respondent_email", 254)):
            if not isinstance(record[field], str) or len(record[field]) > length:
                raise HistoryError("填答者快照格式錯誤")
        answers = record["answers"]
        if not isinstance(answers, list) or len(answers) > len(question_ids):
            raise HistoryError("答案數不符")
        seen = set()
        for answer in answers:
            if set(answer) != {"question_uuid", "value", "choice_codes"}:
                raise HistoryError("答案包含未支援欄位")
            key = answer["question_uuid"]
            if key not in question_ids or key in seen or not isinstance(answer["value"], str):
                raise HistoryError("題目關聯不符或答案重複")
            codes = answer["choice_codes"]
            if codes is not None and (not isinstance(codes, list) or not all(isinstance(c, str) for c in codes)):
                raise HistoryError("選項代碼格式錯誤")
            seen.add(key)
        # Reject extra fields (e.g. account IDs) rather than silently importing unknown metadata.
        if set(record) != {"submission_uuid", "submitted_at", "definition_version", "consent_follow_up",
                "is_complete", "voided_at", "respondent_name", "respondent_email", "provenance", "answers"}:
            raise HistoryError("資料包包含未支援欄位")
        provenance = record["provenance"]
        if provenance is not None:
            if not isinstance(provenance, dict) or set(provenance) != {
                "source_namespace", "source_record_key", "content_sha256", "source_version", "source_timestamp"}:
                raise HistoryError("來源關聯格式錯誤")
            _time(provenance["source_timestamp"], optional=True)
            if not all(isinstance(v, str) for k, v in provenance.items() if k != "source_timestamp"):
                raise HistoryError("來源關聯格式錯誤")
        encoded(record)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, HistoryError):
            raise
        raise HistoryError("回覆格式錯誤") from exc


class HistoryBundle:
    """Reads ZIP entries only; never extracts paths. One open file for validate + import."""

    def __init__(self, path):
        # Freeze the bytes before validation: preview/import cannot observe a file
        # being edited in place after its hash was checked. TemporaryFile is private.
        self.file = tempfile.TemporaryFile()
        self.archive = None
        try:
            total = 0
            with open(path, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HistoryError("資料包超過安全上限")
                    self.file.write(chunk)
            self.file.seek(0)
            self.sha256 = hashlib.file_digest(self.file, "sha256").hexdigest()
            self.file.seek(0)
            self.archive = zipfile.ZipFile(self.file)
            entries = self.archive.infolist()
            if (len(entries) > MAX_SURVEYS + 1 or len({e.filename for e in entries}) != len(entries)
                    or sum(e.file_size for e in entries) > MAX_BYTES):
                raise HistoryError("資料包項目重複或超過安全上限")
            if self.archive.getinfo("manifest.json").file_size > MAX_LINE:
                raise HistoryError("資料包索引過大")
            self.manifest = json.loads(self.archive.read("manifest.json"))
            if set(self.manifest) != {"format", "origin", "contacts_included", "surveys", "captured_at"}:
                raise HistoryError("資料包索引包含未支援欄位")
            if self.manifest.get("format") != FORMAT:
                raise HistoryError("不支援此資料包版本")
            self.origin = origin_name(self.manifest["origin"])
            _time(self.manifest["captured_at"])
            self.surveys = self.manifest["surveys"]
            if (not isinstance(self.surveys, list) or not 1 <= len(self.surveys) <= MAX_SURVEYS
                    or type(self.manifest["contacts_included"]) is not bool):
                raise HistoryError("資料包索引格式錯誤")
            names, ids = {"manifest.json"}, set()
            for survey in self.surveys:
                if set(survey) != {"definition", "definition_sha256", "records_file", "records_sha256",
                                   "submission_count", "answer_count"}:
                    raise HistoryError("問卷索引包含未支援欄位")
                if any(type(survey[field]) is not int or survey[field] < 0
                       for field in ("submission_count", "answer_count")):
                    raise HistoryError("資料包筆數格式錯誤")
                raw_definition = survey["definition"]
                if set(raw_definition) != {"schema_version", "survey_uuid", "version", "title", "slug", "description",
                    "is_active", "analysis_enabled", "thank_you_email_enabled", "improvement_tracking_enabled",
                    "category", "archived_at", "published", "published_version", "published_at",
                    "analysis_definition_version", "next_question_number", "questions"}:
                    raise HistoryError("問卷定義包含未支援欄位")
                definition = validate_definition(survey["definition"])
                uid = definition["survey_uuid"]
                if uid in ids or str(uuid.UUID(uid)) != uid or raw_definition["schema_version"] != 2:
                    raise HistoryError("問卷重複或為外部資料來源")
                for question in raw_definition["questions"]:
                    if (set(question) != {"uuid", *QUESTION_FIELDS}
                            or str(uuid.UUID(question["uuid"])) != question["uuid"]):
                        raise HistoryError("題目定義包含未支援欄位或識別格式錯誤")
                ids.add(uid)
                if digest(survey["definition"]) != survey["definition_sha256"]:
                    raise HistoryError("問卷定義雜湊不符")
                if survey["records_file"] != f"{uid}.jsonl":
                    raise HistoryError("資料包檔名與問卷不符")
                names.add(survey["records_file"])
                checksum, count, answer_count = hashlib.sha256(), 0, 0
                seen = set()
                question_ids = {q["uuid"] for q in definition["questions"]}
                for raw, record in self.records(survey):
                    _validate_record(record, question_ids)
                    if record["submission_uuid"] in seen:
                        raise HistoryError("資料包內有重複回覆識別")
                    seen.add(record["submission_uuid"])
                    if not self.manifest["contacts_included"] and (record["respondent_name"] or record["respondent_email"]):
                        raise HistoryError("資料包聯絡資料旗標不符")
                    checksum.update(raw)
                    count += 1
                    answer_count += len(record["answers"])
                    if count > MAX_RECORDS:
                        raise HistoryError("回覆筆數超過安全上限")
                if (count != survey["submission_count"] or answer_count != survey["answer_count"]
                        or checksum.hexdigest() != survey["records_sha256"]):
                    raise HistoryError("筆數、答案數或內容雜湊不符")
            if {e.filename for e in entries} != names:
                raise HistoryError("資料包含未知檔案")
        except Exception as exc:
            self.close()
            if isinstance(exc, (OSError, HistoryError)):
                raise
            raise HistoryError("無法驗證資料包") from exc

    def records(self, survey):
        with self.archive.open(survey["records_file"]) as stream:
            while raw := stream.readline(MAX_LINE + 1):
                if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                    raise HistoryError("單筆回覆過大或資料包不完整")
                yield raw, json.loads(raw)

    def close(self):
        if self.archive is not None:
            self.archive.close()
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def preview(self):
        return {"sha256": self.sha256, "origin": self.origin, "captured_at": self.manifest["captured_at"],
                "contacts_included": self.manifest["contacts_included"],
                "surveys": [{"title": s["definition"]["title"], "submissions": s["submission_count"],
                    "answers": s["answer_count"], "definition_version": s["definition"]["version"]} for s in self.surveys]}


def _local_identity(origin, survey_uuid):
    namespace = f"cloud-history:{hashlib.sha256(origin.encode()).hexdigest()[:32]}:{survey_uuid}"
    return namespace, uuid.uuid5(COPY_NAMESPACE, namespace)


def import_history(bundle):
    """All-or-nothing copy. Recheck local rows on retry, not only provenance hashes.

    Keeps unknown historical definition versions unknown. No ACK, cloud revision,
    sync watermark, email, auto-analysis, Gemini or upload is produced.
    """
    if not settings.IS_NODE:
        raise HistoryError("歷史資料只能匯入本機節點")
    report = {"package_sha256": bundle.sha256, "surveys": []}
    with transaction.atomic(), suppress_analysis_scheduling():
        for item in bundle.surveys:
            original = item["definition"]
            namespace, local_uuid = _local_identity(bundle.origin, original["survey_uuid"])
            survey = Survey.objects.select_for_update().filter(uuid=local_uuid).first()
            previous = DatasetImportBatch.objects.filter(survey=survey, source_name="cloud-history").first() if survey else None
            if survey and (previous is None or previous.summary.get("definition_sha256") != item["definition_sha256"]):
                raise HistoryError("歷史副本已有不同的問卷定義；未覆蓋，請另行核對")
            transformed = copy.deepcopy(original)
            transformed.update(survey_uuid=str(local_uuid), slug=f"history-{local_uuid.hex}",
                title=f"{original['title'][:245]} · 歷史副本", is_active=False, analysis_enabled=True,
                archived_at=None, thank_you_email_enabled=False, improvement_tracking_enabled=False)
            questions = {}
            for question in transformed["questions"]:
                cloud_uuid = question["uuid"]
                question["uuid"] = str(uuid.uuid5(local_uuid, cloud_uuid))
                questions[cloud_uuid] = question["uuid"]
            if survey is None:
                survey = Survey(uuid=local_uuid)
                apply_definition(survey, transformed, version=original["version"])
            else:
                # A user-edited historical copy cannot silently accept new rows.
                current = serialize_definition(survey)
                if digest(current) != digest(transformed):
                    raise HistoryError("本機副本定義已變更，請核對後再匯入")
            batch = DatasetImportBatch.objects.filter(survey=survey, source_name="cloud-history",
                input_file_sha256=bundle.sha256).first()
            if batch is None:
                batch = DatasetImportBatch.objects.create(survey=survey, source_name="cloud-history",
                    source_version=bundle.manifest["captured_at"], source_url=bundle.origin,
                    input_file_sha256=bundle.sha256, mapping_version=FORMAT, sampling_method="all_history_rows",
                    random_seed=None, status=DatasetImportBatch.Status.RUNNING,
                    summary={"definition_sha256": item["definition_sha256"], "original_survey_uuid": original["survey_uuid"],
                             "original_slug": original["slug"], "historical_definition": original,
                             "historical_versions": "unknown_when_null", "copy_only": True})
            question_map = {key: survey.questions.get(uuid=value) for key, value in questions.items()}
            written, duplicates = 0, 0
            for _, row in bundle.records(item):
                key, content_hash = digest(row["submission_uuid"]), digest(row)
                local_key = uuid.uuid5(local_uuid, row["submission_uuid"])
                source = ImportedSubmissionSource.objects.select_related("submission").filter(
                    source_namespace=namespace, source_record_key=key).first()
                fields = {"submitted_at": _time(row["submitted_at"]), "definition_version": row["definition_version"],
                    "consent_follow_up": row["consent_follow_up"], "is_complete": row["is_complete"],
                    "voided_at": _time(row["voided_at"], optional=True), "respondent_name": row["respondent_name"],
                    "respondent_email": row["respondent_email"]}
                if source:
                    sub = source.submission
                    actual = {str(a.question.uuid): (a.value, a.choice_codes) for a in sub.answers.all()}
                    expected = {questions[a["question_uuid"]]: (a["value"], a["choice_codes"]) for a in row["answers"]}
                    if (source.content_sha256 != content_hash or sub.survey_id != survey.pk
                            or sub.idempotency_key != local_key or sub.user_id is not None
                            or source.source_item_id != row["submission_uuid"]
                            or source.metadata != {"original_provenance": row["provenance"], "definition_version": row["definition_version"]}
                            or any(getattr(sub, f) != v for f, v in fields.items()) or actual != expected):
                        raise HistoryError("同一來源回覆內容衝突或本機資料已變更；整批未寫入")
                    duplicates += 1
                    continue
                if FeedbackSubmission.objects.filter(idempotency_key=local_key).exists():
                    raise HistoryError("回覆識別衝突；整批未寫入")
                sub = FeedbackSubmission.objects.create(survey=survey, idempotency_key=local_key, **fields)
                Answer.objects.bulk_create([Answer(submission=sub, question=question_map[a["question_uuid"]],
                    value=a["value"], choice_codes=a["choice_codes"]) for a in row["answers"]])
                ImportedSubmissionSource.objects.create(submission=sub, batch=batch, source_namespace=namespace,
                    source_record_key=key, content_sha256=content_hash, source_version=batch.source_version,
                    source_item_id=row["submission_uuid"], source_timestamp=sub.submitted_at,
                    metadata={"original_provenance": row["provenance"], "definition_version": row["definition_version"]})
                written += 1
            survey.questions.filter(answers__isnull=False).update(has_received_answer=True)
            batch.status = DatasetImportBatch.Status.COMPLETED
            batch.read_count = item["submission_count"]
            batch.imported_count = batch.submission_sources.count()
            batch.duplicate_count = duplicates
            batch.completed_at = timezone.now()
            batch.summary = {**batch.summary, "reconciled": True, "expected_answers": item["answer_count"],
                             "records_sha256": item["records_sha256"], "copied_this_run": written}
            batch.save()
            report["surveys"].append({"title": original["title"], "local_slug": survey.slug,
                "expected_submissions": item["submission_count"], "expected_answers": item["answer_count"],
                "local_total_submissions": survey.submissions.count(),
                "local_total_answers": Answer.objects.filter(submission__survey=survey).count(),
                "copied": written, "duplicates": duplicates, "reconciled": True})
    return report
