"""Read-only workflow visibility; local publication is not cloud acknowledgement."""

from django.db.models import Exists, F, OuterRef
from django.urls import reverse

from cloudapi.models import SurveyDefinitionRevision
from feedback.models import DatasetImportBatch, Survey, SurveyAnalysisSource, SurveyAnalysisState

from .models import ResultUpload


def local_history_surveys():
    """Verified copy-only imports, never inferred from a title or a missing binding.

    A cloud revision or explicit external binding takes precedence: do not conceal
    an actual upload/source problem merely because a historical batch exists.
    """
    return Survey.objects.annotate(
        _history_copy=Exists(DatasetImportBatch.objects.filter(
            survey_id=OuterRef("pk"), source_name="cloud-history",
            status=DatasetImportBatch.Status.COMPLETED, summary__copy_only=True, summary__reconciled=True,
        )),
        _cloud_revision=Exists(SurveyDefinitionRevision.objects.filter(survey_id=OuterRef("pk"))),
        _external_binding=Exists(SurveyAnalysisSource.objects.filter(
            survey_id=OuterRef("pk"), kind=SurveyAnalysisSource.Kind.EXTERNAL,
        )),
    ).filter(_history_copy=True, _cloud_revision=False, _external_binding=False)


def publication_issues():
    """Missing binding/outbox for a published, enabled source, without reading result bodies."""
    return SurveyAnalysisState.objects.filter(
        published_at__isnull=False, survey__analysis_enabled=True, survey__archived_at__isnull=True,
    ).annotate(
        cloud_bound=Exists(SurveyDefinitionRevision.objects.filter(survey_id=OuterRef("survey_id"))),
        queued=Exists(ResultUpload.objects.filter(survey_id=OuterRef("survey_id"), published_at=OuterRef("published_at"))),
    ).filter(queued=False).exclude(survey_id__in=local_history_surveys().values("pk"))


def current_failed_uploads():
    """Failed uploads of the version each enabled survey shows now; superseded failures stay history."""
    return ResultUpload.objects.filter(
        status=ResultUpload.Status.FAILED, published_at=F("survey__analysis_state__published_at"),
        survey__analysis_enabled=True, survey__archived_at__isnull=True,
    )


def decorate_publications(surveys, link):
    """Attach the status of the currently shown publication, never an older upload."""
    ids = [survey.pk for survey in surveys]
    bound = set(SurveyDefinitionRevision.objects.filter(survey_id__in=ids).values_list("survey_id", flat=True))
    local_only = set(local_history_surveys().filter(pk__in=ids).values_list("pk", flat=True))
    uploads = {}
    for upload in ResultUpload.objects.filter(
        survey_id__in=ids, published_at=F("survey__analysis_state__published_at"),
    ).order_by("-publish_sequence", "-pk").values(
        "survey_id", "published_at", "status", "last_error",
    ):
        uploads.setdefault((upload["survey_id"], upload["published_at"]), upload)
    linked = bool(link and link.is_linked)
    for survey in surveys:
        state = getattr(survey, "analysis_state", None)
        survey.cloud_bound = survey.pk in bound
        survey.cloud_local_only = survey.pk in local_only
        survey.cloud_setup_url = reverse("cloudsync:connection")
        survey.cloud_setup_label = "查看雲端連線"
        survey.ui_upload_tone, survey.ui_upload_label = "neutral", "等待本機發布"
        survey.ui_upload_hint = "發布後同一交易建立上傳紀錄，由背景同步自動送出。"
        if survey.cloud_local_only:
            survey.ui_upload_tone, survey.ui_upload_label = "neutral", "僅本機"
            survey.ui_upload_hint = "歷史副本只在本機分析與保存，不參與雲端上傳；這不是同步失敗。舊雲端問卷與資料仍保留。"
            survey.cloud_setup_url, survey.cloud_setup_label = "", ""
            continue
        if not survey.cloud_bound:
            survey.ui_upload_tone, survey.ui_upload_label = "waiting", "未接上雲端發布"
            if getattr(survey, "is_external_source", False):
                survey.cloud_setup_url = reverse("node:datasets") + f"?survey={survey.pk}"
                survey.cloud_setup_label = "接上雲端發布"
                survey.ui_upload_hint = "已有本機檔案，但未完成雲端來源登錄；先確認登錄，之後更新自動上傳。"
            else:
                survey.ui_upload_hint = "這份是本機副本，不是節點所屬的線上問卷；依接手規格完成新節點問卷後才可自動發布，不能直接覆蓋舊問卷。"
            continue
        if not state or not state.published_at:
            continue
        upload = uploads.get((survey.pk, state.published_at))
        if upload is None:
            survey.ui_upload_tone, survey.ui_upload_label = "waiting", "等待補建上傳紀錄"
            survey.ui_upload_hint = "下次背景同步會補建目前發布版本；本機完成不代表已上傳。"
        else:
            survey.ui_upload_tone, survey.ui_upload_label, survey.ui_upload_hint = {
                "uploaded": ("ready", "雲端已接收此版本", "目前本機發布版本已收到雲端套用確認。"),
                "pending": ("waiting", "等待自動上傳", "背景同步會自動上傳，通常每 5 分鐘一次；失敗保留紀錄並依錯誤分類重試。"),
                "failed": ("failed", "上傳需要處理", "本機結果仍保留；請到雲端連線查看錯誤，不需要重新呼叫 Gemini。"),
                "stale": ("waiting", "雲端未套用此版本", "雲端只保留這份上傳的歷史紀錄；請核對資料與定義版本，不可倒退覆蓋。"),
            }.get(upload["status"], ("waiting", "上傳狀態待確認", "請至雲端連線查看。"))
        if not linked and (upload is None or upload["status"] != "uploaded"):
            survey.ui_upload_tone, survey.ui_upload_label = "waiting", "離線，結果保留待上傳"
            survey.ui_upload_hint = "重新連結後背景同步會自動補建或重送；不用重跑分析。"
