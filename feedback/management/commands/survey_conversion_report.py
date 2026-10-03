from django.core.management.base import BaseCommand
from django.db import connection

from cloudapi.models import SurveyDefinitionRevision
from feedback.models import Survey, SurveyAnalysisState
from feedback.published_analysis import _stage_is_current


class Command(BaseCommand):
    help = "唯讀：列出每份問卷的發布狀態、版本、revision 與已發布分析是否仍為最新（部署前檢查，規格 §7.5）。"

    def handle(self, *args, **options):
        settings_dict = connection.settings_dict
        self.stdout.write(
            f"目前使用資料庫：alias={connection.alias!r}, "
            f"ENGINE={settings_dict.get('ENGINE', '')!r}, NAME={settings_dict.get('NAME', '')!r}"
        )
        problems = 0
        for survey in Survey.objects.order_by("pk"):
            has_revision = SurveyDefinitionRevision.objects.filter(
                survey=survey, version=survey.definition_version
            ).exists()
            unconverted = [
                q.title for q in survey.questions.all()
                if (q.kind in ("single_choice", "multiple_choice") and not q.choices)
                or (q.kind == "scale" and q.scale_min is None)
            ]
            state = SurveyAnalysisState.objects.filter(survey=survey).first()
            current = {}
            if state is not None and state.publication_manifest:
                current = {key: _stage_is_current(state, key) for key in ("statistics", "text", "ai")
                           if key in state.publication_manifest}
            status = "已發布" if survey.is_published else "草稿"
            self.stdout.write(
                f"{survey.slug}｜{status}｜定義版本 {survey.definition_version}｜發布版本 {survey.published_version}"
                f"｜revision {'有' if has_revision else '缺'}｜未轉換題目 {len(unconverted)}"
                f"｜最新：{', '.join(f'{k}={v}' for k, v in current.items()) or '無已發布結果'}"
            )
            if not has_revision or unconverted or not survey.is_published:
                problems += 1
        if problems:
            self.stdout.write(self.style.WARNING(f"有 {problems} 份問卷需要檢查。"))
        else:
            self.stdout.write(self.style.SUCCESS("所有問卷皆已發布、題目已轉換且 revision 齊全。"))
