"""Desktop boundary for Supabase survey status, local analysis, and publication."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[str, int], None]
CancelCallback = Callable[[], bool]


class DesktopServiceError(RuntimeError):
    pass


class DesktopTaskCancelled(DesktopServiceError):
    pass


@dataclass(frozen=True)
class DatasetStatus:
    dataset: str
    source_revision: str
    conversion_revision: str
    cleaning_version: str
    input_rows: int
    retained_rows: int
    clean_path: str
    clean_size: int
    clean_sha256: str
    verified: bool

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AnalysisSummary:
    result_path: str
    cache_hit: bool
    input_rows: int
    chart_count: int
    available_test_count: int
    keyword_count: int
    text_coverage: float
    ai_mode: str
    elapsed_seconds: float

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SurveyStatus:
    survey_id: int
    title: str
    slug: str
    source: str
    is_active: bool
    question_count: int
    response_count: int
    latest_data_at: datetime | None
    latest_analysis_at: datetime | None
    latest_ai_at: datetime | None
    statistics_current: bool
    text_current: bool
    ai_current: bool
    needs_update: bool
    needs_ai: bool
    latest_job_status: str
    latest_ai_job_status: str
    latest_import_status: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class DatabaseUpdateSummary:
    requested_count: int
    updated_count: int
    already_current_count: int
    input_rows: int
    stale_count: int
    cancelled: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TwoStageUpdateSummary:
    deterministic: DatabaseUpdateSummary
    ai: DatabaseUpdateSummary | None = None

    @property
    def cancelled(self):
        return self.deterministic.cancelled or bool(self.ai and self.ai.cancelled)

    def to_dict(self):
        return {
            "deterministic": self.deterministic.to_dict(),
            "ai": self.ai.to_dict() if self.ai else None,
            "cancelled": self.cancelled,
        }


class DesktopService:
    """Coordinates verified local artifacts and the existing analysis pipeline."""

    def __init__(
        self,
        project_root=None,
        *,
        dataset_root=None,
        manifest_path=None,
        mapping_path=None,
        output_root=None,
    ):
        source_root = Path(__file__).resolve().parent.parent
        runtime_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else source_root
        resource_root = Path(getattr(sys, "_MEIPASS", source_root)).resolve()
        self.project_root = Path(project_root or runtime_root).resolve()
        self.dataset_root = Path(
            dataset_root
            or os.getenv("FEEDBACK_HUB_DATA_ROOT", "").strip()
            or self.project_root / "data" / "local" / "tripadvisor-review-rating"
        ).resolve()
        self.manifest_path = Path(
            manifest_path or self.dataset_root / "manifest" / "dataset-manifest.json"
        ).resolve()
        self.mapping_path = Path(
            mapping_path
            or resource_root / "feedback" / "import_mappings" / "tripadvisor_hotel_reviews.json"
        ).resolve()
        self.output_root = Path(output_root or self.dataset_root / "analysis").resolve()
        desktop_data_root = Path(
            os.getenv("LOCALAPPDATA", "").strip() or self.project_root
        ) / "FeedbackInsightHub"
        self.database_output_root = Path(
            os.getenv("FEEDBACK_HUB_ANALYSIS_OUTPUT", "").strip()
            or desktop_data_root / "analysis"
        ).resolve()
        self.worker_id = (
            os.getenv("FEEDBACK_HUB_WORKER_ID", "").strip()
            or f"desktop-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        )[:128]
        self._active_job_id = None
        self._active_job_lock = threading.Lock()

    @staticmethod
    def _checkpoint(cancel_requested, progress, stage, percent):
        if cancel_requested and cancel_requested():
            raise DesktopTaskCancelled("工作已取消")
        if progress:
            progress(stage, percent)

    @staticmethod
    def _read_json(path, label):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DesktopServiceError(f"找不到{label}：{path}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopServiceError(f"無法讀取{label}：{path}") from exc

    @staticmethod
    def _hash_file(path, *, cancel_requested=None, progress=None, start=20, end=92):
        digest = hashlib.sha256()
        size = path.stat().st_size
        consumed = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                if cancel_requested and cancel_requested():
                    raise DesktopTaskCancelled("工作已取消")
                digest.update(chunk)
                consumed += len(chunk)
                if progress and size:
                    percent = start + int((end - start) * consumed / size)
                    progress("驗證本機資料完整性", min(percent, end))
        return digest.hexdigest()

    def inspect_dataset(self, *, cancel_requested=None, progress=None):
        self._checkpoint(cancel_requested, progress, "讀取資料清單", 5)
        manifest = self._read_json(self.manifest_path, "資料清單")
        mapping = self._read_json(self.mapping_path, "欄位設定")
        try:
            source = manifest["source"]
            clean_relative = manifest["clean_path"]
            artifact = next(item for item in manifest["artifacts"] if item["path"] == clean_relative)
            counts = manifest["counts"]
        except (KeyError, StopIteration, TypeError) as exc:
            raise DesktopServiceError("資料清單缺少必要欄位") from exc

        clean_path = (self.dataset_root / clean_relative).resolve()
        if not clean_path.is_relative_to(self.dataset_root):
            raise DesktopServiceError("資料清單的路徑超出允許範圍")
        if not clean_path.is_file():
            raise DesktopServiceError(f"找不到清理後資料：{clean_path}")
        if int(artifact["size"]) != clean_path.stat().st_size:
            raise DesktopServiceError("清理後資料大小與資料清單不符")
        if mapping.get("dataset", {}).get("name") != source.get("dataset"):
            raise DesktopServiceError("欄位設定與資料來源不一致")
        if mapping.get("dataset", {}).get("version") != source.get("source_revision"):
            raise DesktopServiceError("欄位設定與資料版本不一致")

        self._checkpoint(cancel_requested, progress, "驗證欄位與隱私規則", 15)
        question_names = {
            "review_length" if item.get("normalizers") == ["text_length"] else item.get("source_field")
            for item in mapping.get("questions", [])
        }
        if "user_id" in question_names:
            raise DesktopServiceError("正式分析欄位不得包含 user_id")

        actual_hash = self._hash_file(
            clean_path,
            cancel_requested=cancel_requested,
            progress=progress,
        )
        if actual_hash != artifact["sha256"]:
            raise DesktopServiceError("清理後資料雜湊與資料清單不符")
        self._checkpoint(cancel_requested, progress, "檢查 Parquet 欄位", 94)
        try:
            import duckdb

            with duckdb.connect(":memory:") as connection:
                schema = {
                    row[0]
                    for row in connection.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)", [str(clean_path)]
                    ).fetchall()
                }
        except Exception as exc:
            raise DesktopServiceError("清理後 Parquet 無法讀取") from exc
        if "user_id" in schema:
            raise DesktopServiceError("清理後 Parquet 不得包含 user_id")
        missing_fields = question_names - schema
        if missing_fields:
            raise DesktopServiceError("清理後 Parquet 缺少分析欄位：" + "、".join(sorted(missing_fields)))
        self._checkpoint(cancel_requested, progress, "資料驗證完成", 100)
        return DatasetStatus(
            dataset=str(source["dataset"]),
            source_revision=str(source["source_revision"]),
            conversion_revision=str(source.get("viewer_conversion_revision", "")),
            cleaning_version=str(manifest["cleaning_version"]),
            input_rows=int(counts["input_rows"]),
            retained_rows=int(counts["retained_rows"]),
            clean_path=str(clean_path),
            clean_size=int(artifact["size"]),
            clean_sha256=actual_hash,
            verified=True,
        )

    def analyze(self, *, cancel_requested=None, progress=None):
        self._checkpoint(cancel_requested, progress, "建立分析工作", 1)
        try:
            from feedback.analysis_adapters import ParquetInput
            from feedback.background_analysis import LocalAnalysisCancelled, run_once
        except Exception as exc:
            raise DesktopServiceError(f"無法載入本機分析元件：{exc}") from exc
        try:
            adapter = ParquetInput(self.manifest_path, self.mapping_path)
            path, cache_hit = run_once(
                adapter,
                self.output_root,
                cancel_requested=cancel_requested,
                progress=progress,
            )
        except LocalAnalysisCancelled as exc:
            raise DesktopTaskCancelled("工作已取消") from exc
        except DesktopTaskCancelled:
            raise
        except Exception as exc:
            raise DesktopServiceError(f"本機分析失敗：{exc}") from exc

        envelope = self._read_json(path, "分析結果")
        result = envelope.get("result", {})
        stats = result.get("statistics", {}).get("payload", {})
        text = result.get("text", {}).get("payload", {})
        return AnalysisSummary(
            result_path=str(path),
            cache_hit=bool(cache_hit),
            input_rows=int(result.get("input_rows", 0)),
            chart_count=len(stats.get("charts", [])),
            available_test_count=int(stats.get("available_tests_count", 0)),
            keyword_count=len(text.get("keywords", [])),
            text_coverage=float(text.get("summary", {}).get("analysis_coverage", 0)),
            ai_mode=str(result.get("ai_mode", "unknown")),
            elapsed_seconds=float(result.get("timings_seconds", {}).get("total", 0)),
        )

    @staticmethod
    def _manifest_stage_current(state, stage):
        item = (state.publication_manifest or {}).get(stage) or {}
        return bool(item) and (
            item.get("input_version") == state.input_version
            and item.get("config_version") == state.config_version
            and item.get("pipeline_version") == state.pipeline_version
        )

    @staticmethod
    def _manifest_datetime(state, stage):
        from django.utils.dateparse import parse_datetime

        value = ((state.publication_manifest or {}).get(stage) or {}).get("published_at")
        if not value:
            return None
        return parse_datetime(value) if isinstance(value, str) else value

    def list_surveys(self, *, cancel_requested=None, progress=None):
        """Read compact survey freshness data without fetching Answer values."""

        from django.db import connection
        from django.db.models import Count, Exists, Max, OuterRef, Q, Subquery
        from django.db.models.functions import Coalesce
        from django.db.models.fields import IntegerField

        from feedback.models import AnalysisJob, DatasetImportBatch, FeedbackSubmission, Survey

        self._checkpoint(cancel_requested, progress, "連線資料庫", 5)
        try:
            connection.ensure_connection()
            if getattr(sys, "frozen", False) and connection.vendor != "postgresql":
                raise DesktopServiceError(
                    "尚未設定 Supabase PostgreSQL 連線；請使用 EXE 外部環境設定，憑證不會打包進程式。"
                )
            submission_summary = (
                FeedbackSubmission.objects.filter(
                    survey_id=OuterRef("pk"),
                    is_complete=True,
                    voided_at__isnull=True,
                )
                .values("survey_id")
                .annotate(total=Count("pk"), latest=Max("ingested_at"))
            )
            latest_job = AnalysisJob.objects.filter(
                survey_id=OuterRef("pk"),
                executor=AnalysisJob.Executor.DETERMINISTIC,
            ).order_by("-created_at", "-pk")
            latest_ai_job = AnalysisJob.objects.filter(
                survey_id=OuterRef("pk"),
                executor=AnalysisJob.Executor.AI,
            ).order_by("-created_at", "-pk")
            latest_import = DatasetImportBatch.objects.filter(survey_id=OuterRef("pk")).order_by(
                "-created_at", "-pk"
            )
            imported_submission = FeedbackSubmission.objects.filter(
                survey_id=OuterRef("pk"),
                imported_source__isnull=False,
                is_complete=True,
                voided_at__isnull=True,
            )
            native_submission = FeedbackSubmission.objects.filter(
                survey_id=OuterRef("pk"),
                imported_source__isnull=True,
                is_complete=True,
                voided_at__isnull=True,
            )
            surveys = list(
                Survey.objects.filter(archived_at__isnull=True).select_related("analysis_state")
                .annotate(
                    question_total=Count(
                        "questions",
                        filter=Q(questions__is_active=True),
                        distinct=True,
                    ),
                    response_total=Coalesce(
                        Subquery(submission_summary.values("total")[:1]),
                        0,
                        output_field=IntegerField(),
                    ),
                    latest_response_at=Subquery(submission_summary.values("latest")[:1]),
                    latest_job_status_value=Subquery(latest_job.values("status")[:1]),
                    latest_ai_job_status_value=Subquery(latest_ai_job.values("status")[:1]),
                    latest_import_status_value=Subquery(latest_import.values("status")[:1]),
                    has_imported=Exists(imported_submission),
                    has_native=Exists(native_submission),
                )
                .order_by("title", "pk")
            )
        except DesktopServiceError:
            raise
        except Exception as exc:
            raise DesktopServiceError(f"無法讀取問卷資料庫（{type(exc).__name__}）") from exc

        self._checkpoint(cancel_requested, progress, "整理問卷狀態", 80)
        result = []
        for survey in surveys:
            try:
                state = survey.analysis_state
            except Survey.analysis_state.RelatedObjectDoesNotExist:
                state = None
            statistics_current = bool(state and self._manifest_stage_current(state, "statistics"))
            text_current = bool(state and self._manifest_stage_current(state, "text"))
            ai_current = bool(state and self._manifest_stage_current(state, "ai"))
            generated_values = [
                self._manifest_datetime(state, stage)
                for stage in ("statistics", "text")
                if state
            ]
            generated_values = [value for value in generated_values if value]
            latest_ai_at = self._manifest_datetime(state, "ai") if state else None
            if survey.has_imported and survey.has_native:
                source = "線上問卷＋匯入資料"
            elif survey.has_imported:
                source = "匯入資料"
            else:
                source = "線上問卷"
            response_count = int(survey.response_total or 0)
            result.append(
                SurveyStatus(
                    survey_id=survey.pk,
                    title=survey.title,
                    slug=survey.slug,
                    source=source,
                    is_active=survey.is_active,
                    question_count=int(survey.question_total or 0),
                    response_count=response_count,
                    latest_data_at=survey.latest_response_at,
                    latest_analysis_at=max(generated_values) if generated_values else None,
                    latest_ai_at=latest_ai_at,
                    statistics_current=statistics_current,
                    text_current=text_current,
                    ai_current=ai_current,
                    needs_update=response_count > 0 and not (statistics_current and text_current),
                    needs_ai=response_count > 0 and statistics_current and text_current and not ai_current,
                    latest_job_status=survey.latest_job_status_value or "",
                    latest_ai_job_status=survey.latest_ai_job_status_value or "",
                    latest_import_status=survey.latest_import_status_value or "",
                )
            )
        self._checkpoint(cancel_requested, progress, "狀態已更新", 100)
        return result

    def cancel_active_update(self):
        from feedback.analysis_jobs import request_job_cancel

        with self._active_job_lock:
            job_id = self._active_job_id
        if job_id:
            request_job_cancel(job_id)
            return True
        return False

    def update_surveys(
        self,
        survey_ids=None,
        *,
        cancel_requested=None,
        progress=None,
        lease_seconds=600,
    ):
        """Run stale deterministic survey jobs locally and publish bounded results."""

        from feedback.analysis_jobs import (
            claim_next_job,
            fail_job,
            finish_cancelled_job,
            schedule_survey_analysis,
        )
        from feedback.analysis_worker import (
            WorkerCancelled,
            WorkerExecutionError,
            execute_deterministic_job,
        )
        from feedback.models import AnalysisJob, Survey

        selected = set(int(value) for value in survey_ids) if survey_ids is not None else None
        statuses = self.list_surveys(cancel_requested=cancel_requested)
        targets = [
            status
            for status in statuses
            if status.needs_update and (selected is None or status.survey_id in selected)
        ]
        already_current_count = 0
        if selected is not None:
            known = {status.survey_id for status in statuses if status.survey_id in selected}
            missing = selected - known
            if missing:
                raise DesktopServiceError("選取的問卷已不存在")
            already_current_count = len(selected) - len(targets)
        if not targets:
            self._checkpoint(cancel_requested, progress, "所有問卷均為最新", 100)
            return DatabaseUpdateSummary(0, 0, already_current_count, 0, 0)

        updated = input_rows = stale = 0
        cancelled = False
        for index, status in enumerate(targets, start=1):
            if cancel_requested and cancel_requested():
                cancelled = True
                break
            percent = int((index - 1) * 100 / len(targets))
            if progress:
                progress(f"準備分析：{status.title}", percent)
            survey = Survey.objects.filter(pk=status.survey_id).first()
            if survey is None:
                continue
            schedule_survey_analysis(survey.pk, change="none")
            job = claim_next_job(
                self.worker_id,
                lease_seconds=lease_seconds,
                executor=AnalysisJob.Executor.DETERMINISTIC,
                external_source_refs=(),
                survey_ids=(survey.pk,),
            )
            if job is None:
                continue
            with self._active_job_lock:
                self._active_job_id = job.pk
            try:
                result = execute_deterministic_job(
                    job,
                    output_root=self.database_output_root,
                    lease_seconds=lease_seconds,
                )
            except WorkerCancelled:
                finish_cancelled_job(job.pk, job.lease_token)
                cancelled = True
                break
            except WorkerExecutionError as exc:
                fail_job(
                    job.pk,
                    job.lease_token,
                    error_code=exc.code,
                    retryable=exc.retryable,
                    retry_delay_seconds=30 if exc.retryable else 0,
                )
                raise DesktopServiceError(f"問卷分析失敗（{exc.code}）") from exc
            except Exception as exc:
                code = f"desktop_worker_{type(exc).__name__.lower()}"[:64]
                fail_job(job.pk, job.lease_token, error_code=code, retryable=False)
                raise DesktopServiceError(f"問卷分析失敗（{code}）") from exc
            finally:
                with self._active_job_lock:
                    self._active_job_id = None
            input_rows += result.input_rows
            if result.published:
                updated += 1
            elif result.stale:
                stale += 1
        if progress:
            progress("更新完成" if not cancelled else "更新已取消", 100 if not cancelled else 0)
        return DatabaseUpdateSummary(
            requested_count=len(targets),
            updated_count=updated,
            already_current_count=already_current_count,
            input_rows=input_rows,
            stale_count=stale,
            cancelled=cancelled,
        )

    def update_ai_surveys(
        self,
        survey_ids=None,
        *,
        allow_paid_ai=False,
        cancel_requested=None,
        progress=None,
        lease_seconds=600,
    ):
        """Run the second analysis stage for surveys with current base results."""

        if not allow_paid_ai:
            raise DesktopServiceError("Gemini 尚未啟用；請先確認會使用 API 額度。")

        from feedback.ai_worker import (
            AIWorkerCancelled,
            AIWorkerExecutionError,
            AIWorkerSuperseded,
            execute_ai_job,
        )
        from feedback.analysis_jobs import (
            claim_next_job,
            fail_job,
            finish_cancelled_job,
            schedule_survey_analysis,
        )
        from feedback.models import AnalysisJob, Survey, SurveyAIAnalysisStage

        selected = set(int(value) for value in survey_ids) if survey_ids is not None else None
        statuses = self.list_surveys(cancel_requested=cancel_requested)
        if selected is not None:
            known = {status.survey_id for status in statuses if status.survey_id in selected}
            if selected - known:
                raise DesktopServiceError("選取的問卷已不存在")
            blocked = [
                status.title
                for status in statuses
                if status.survey_id in selected and status.response_count and status.needs_update
            ]
            if blocked:
                raise DesktopServiceError("請先完成第一段統計／文字分析：" + "、".join(blocked[:3]))

        targets = [
            status
            for status in statuses
            if status.needs_ai and (selected is None or status.survey_id in selected)
        ]
        already_current_count = 0
        if selected is not None:
            already_current_count = sum(
                status.survey_id in selected and status.ai_current for status in statuses
            )
        if not targets:
            self._checkpoint(cancel_requested, progress, "Gemini 分析均為最新", 100)
            return DatabaseUpdateSummary(0, 0, already_current_count, 0, 0)

        updated = stale = 0
        cancelled = False
        for index, status in enumerate(targets, start=1):
            if cancel_requested and cancel_requested():
                cancelled = True
                break
            if progress:
                progress(f"準備 Gemini 分析：{status.title}", int((index - 1) * 100 / len(targets)))
            survey = Survey.objects.filter(pk=status.survey_id).first()
            if survey is None:
                continue
            schedule_survey_analysis(
                survey.pk,
                change="none",
                requested_stages=(SurveyAIAnalysisStage.StageType.SYNTHESIS,),
            )
            job = claim_next_job(
                self.worker_id,
                lease_seconds=lease_seconds,
                executor=AnalysisJob.Executor.AI,
                survey_ids=(survey.pk,),
            )
            if job is None:
                continue
            with self._active_job_lock:
                self._active_job_id = job.pk
            try:
                execute_ai_job(job, allow_paid_ai=True, lease_seconds=lease_seconds)
            except AIWorkerCancelled:
                finish_cancelled_job(job.pk, job.lease_token)
                cancelled = True
                break
            except AIWorkerSuperseded:
                stale += 1
            except AIWorkerExecutionError as exc:
                if not exc.already_finalized:
                    fail_job(
                        job.pk,
                        job.lease_token,
                        error_code=exc.code,
                        retryable=exc.retryable,
                        retry_delay_seconds=30 if exc.retryable else 0,
                    )
                raise DesktopServiceError(f"Gemini 分析失敗（{exc.code}）") from exc
            except Exception as exc:
                code = f"desktop_ai_{type(exc).__name__.lower()}"[:64]
                fail_job(job.pk, job.lease_token, error_code=code, retryable=False)
                raise DesktopServiceError(f"Gemini 分析失敗（{code}）") from exc
            else:
                updated += 1
            finally:
                with self._active_job_lock:
                    self._active_job_id = None

        if progress:
            progress("Gemini 分析完成" if not cancelled else "Gemini 分析已取消", 100 if not cancelled else 0)
        return DatabaseUpdateSummary(
            requested_count=len(targets),
            updated_count=updated,
            already_current_count=already_current_count,
            input_rows=0,
            stale_count=stale,
            cancelled=cancelled,
        )

    def update_two_stage_analysis(
        self,
        survey_ids=None,
        *,
        include_ai=False,
        cancel_requested=None,
        progress=None,
        lease_seconds=600,
    ):
        """Publish deterministic results, then optionally publish Gemini analysis."""

        def phase_progress(prefix, start, width):
            if progress is None:
                return None
            return lambda stage, percent: progress(
                f"{prefix}｜{stage}",
                min(100, start + int(width * percent / 100)),
            )

        deterministic = self.update_surveys(
            survey_ids,
            cancel_requested=cancel_requested,
            progress=phase_progress("第一段", 0, 65 if include_ai else 100),
            lease_seconds=lease_seconds,
        )
        if deterministic.cancelled or not include_ai:
            return TwoStageUpdateSummary(deterministic=deterministic)
        ai = self.update_ai_surveys(
            survey_ids,
            allow_paid_ai=True,
            cancel_requested=cancel_requested,
            progress=phase_progress("第二段", 65, 35),
            lease_seconds=lease_seconds,
        )
        return TwoStageUpdateSummary(deterministic=deterministic, ai=ai)
