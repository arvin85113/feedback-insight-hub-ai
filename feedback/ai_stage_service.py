import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping

from django.conf import settings
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone
from google.genai import types

from . import ai_statistics_service, ai_synthesis_service, ai_text_service
from .ai_report_service import AIReportError, _provider_error, _wait_for_request_slot, create_gemini_client
from .ai_snapshot_service import (
    SNAPSHOT_SCHEMA_VERSION,
    build_evidence_coverage,
    calculate_data_fingerprint,
    get_report_status,
    serialize_ai_report_content,
)
from .evidence_projection import COMPACT_PROFILE, STANDARD_PROFILE, estimate_input_tokens
from .models import ImprovementUpdate, SurveyAIAnalysisStage


logger = logging.getLogger(__name__)
_UNTRUSTED_NUMBER_RE = re.compile(r"\d|百分之[零〇一二兩三四五六七八九十百千萬億]+")
STAGE_MODULES = {
    SurveyAIAnalysisStage.StageType.STATISTICS: ai_statistics_service,
    SurveyAIAnalysisStage.StageType.TEXT: ai_text_service,
    SurveyAIAnalysisStage.StageType.SYNTHESIS: ai_synthesis_service,
}
STAGE_EVIDENCE_ENUM_LIMIT = 20
UPSTREAM_TYPES = (
    SurveyAIAnalysisStage.StageType.STATISTICS,
    SurveyAIAnalysisStage.StageType.TEXT,
)


class StageError(Exception):
    def __init__(
        self,
        error_code,
        user_message,
        *,
        status_code=400,
        retryable=False,
        reason=None,
        http_status=None,
    ):
        super().__init__(error_code)
        self.error_code = error_code
        self.user_message = user_message
        self.status_code = status_code
        self.retryable = retryable
        self.reason = reason
        self.http_status = http_status


class StageAttemptError(Exception):
    def __init__(self, stage_error, metrics):
        super().__init__(stage_error.error_code)
        self.stage_error = stage_error
        self.metrics = metrics


def _canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _improvement_summary(snapshot, *, exclude_stage=None):
    queryset = ImprovementUpdate.objects.filter(survey=snapshot.survey).order_by("created_at", "id")
    if exclude_stage is not None:
        queryset = queryset.exclude(source_ai_analysis_stage=exclude_stage)
    return [
        {
            "title": item.title[:255],
            "summary": item.summary[:1000],
            "related_category": item.related_category[:100],
            "notification_status": "notified" if item.emailed_at else "not_notified",
        }
        for item in queryset.iterator(chunk_size=settings.AI_REPORT_FINGERPRINT_CHUNK_SIZE)
    ]


def _latest_matching_stage(snapshot, stage_type):
    module = STAGE_MODULES[stage_type]
    stage_input, _ = build_stage_input(snapshot, stage_type)
    input_hash = _canonical_hash(stage_input)
    return (
        snapshot.analysis_stages.filter(
            stage_type=stage_type,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
            input_hash=input_hash,
            schema_version=module.SCHEMA_VERSION,
            prompt_version=module.PROMPT_VERSION,
            model_name=settings.GEMINI_MODEL,
        )
        .order_by("-revision", "-id")
        .first()
    )


