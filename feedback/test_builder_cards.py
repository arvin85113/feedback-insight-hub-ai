from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from feedback import survey_lifecycle
from feedback.models import Survey
from feedback.question_schema import UI_TYPES
from feedback.test_utils import cloud_only


def builder_url(survey):
    return reverse("feedback:survey-builder", args=[survey.slug])


@cloud_only
class BuilderCardTests(TestCase):
    def setUp(self):
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)
        self.survey = survey_lifecycle.create_draft({"title": "飲料店"})

    def refreshed(self):
        return Survey.objects.get(pk=self.survey.pk)

    def post_card(self, *, ui_type, title, choices=None, rows=None, question_uuid="", definition_version=None,
                  **extra):
        data = {
            "action": "save-question", "question_uuid": question_uuid, "ui_type": ui_type, "title": title,
            "help_text": "", "is_required": "on",
            "definition_version": self.refreshed().definition_version if definition_version is None else definition_version,
            "scale_min": "1", "scale_max": "5", "score_start": "1",
        }
        rows = rows if rows is not None else [(label, False) for label in (choices or [])]
        for index, (label, excluded) in enumerate(rows):
            data[f"choices-{index}-label"] = label
            data[f"choices-{index}-code"] = ""
            data[f"choices-{index}-position"] = str(index)
            data[f"choices-{index}-excluded"] = ["0", "1"] if excluded else ["0"]
        for key, value in extra.items():
            data[key] = "on" if value is True else value
        return self.client.post(builder_url(self.survey), data)

    def test_each_ui_type_saves_with_defaults(self):
        for ui_type in UI_TYPES:
            response = self.post_card(ui_type=ui_type, title=f"{ui_type} 題", choices=["甲", "乙"])
            self.assertEqual(response.status_code, 302, ui_type)
        survey = self.refreshed()
        self.assertEqual(survey.questions.count(), 7)
        kinds = {q.title: (q.kind, q.display) for q in survey.questions.all()}
        self.assertEqual(kinds["dropdown 題"], ("single_choice", "dropdown"))
        self.assertEqual(kinds["number 題"], ("integer", ""))

    def test_posted_data_type_is_ignored(self):
        self.post_card(ui_type="scale", title="滿意度", data_type="continuous")
        self.assertEqual(self.refreshed().questions.get().data_type, "ordinal")

    def test_invalid_card_shows_error_and_keeps_input(self):
        response = self.post_card(ui_type="radio", title="門市", choices=["甲", "甲"])
        self.assertContains(response, "選項文字不可重複", status_code=200)
        self.assertContains(response, 'value="門市"', status_code=200)
        self.assertContains(response, 'value="甲"', status_code=200)
        self.assertFalse(self.refreshed().questions.exists())

    def test_version_conflict_keeps_input(self):
        response = self.post_card(ui_type="short_text", title="我的修改",
                                  definition_version=self.refreshed().definition_version - 1)
        self.assertContains(response, "此問卷已在其他視窗修改", status_code=409)
        self.assertContains(response, 'value="我的修改"', status_code=409)
        self.assertContains(response, "重新載入（捨棄我的修改）", status_code=409)
        self.assertContains(response, "繼續編輯", status_code=409)

    def test_published_survey_renders_read_only_with_copy(self):
        self.post_card(ui_type="short_text", title="感想")
        self.client.post(builder_url(self.survey), {"action": "publish",
                                                    "definition_version": self.refreshed().definition_version})
        response = self.client.get(builder_url(self.survey))
        self.assertContains(response, "複製為新草稿")
        self.assertNotContains(response, 'value="save-question"')

    def test_decimal_hint_present(self):
        self.assertContains(self.client.get(builder_url(self.survey)), "允許小數的數字題可做平均數比較與相關分析")

    def test_choice_rows_keep_excluded_on_the_right_row(self):
        self.post_card(ui_type="radio", title="等候", ordered=True,
                       rows=[("很快", False), ("不適用 ", True), ("很久", False)])
        question = self.refreshed().questions.get()
        self.assertEqual([(c["label"], c["excluded"]) for c in question.choices],
                         [("很快", False), ("不適用", True), ("很久", False)])
        self.assertEqual(question.analysis_options, ["很快", "很久"])

    def test_choice_reorder_and_blank_rows(self):
        data_rows = [("甲", False), ("", False), ("乙", False)]
        response = self.post_card(ui_type="radio", title="排序", rows=data_rows,
                                  **{"choices-0-position": "2", "choices-2-position": "0"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([c["label"] for c in self.refreshed().questions.get().choices], ["乙", "甲"])

    def test_edit_keeps_choice_codes(self):
        self.post_card(ui_type="radio", title="門市", choices=["信義", "公館"])
        question = self.refreshed().questions.get()
        data_rows = [("信義區", False), ("公館", False)]
        self.post_card(ui_type="radio", title="門市", rows=data_rows, question_uuid=str(question.uuid),
                       **{"choices-0-code": "c1", "choices-1-code": "c2"})
        self.assertEqual([(c["code"], c["label"]) for c in self.refreshed().questions.get().choices],
                         [("c1", "信義區"), ("c2", "公館")])

    def test_duplicate_question_places_copy_after_original(self):
        self.post_card(ui_type="short_text", title="第一題")
        self.post_card(ui_type="short_text", title="第二題")
        first = self.refreshed().questions.get(title="第一題")
        self.client.post(builder_url(self.survey), {"action": "duplicate-question", "question_uuid": str(first.uuid),
                                                    "definition_version": self.refreshed().definition_version})
        titles = list(self.refreshed().questions.order_by("order", "id").values_list("title", flat=True))
        self.assertEqual(titles, ["第一題", "第一題", "第二題"])

    def test_long_text_tracking_default_on_short_text_off(self):
        response = self.client.get(builder_url(self.survey))
        self.assertContains(response, 'data-tracking-default-long="1"')
        self.post_card(ui_type="long_text", title="建議", enable_keyword_tracking=True)
        self.post_card(ui_type="short_text", title="姓名")
        questions = {q.title: q.enable_keyword_tracking for q in self.refreshed().questions.all()}
        self.assertEqual(questions, {"建議": True, "姓名": False})

    def test_new_card_has_no_question_type_until_chosen(self):
        response = self.client.get(builder_url(self.survey))
        self.assertContains(response, '<option value="" selected disabled>請選擇題型</option>', html=True)

    def test_new_card_without_type_shows_error_and_saves_nothing(self):
        response = self.post_card(ui_type="", title="還沒選題型")
        self.assertContains(response, "請選擇題型", status_code=200)
        self.assertContains(response, 'value="還沒選題型"', status_code=200)
        self.assertFalse(self.refreshed().questions.exists())

    def test_move_announces_new_position(self):
        self.post_card(ui_type="short_text", title="A")
        self.post_card(ui_type="short_text", title="B")
        b = self.refreshed().questions.get(title="B")
        response = self.client.post(builder_url(self.survey), {
            "action": "move-question", "direction": "up", "question_uuid": str(b.uuid),
            "definition_version": self.refreshed().definition_version}, follow=True)
        self.assertContains(response, "已移到第 1 題")


@cloud_only
class SurveyCreatePageTests(TestCase):
    def setUp(self):
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)

    def test_create_page_has_no_activation_toggle_or_promo(self):
        response = self.client.get(reverse("feedback:survey-create"))
        self.assertNotContains(response, "立即啟用問卷")
        self.assertNotContains(response, "智能分析已就緒")
        self.assertNotContains(response, "發佈")
        self.assertContains(response, "自動分析")

    def test_created_survey_is_an_accepting_draft(self):
        response = self.client.post(reverse("feedback:survey-create"), {
            "title": "咖啡店", "description": "", "analysis_enabled": "on", "thank_you_email_enabled": "on"})
        self.assertEqual(response.status_code, 302)
        survey = Survey.objects.get(title="咖啡店")
        self.assertTrue(survey.is_active)
        self.assertIsNone(survey.published_version)

    def test_settings_panel_uses_current_wording(self):
        survey = survey_lifecycle.create_draft({"title": "咖啡店"})
        response = self.client.get(builder_url(survey))
        self.assertContains(response, "收件中")
        self.assertContains(response, "自動分析")
        self.assertNotContains(response, "發佈")
        self.assertNotContains(response, "立即啟用問卷")
