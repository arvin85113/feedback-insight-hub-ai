from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.models import AnalysisJob, FeedbackSubmission, Survey, SurveyAnalysisSource, SurveyAnalysisState
from node.tests.test_console import ConsoleTestCase


class JobsPageTests(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        vault = patch("node.gemini.configured", return_value=False)
        vault.start()
        self.addCleanup(vault.stop)
        with suppress_analysis_scheduling():
            self.survey = Survey.objects.create(title="隔離測試問卷", slug="jobs-fixture")
            FeedbackSubmission.objects.create(survey=self.survey)
        self.state = SurveyAnalysisState.objects.create(survey=self.survey, input_version=1,
            config_version=1, pipeline_version="fixture-v1")

    def job(self, status="pending", **values):
        return AnalysisJob.objects.create(survey=self.survey, status=status, input_version=1,
            config_version=1, pipeline_version="fixture-v1", **values)

    def page(self):
        return self.client.get(reverse("node:jobs"))

    def test_idle_get_does_not_schedule_or_invoke_analysis(self):
        with patch("node.views.AnalysisJobsView.post", side_effect=AssertionError("no POST")):
            response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "開始統計與文字分析")
        self.assertContains(response, "先設定 Gemini 金鑰")
        self.assertContains(response, "每 10 秒更新")
        self.assertFalse(AnalysisJob.objects.exists())

    def test_running_and_pending_are_visible_and_cannot_duplicate(self):
        running = self.job("running", started_at=timezone.now(), heartbeat_at=timezone.now())
        self.job("pending")
        response = self.page()
        card = response.context["surveys"][0]
        self.assertEqual(card.live_job.pk, running.pk)
        self.assertFalse(card.can_analyse)
        self.assertEqual(card.active_base_count, 2)
        self.assertEqual(response.context["work_summary"], {"running": 1, "waiting": 1})
        self.assertContains(response, "最近回報")
        self.assertContains(response, "沒有可靠的完成百分比")
        self.client.post(reverse("node:jobs"), {"action": "analyse", "survey": self.survey.pk})
        self.assertEqual(AnalysisJob.objects.count(), 2)

    def test_waiting_post_does_not_duplicate(self):
        self.job()
        self.assertEqual(self.page().context["surveys"][0].ui_label, "等待執行")
        self.client.post(reverse("node:jobs"), {"action": "analyse", "survey": self.survey.pk})
        self.assertEqual(AnalysisJob.objects.count(), 1)

    def test_current_failure_shows_safe_code_and_retry(self):
        self.job("failed", error_code="external_location_missing")
        response = self.page()
        self.assertEqual(response.context["surveys"][0].ui_label, "分析失敗")
        self.assertContains(response, "external_location_missing")
        self.assertTrue(response.context["surveys"][0].can_analyse)

    def test_old_failure_does_not_mark_current_source_failed(self):
        job = self.job("failed")
        AnalysisJob.objects.filter(pk=job.pk).update(input_version=0)
        self.assertEqual(self.page().context["surveys"][0].ui_label, "尚未分析")

    def test_invalid_external_source_is_actionable(self):
        SurveyAnalysisSource.objects.create(survey=self.survey, kind="external")
        response = self.page()
        card = response.context["surveys"][0]
        self.assertEqual(card.ui_label, "來源需要處理")
        self.assertFalse(card.can_analyse)

    def test_current_published_result_is_distinct_from_worker_success(self):
        manifest = {key: {"input_version": 1, "config_version": 1, "pipeline_version": "fixture-v1",
                          "source_kind": "answers", "source_ref": "", "source_version": ""}
                    for key in ("statistics", "text")}
        SurveyAnalysisState.objects.filter(pk=self.state.pk).update(publication_manifest=manifest)
        response = self.page()
        self.assertEqual(response.context["surveys"][0].ui_label, "結果已更新")
        self.assertContains(response, "查看已發布結果")
        self.assertEqual(response.context["ready_count"], 1)

    def test_cancel_requested_is_not_presented_as_stopped(self):
        self.job("running", cancel_requested_at=timezone.now())
        response = self.page()
        self.assertContains(response, "取消中")
        self.assertContains(response, "等待停止")

    def test_ai_queue_has_its_own_stage_status(self):
        job = self.job("running", executor="ai")
        response = self.page()
        card = response.context["surveys"][0]
        self.assertEqual(card.live_ai_job.pk, job.pk)
        self.assertContains(response, "目前版本已有 Gemini 工作")
        self.assertIsNone(card.live_job)

    def test_unbound_results_show_the_missing_cloud_step_on_both_pages(self):
        SurveyAnalysisState.objects.filter(pk=self.state.pk).update(published_at=timezone.now())
        self.job("succeeded")
        response = self.page()
        self.assertContains(response, "自動上傳雲端")
        self.assertContains(response, "未接上雲端發布")
        self.assertContains(response, "本機結果已發布（不代表已上傳）")
        self.assertEqual(response.context["attention_count"], 1)
        connection = self.client.get(reverse("cloudsync:connection"))
        self.assertEqual(connection.context["unbound_results_count"], 1)
        self.assertContains(connection, "並非已上傳")

    def test_local_history_has_no_cloud_repair_button_or_attention_count(self):
        from cloudsync.tests.test_publication_status import history_copy_batch
        history_copy_batch(self.survey)
        SurveyAnalysisState.objects.filter(pk=self.state.pk).update(published_at=timezone.now())
        response = self.page()
        card = response.context["surveys"][0]
        self.assertTrue(card.cloud_local_only)
        self.assertEqual(response.context["attention_count"], 0)
        self.assertContains(response, "僅本機")
        self.assertContains(response, "這不是同步失敗")
        self.assertNotContains(response, "未接上雲端發布")
        self.assertEqual(card.cloud_setup_url, "")

    def test_local_history_analysis_failure_is_still_actionable(self):
        from cloudsync.tests.test_publication_status import history_copy_batch
        history_copy_batch(self.survey)
        self.job("failed", error_code="fixture_failure")
        response = self.page()
        self.assertEqual(response.context["attention_count"], 1)
        self.assertContains(response, "fixture_failure")

    def test_legacy_running_bar_is_indeterminate_not_fake_percentage(self):
        from django.template.loader import render_to_string
        from node.job_display import describe_job
        html = render_to_string("node/_job_progress.html", {"job": describe_job(self.job("running"))})
        self.assertIn('role="progressbar"', html)
        self.assertIn("nj-progress-indeterminate", html)
        self.assertNotIn("aria-valuenow=", html)

    def test_stage_checkpoint_shows_units_and_accessible_percentage(self):
        job = self.job("running", executor="ai", result_manifest={"_progress": {
            "version": 1, "attempt": 0, "phase": "ai_text", "completed": 1, "total": 4}})
        response = self.page()
        self.assertContains(response, 'aria-valuenow="25"')
        self.assertContains(response, "已完成 1/4 階段（非耗時預估）")
        self.assertContains(response, "Gemini：產生文字洞察")