def build_stage_input(snapshot, stage_type, *, exclude_stage=None):
    if stage_type == SurveyAIAnalysisStage.StageType.STATISTICS:
        stage_input = ai_statistics_service.build_input(snapshot.source_snapshot)
        registry = stage_input["evidence_catalog"]
        return stage_input, {row["id"]: row for row in registry}
    if stage_type == SurveyAIAnalysisStage.StageType.TEXT:
        stage_input = ai_text_service.build_input(snapshot.source_snapshot)
        registry = stage_input["evidence_catalog"]
        return stage_input, {row["id"]: row for row in registry}
    if stage_type != SurveyAIAnalysisStage.StageType.SYNTHESIS:
        raise StageError("invalid_stage", "不支援的 AI 分析階段。")

    upstream = {stage_type: _latest_matching_stage(snapshot, stage_type) for stage_type in UPSTREAM_TYPES}
    if any(stage is None for stage in upstream.values()):
        raise StageError("upstream_incomplete", "請先完成統計分析與文字洞察階段。", status_code=409)
    evidence_by_id = {}
    for stage in upstream.values():
        evidence_by_id.update((stage.output_json or {}).get("_evidence_registry", {}))
    data_scope = {
        key: snapshot.source_snapshot.get("data_scope", {}).get(key)
        for key in (
            "survey_title",
            "valid_response_count",
            "source_latest_date",
            "analysis_coverage",
        )
    }
    stage_input = ai_synthesis_service.build_input(
        upstream[SurveyAIAnalysisStage.StageType.STATISTICS],
        upstream[SurveyAIAnalysisStage.StageType.TEXT],
        _improvement_summary(snapshot, exclude_stage=exclude_stage),
        data_scope,
    )
    return stage_input, evidence_by_id


def stage_input_hash(snapshot, stage_type, *, exclude_stage=None):
    stage_input, _ = build_stage_input(snapshot, stage_type, exclude_stage=exclude_stage)
    return _canonical_hash(stage_input)


def _next_revision(snapshot, stage_type):
    highest = snapshot.analysis_stages.filter(stage_type=stage_type).aggregate(value=Max("revision"))["value"]
    return (highest or 0) + 1


def prepare_stage(snapshot, stage_type, *, force=False):
    module = STAGE_MODULES.get(stage_type)
    if module is None:
        raise StageError("invalid_stage", "不支援的 AI 分析階段。")
    stage_input, evidence_by_id = build_stage_input(snapshot, stage_type)
    input_hash = _canonical_hash(stage_input)
    analysis_source_hash = calculate_data_fingerprint(
        snapshot.survey,
        include_improvements=False,
    ).value
    identity = {
        "stage_type": stage_type,
        "input_hash": input_hash,
        "schema_version": module.SCHEMA_VERSION,
        "prompt_version": module.PROMPT_VERSION,
        "model_name": settings.GEMINI_MODEL,
    }
    existing = (
        snapshot.analysis_stages.filter(status=SurveyAIAnalysisStage.Status.SUCCEEDED, **identity)
        .order_by("-revision", "-id")
        .first()
    )
    if existing and not force:
        return existing, stage_input, evidence_by_id, True

    if not force:
        reusable = (
            SurveyAIAnalysisStage.objects.filter(
                snapshot__survey=snapshot.survey,
                status=SurveyAIAnalysisStage.Status.SUCCEEDED,
                **identity,
            )
            .exclude(snapshot=snapshot)
            .order_by("-generated_at", "-id")
            .first()
        )
        if reusable:
            reused = SurveyAIAnalysisStage.objects.create(
                snapshot=snapshot,
                status=SurveyAIAnalysisStage.Status.SUCCEEDED,
                revision=_next_revision(snapshot, stage_type),
                input_manifest={
                    "input_hash": input_hash,
                    "analysis_source_hash": analysis_source_hash,
                    "upstream_stage_ids": stage_input.get("upstream_stage_ids", {}),
                    "reused": True,
                },
                output_json=copy.deepcopy(reusable.output_json),
                error_code="",
                generation_ms=0,
                token_metrics={},
                reused_from=reusable,
                generated_at=timezone.now(),
                **identity,
            )
            return reused, stage_input, evidence_by_id, True

    stage = SurveyAIAnalysisStage.objects.create(
        snapshot=snapshot,
        status=SurveyAIAnalysisStage.Status.GENERATING,
        revision=_next_revision(snapshot, stage_type),
        input_manifest={
            "input_hash": input_hash,
            "analysis_source_hash": analysis_source_hash,
            "upstream_stage_ids": stage_input.get("upstream_stage_ids", {}),
            "estimated_input_tokens": estimate_input_tokens(stage_input),
            "reused": False,
        },
        **identity,
    )
    return stage, stage_input, evidence_by_id, False


def _validate_text(value, max_length=800):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _UNTRUSTED_NUMBER_RE.search(value)
    ):
        raise ValueError("invalid_text")
    return value.strip()


def _validate_refs(value, evidence_by_id, *, max_items=3):
    unique_refs = list(dict.fromkeys(value)) if isinstance(value, list) else []
    if (
        not isinstance(value, list)
        or not value
        or len(value) > max_items
        or len(unique_refs) > max_items
        or any(ref not in evidence_by_id for ref in unique_refs)
    ):
        raise ValueError("invalid_evidence_refs")
    return unique_refs


def _validate_stage_finding(row, evidence_by_id, *, max_refs=3, max_limitations=3):
    expected = {"title", "rationale", "evidence_refs", "data_limitations"}
    if not isinstance(row, Mapping) or set(row) != expected:
        raise ValueError("invalid_finding")
    refs = _validate_refs(row.get("evidence_refs"), evidence_by_id, max_items=max_refs)
    limitations = row.get("data_limitations")
    if not isinstance(limitations, list) or len(limitations) > max_limitations:
        raise ValueError("invalid_limitations")
    return {
        "title": _validate_text(row.get("title"), 180),
        "rationale": _validate_text(row.get("rationale")),
        "evidence_refs": refs,
        "evidence": [evidence_by_id[ref] for ref in refs],
        "data_limitations": [_validate_text(item, 400) for item in limitations],
    }


def _response_payload(response):
    text = getattr(response, "text", "")
    if not text or not text.strip():
        raise StageError(
            "empty_response",
            "AI 服務沒有回傳內容。",
            status_code=502,
            reason="empty_response_text",
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise StageError(
            "schema_invalid",
            "AI 階段輸出格式無法驗證。",
            status_code=502,
            retryable=True,
            reason="invalid_json",
        ) from exc


def _usage_metrics(response):
    usage = getattr(response, "usage_metadata", None)
    return {
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        "thinking_token_count": getattr(usage, "thoughts_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    }


def _finish_reason(response):
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    value = getattr(candidates[0], "finish_reason", None)
    if value is None:
        return None
    value = getattr(value, "value", value)
    return str(value).split(".")[-1].upper()


def _http_status(response):
    sdk_response = getattr(response, "sdk_http_response", None)
    return getattr(sdk_response, "status_code", None) or 200


def _safe_validation_reason(exc):
    reason = str(exc)
    return reason if re.fullmatch(r"[a-z0-9_]{1,80}", reason) else "validation_error"


def _profile_instruction(module, profile):
    limits = module.PROFILE_LIMITS[profile]
    label = "標準" if profile == STANDARD_PROFILE else "精簡"
    instruction = (
        f"使用{label}分析模式。每個分析區塊最多{limits['findings']}項；"
        f"每項最多引用{limits['evidence_refs']}筆 evidence；"
        f"每項最多{limits['limitations']}筆資料限制。"
    )
    if "drafts" in limits:
        instruction += f"改善草稿最多{limits['drafts']}項。"
    return instruction


def _bind_evidence_enum(schema, evidence_by_id):
    allowed_ids = sorted(evidence_by_id)
    if not allowed_ids:
        return schema

    def visit(node):
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            evidence_refs = properties.get("evidence_refs")
            if isinstance(evidence_refs, dict) and isinstance(evidence_refs.get("items"), dict):
                evidence_refs["items"]["enum"] = allowed_ids
        for value in node.values():
            if isinstance(value, dict):
                visit(value)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

    visit(schema)
    return schema


def _attempt_config(module, profile, evidence_by_id, *, bind_evidence_enum):
    if profile == COMPACT_PROFILE:
        thinking_budget = settings.GEMINI_COMPACT_THINKING_BUDGET
        output_tokens = settings.GEMINI_COMPACT_MAX_OUTPUT_TOKENS
    else:
        thinking_budget = settings.GEMINI_THINKING_BUDGET
        output_tokens = settings.GEMINI_MAX_OUTPUT_TOKENS
    response_schema = module.response_schema_for_profile(profile)
    if bind_evidence_enum:
        response_schema = _bind_evidence_enum(response_schema, evidence_by_id)
    return types.GenerateContentConfig(
        system_instruction=f"{module.SYSTEM_INSTRUCTION}\n{_profile_instruction(module, profile)}",
        temperature=0.2,
        max_output_tokens=output_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
        response_mime_type="application/json",
        response_schema=response_schema,
        http_options=types.HttpOptions(
            timeout=settings.GEMINI_TIMEOUT_SECONDS * 1000,
            retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[429]),
        ),
    )


def _run_stage_attempt(client, stage, stage_input, evidence_by_id, module, *, profile, retry_count):
    contents = json.dumps(stage_input, ensure_ascii=False, separators=(",", ":"))
    system_instruction = f"{module.SYSTEM_INSTRUCTION}\n{_profile_instruction(module, profile)}"
    bind_evidence_enum = (
        stage.stage_type == SurveyAIAnalysisStage.StageType.SYNTHESIS
        and 0 < len(evidence_by_id) <= STAGE_EVIDENCE_ENUM_LIMIT
    )
    metrics = {
        "total_evidence_count": len(evidence_by_id),
        "selected_evidence_count": len(evidence_by_id),
        "excluded_evidence_count": 0,
        "prompt_character_count": len(contents) + len(system_instruction),
        "estimated_input_tokens": estimate_input_tokens(f"{system_instruction}\n{contents}"),
        "prompt_token_count": None,
        "candidates_token_count": None,
        "thinking_token_count": None,
        "total_token_count": None,
        "generation_ms": None,
        "http_status": None,
        "exception_class": None,
        "finish_reason": None,
        "generation_profile": profile,
        "retry_count": retry_count,
        "evidence_enum_bound": bind_evidence_enum,
    }
    started = time.perf_counter()
    try:
        _wait_for_request_slot()
        response = client.models.generate_content(
            model=stage.model_name,
            contents=contents,
            config=_attempt_config(
                module,
                profile,
                evidence_by_id,
                bind_evidence_enum=bind_evidence_enum,
            ),
        )
        metrics["generation_ms"] = round((time.perf_counter() - started) * 1000)
        metrics["http_status"] = _http_status(response)
        metrics["finish_reason"] = _finish_reason(response)
        metrics.update(_usage_metrics(response))
        if metrics["finish_reason"] in {"MAX_TOKENS", "LENGTH"}:
            raise StageError(
                "output_truncated",
                "AI 階段輸出未完成，已保留既有報告。",
                status_code=502,
                retryable=True,
                reason="finish_reason_max_tokens",
                http_status=metrics["http_status"],
            )
        payload = _response_payload(response)
        if stage.stage_type == SurveyAIAnalysisStage.StageType.SYNTHESIS:
            validated = module.validate_output(
                payload,
                evidence_by_id,
                stage.input_hash,
                profile=profile,
            )
        else:
            validated = module.validate_output(
                payload,
                evidence_by_id,
                _validate_stage_finding,
                profile=profile,
            )
        validated["_evidence_registry"] = evidence_by_id
        return validated, metrics
    except ValueError as exc:
        safe_error = StageError(
            "schema_invalid",
            "AI 階段輸出格式無法驗證。",
            status_code=502,
            retryable=True,
            reason=_safe_validation_reason(exc),
            http_status=metrics["http_status"],
        )
        metrics["exception_class"] = type(exc).__name__
    except StageError as exc:
        safe_error = exc
        metrics["exception_class"] = type(exc).__name__
    except AIReportError as exc:
        safe_error = StageError(
            exc.error_code,
            exc.user_message,
            status_code=exc.status_code,
            retryable=exc.retryable,
            reason=exc.reason,
            http_status=exc.http_status,
        )
        metrics["exception_class"] = type(exc).__name__
    except Exception as exc:
        provider_error = _provider_error(exc)
        safe_error = StageError(
            provider_error.error_code,
            provider_error.user_message,
            status_code=provider_error.status_code,
            retryable=provider_error.retryable,
            reason=provider_error.reason,
            http_status=provider_error.http_status,
        )
        metrics["exception_class"] = type(exc).__name__

    metrics["generation_ms"] = metrics["generation_ms"] or round((time.perf_counter() - started) * 1000)
    metrics["http_status"] = metrics["http_status"] or safe_error.http_status
    metrics["validation_reason"] = safe_error.reason
    logger.warning(
        "ai_stage_attempt stage_id=%s stage_type=%s metrics=%s error_code=%s",
        stage.pk,
        stage.stage_type,
        json.dumps(metrics, sort_keys=True),
        safe_error.error_code,
    )
    raise StageAttemptError(safe_error, metrics)


def _stage_token_metrics(attempts, *, profile=None, retry_count=0):
    final = attempts[-1] if attempts else {}
    return {
        "generation_profile": profile,
        "retry_count": retry_count,
        "prompt_token_count": final.get("prompt_token_count"),
        "candidates_token_count": final.get("candidates_token_count"),
        "thinking_token_count": final.get("thinking_token_count"),
        "total_token_count": final.get("total_token_count"),
        "attempts": attempts,
    }


def generate_stage(snapshot, stage_type, *, force=False):
    stage, stage_input, evidence_by_id, cache_hit = prepare_stage(snapshot, stage_type, force=force)
    if cache_hit:
        return stage
    module = STAGE_MODULES[stage_type]
    client = None
    started = time.perf_counter()
    attempts = []
    last_error = None
    try:
        client = create_gemini_client()
        for retry_count, profile in enumerate((STANDARD_PROFILE, COMPACT_PROFILE)):
            try:
                validated, attempt_metrics = _run_stage_attempt(
                    client,
                    stage,
                    stage_input,
                    evidence_by_id,
                    module,
                    profile=profile,
                    retry_count=retry_count,
                )
                attempts.append(attempt_metrics)
                generation_ms = round((time.perf_counter() - started) * 1000)
                updated = SurveyAIAnalysisStage.objects.filter(
                    pk=stage.pk,
                    status=SurveyAIAnalysisStage.Status.GENERATING,
                ).update(
                    status=SurveyAIAnalysisStage.Status.SUCCEEDED,
                    output_json=validated,
                    error_code="",
                    generation_ms=generation_ms,
                    token_metrics=_stage_token_metrics(
                        attempts,
                        profile=profile,
                        retry_count=retry_count,
                    ),
                    generated_at=timezone.now(),
                    updated_at=timezone.now(),
                )
                if not updated:
                    raise StageError("terminal_stage", "AI 階段已完成，不能覆寫。", status_code=409)
                stage.refresh_from_db()
                return stage
            except StageAttemptError as exc:
                attempts.append(exc.metrics)
                last_error = exc.stage_error
                if profile == COMPACT_PROFILE or not last_error.retryable:
                    break
                if last_error.error_code == "rate_limited":
                    time.sleep(settings.AI_REPORT_RATE_LIMIT_BACKOFF_SECONDS * (2**retry_count))
    except AIReportError as exc:
        last_error = StageError(
            exc.error_code,
            exc.user_message,
            status_code=exc.status_code,
            retryable=exc.retryable,
            reason=exc.reason,
            http_status=exc.http_status,
        )
    except StageError:
        raise
    except Exception as exc:
        provider_error = _provider_error(exc)
        last_error = StageError(
            provider_error.error_code,
            provider_error.user_message,
            status_code=provider_error.status_code,
            reason=provider_error.reason,
            http_status=provider_error.http_status,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    if last_error is None:
        last_error = StageError("provider_error", "AI 服務暫時無法使用。", status_code=503)
    SurveyAIAnalysisStage.objects.filter(
        pk=stage.pk,
        status=SurveyAIAnalysisStage.Status.GENERATING,
    ).update(
        status=SurveyAIAnalysisStage.Status.FAILED,
        error_code=last_error.error_code,
        generation_ms=round((time.perf_counter() - started) * 1000),
        token_metrics=_stage_token_metrics(
            attempts,
            profile=attempts[-1]["generation_profile"] if attempts else None,
            retry_count=max(0, len(attempts) - 1),
        ),
        updated_at=timezone.now(),
    )
    raise last_error


def is_stage_current(stage):
    if stage.status != SurveyAIAnalysisStage.Status.SUCCEEDED:
        return False
    module = STAGE_MODULES.get(stage.stage_type)
    if not module or (
        stage.schema_version != module.SCHEMA_VERSION
        or stage.prompt_version != module.PROMPT_VERSION
        or stage.model_name != settings.GEMINI_MODEL
        or stage.snapshot.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION
    ):
        return False
    analysis_source_hash = calculate_data_fingerprint(
        stage.snapshot.survey,
        include_improvements=False,
    ).value
    if analysis_source_hash != stage.input_manifest.get("analysis_source_hash"):
        return False
    try:
        expected = stage_input_hash(
            stage.snapshot,
            stage.stage_type,
            exclude_stage=stage if stage.stage_type == SurveyAIAnalysisStage.StageType.SYNTHESIS else None,
        )
    except StageError:
        return False
    return expected == stage.input_hash


def latest_stage_status(snapshot):
    result = {}
    for stage_type in STAGE_MODULES:
        latest = snapshot.analysis_stages.filter(stage_type=stage_type).order_by("-revision", "-id").first()
        result[stage_type] = {
            "status": latest.status if latest else "not_started",
            "stage_id": latest.pk if latest else None,
            "revision": latest.revision if latest else None,
            "cache_hit": bool(latest and latest.reused_from_id),
            "is_current": bool(latest and is_stage_current(latest)),
            "error_code": latest.error_code if latest else "",
        }
    return result


def serialize_synthesis_stage(stage, *, is_current):
    output = stage.output_json or {}
    evidence_total = len(output.get("_evidence_registry", {}))
    imported = {
        item.source_ai_draft_id: item
        for item in ImprovementUpdate.objects.filter(source_ai_analysis_stage=stage)
    }
    draft_states = {}
    for draft in output.get("improvement_drafts", []):
        draft_id = draft.get("draft_id")
        if not draft_id:
            continue
        improvement = imported.get(draft_id)
        if improvement:
            draft_states[draft_id] = {
                "imported": True,
                "url": f"{reverse('feedback:improvement-list')}?survey={stage.snapshot.survey.slug}#improvement-{improvement.pk}",
            }
        elif is_current:
            draft_states[draft_id] = {
                "imported": False,
                "url": reverse(
                    "feedback:ai-stage-improvement-draft",
                    args=[stage.snapshot.survey.slug, stage.pk, draft_id],
                ),
            }
        else:
            draft_states[draft_id] = {"imported": False, "url": None}
    return {
        "report_source": "staged",
        "stage_id": stage.pk,
        "snapshot_id": stage.snapshot_id,
        "survey_slug": stage.snapshot.survey.slug,
        "survey_title": stage.snapshot.survey.title,
        "status": stage.status,
        "model": stage.model_name,
        "generated_at": stage.generated_at.isoformat() if stage.generated_at else None,
        "source_latest_at": (
            stage.snapshot.source_latest_at.isoformat() if stage.snapshot.source_latest_at else None
        ),
        "response_count": stage.snapshot.response_count,
        "analysis_coverage": float(stage.snapshot.analysis_coverage),
        "is_current": is_current,
        "cache_hit": bool(stage.reused_from_id),
        "generation_ms": stage.generation_ms,
        "generation_profile": (stage.token_metrics or {}).get("generation_profile") or STANDARD_PROFILE,
        "evidence_coverage": build_evidence_coverage(evidence_total, evidence_total, 0),
        "content": serialize_ai_report_content(
            {
                key: output.get(key)
                for key in ("executive_summary", "combined_findings", "improvement_drafts", "data_caveats")
            },
            stage.snapshot.source_snapshot,
        ),
        "draft_states": draft_states,
    }


def _stage_failure_message(stage):
    if stage is None:
        return ""
    attempts = (stage.token_metrics or {}).get("attempts") or []
    reason = attempts[-1].get("validation_reason") if attempts else None
    if stage.error_code == "schema_invalid" and reason == "provider_schema_definition":
        return "AI 服務拒絕目前階段的輸出結構，請更新後重試。"
    if stage.error_code == "schema_invalid" and reason == "invalid_evidence_refs":
        return "AI 階段引用的分析證據無法驗證，請重新產生。"
    messages = {
        "authentication_error": "AI API 金鑰驗證失敗，請檢查伺服器設定。",
        "forbidden": "AI API 金鑰沒有執行此模型的權限。",
        "model_not_found": "找不到設定的 Gemini 模型。",
        "rate_limited": "AI 服務請求過多或額度不足，請稍後再試。",
        "timeout": "AI 分析逾時，舊成功報告仍可正常使用。",
        "server_unavailable": "AI 服務目前無法完成請求，請稍後再試。",
        "output_truncated": "AI 階段輸出未完成，請重新產生。",
        "empty_response": "AI 服務沒有回傳內容，請重新產生。",
        "schema_invalid": "AI 階段輸出格式無法驗證，請重新產生。",
        "provider_error": "AI 服務暫時無法使用，舊成功報告不受影響。",
    }
    return messages.get(stage.error_code, "AI 分析階段暫時無法完成。")


def get_pipeline_status(survey):
    legacy_status = get_report_status(survey)
    successful_synthesis = list(
        SurveyAIAnalysisStage.objects.filter(
            snapshot__survey=survey,
            stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
        )
        .select_related("snapshot__survey")
        .order_by("-generated_at", "-id")[:10]
    )
    current_synthesis = next((stage for stage in successful_synthesis if is_stage_current(stage)), None)
    fallback_synthesis = successful_synthesis[0] if successful_synthesis else None
    report_stage = current_synthesis or fallback_synthesis

    latest_snapshot = (
        current_synthesis.snapshot
        if current_synthesis
        else survey.ai_report_snapshots.order_by("-updated_at", "-id").first()
    )
    stages = latest_stage_status(latest_snapshot) if latest_snapshot else {
        stage_type: {
            "status": "not_started",
            "stage_id": None,
            "revision": None,
            "cache_hit": False,
            "is_current": False,
            "error_code": "",
        }
        for stage_type in STAGE_MODULES
    }
    latest_attempt = (
        SurveyAIAnalysisStage.objects.filter(snapshot__survey=survey)
        .order_by("-updated_at", "-id")
        .first()
    )
    latest_failed = (
        latest_attempt
        if latest_attempt and latest_attempt.status == SurveyAIAnalysisStage.Status.FAILED
        else None
    )
    report = serialize_synthesis_stage(report_stage, is_current=bool(current_synthesis)) if report_stage else None
    if report is None and legacy_status.get("report"):
        report = {**legacy_status["report"], "report_source": "legacy", "draft_states": {}}
    freshness = {
        **legacy_status["freshness"],
        "is_current": bool(current_synthesis),
        "latest_analysis_incomplete": legacy_status["freshness"]["has_enough_data"] and not current_synthesis,
        "latest_ai_status": (
            SurveyAIAnalysisStage.Status.SUCCEEDED
            if current_synthesis
            else latest_failed.status if latest_failed else "not_started"
        ),
        "latest_error_code": latest_failed.error_code if latest_failed and not current_synthesis else "",
        "latest_error_message": (
            _stage_failure_message(latest_failed) if latest_failed and not current_synthesis else ""
        ),
    }
    return {
        "survey": legacy_status["survey"],
        "freshness": freshness,
        "stages": stages,
        "report": report,
    }
