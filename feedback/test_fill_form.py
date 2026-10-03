import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from feedback.forms import SurveyFormBuilder
from feedback.local_service import SurveyFormOutdated, SurveyNotAccepting, submit_survey_payload
from feedback.models import Answer, FeedbackSubmission, Question, Survey
from feedback.test_utils import published

User = get_user_model()


def make_survey(slug="drink"):
    survey = Survey.objects.create(title="飲料店", slug=slug)
    Question.objects.create(survey=survey, title="門市", kind="single_choice", order=1,
                            choices=[{"code": "", "label": "信義"}, {"code": "", "label": "公館"}])
    Question.objects.create(survey=survey, title="品項", kind="multiple_choice", order=2,
                            choices=[{"code": "", "label": "珍珠, 椰果"}, {"code": "", "label": "紅茶"}])
    Question.objects.create(survey=survey, title="地區", kind="single_choice", display="dropdown", order=3,
                            choices=[{"code": "", "label": "北部"}, {"code": "", "label": "南部"}])
    Question.objects.create(survey=survey, title="推薦", kind="scale", scale_min=0, scale_max=10,
                            scale_min_label="完全不會", scale_max_label="一定會", order=4)
    Question.objects.create(survey=survey, title="杯數", kind="integer", order=5)
    Question.objects.create(survey=survey, title="分鐘", kind="decimal", order=6)
    return survey


class FillFormTests(TestCase):
    def setUp(self):
        self.survey = published(make_survey())
        self.q = {q.title: q for q in self.survey.questions.all()}
        self.customer = User.objects.create_user(username="c", password="x")
        self.manager = User.objects.create_user(username="m", password="x", role="manager")
        self.url = reverse("feedback:survey-detail", args=[self.survey.slug])

    def answers(self, **overrides):
        data = {
            f"question_{self.q['門市'].id}": "c2",
            f"question_{self.q['品項'].id}": ["c1", "c2"],
            f"question_{self.q['地區'].id}": "c1",
            f"question_{self.q['推薦'].id}": "9",
            f"question_{self.q['杯數'].id}": "2",
            f"question_{self.q['分鐘'].id}": "3.5",
        }
        data.update(overrides)
        return data

    def post(self, version=None, key=None, **overrides):
        data = {**self.answers(**overrides), "meta-idempotency_key": key or str(uuid.uuid4())}
        if version != "missing":
            data["definition_version"] = self.survey.published_version if version is None else version
        return self.client.post(self.url, data)

    # --- form ------------------------------------------------------------------------------------
    def test_choice_value_is_code_not_label(self):
        form = SurveyFormBuilder(survey=self.survey)
        self.assertEqual(form.fields[f"question_{self.q['門市'].id}"].choices, [("c1", "信義"), ("c2", "公館")])

    def test_dropdown_uses_select_widget(self):
        field = SurveyFormBuilder(survey=self.survey).fields[f"question_{self.q['地區'].id}"]
        self.assertEqual(field.widget.__class__.__name__, "Select")

    def test_scale_renders_range_with_end_labels(self):
        self.client.force_login(self.customer)
        page = self.client.get(self.url).content.decode()
        name = f'name="question_{self.q["推薦"].id}"'
        self.assertEqual(page.count(name), 11)
        self.assertIn("完全不會", page)
        self.assertIn("一定會", page)
        self.assertNotIn("checked", page.split(name)[1][:200])

    def test_integer_and_decimal_fields(self):
        form = SurveyFormBuilder(survey=self.survey)
        self.assertEqual(form.fields[f"question_{self.q['杯數'].id}"].__class__.__name__, "IntegerField")
        self.assertEqual(form.fields[f"question_{self.q['分鐘'].id}"].__class__.__name__, "DecimalField")

    def test_all_fill_forms_carry_published_version(self):
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(self.url), f'name="definition_version" value="{self.survey.published_version}"')

    # --- writing ---------------------------------------------------------------------------------
    def test_submission_records_codes_and_published_version(self):
        self.client.force_login(self.customer)
        response = self.post()
        self.assertEqual(response.status_code, 302)
        submission = FeedbackSubmission.objects.get()
        self.assertEqual(submission.definition_version, self.survey.published_version)
        store = Answer.objects.get(question=self.q["門市"])
        self.assertEqual((store.choice_codes, store.value), (["c2"], "公館"))
        items = Answer.objects.get(question=self.q["品項"])
        self.assertEqual((items.choice_codes, items.value), (["c1", "c2"], "珍珠, 椰果, 紅茶"))

    def test_missing_or_wrong_form_version_creates_nothing(self):
        self.client.force_login(self.customer)
        self.assertContains(self.post(version="missing"), "問卷已更新，請確認後重新送出")
        self.assertContains(self.post(version=99), "問卷已更新，請確認後重新送出")
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_service_rejects_draft_and_closed_survey(self):
        draft = make_survey("draft")
        with self.assertRaises(SurveyNotAccepting):
            submit_survey_payload(draft, user=self.customer, respondent_name="", respondent_email="",
                                  consent_follow_up=False, answers={}, form_version=1)
        Survey.objects.filter(pk=self.survey.pk).update(is_active=False)
        with self.assertRaises(SurveyNotAccepting):
            submit_survey_payload(Survey.objects.get(pk=self.survey.pk), user=self.customer, respondent_name="",
                                  respondent_email="", consent_follow_up=False, answers={},
                                  form_version=self.survey.published_version)
        Survey.objects.filter(pk=self.survey.pk).update(is_active=True)
        with self.assertRaises(SurveyFormOutdated):
            submit_survey_payload(self.survey, user=self.customer, respondent_name="", respondent_email="",
                                  consent_follow_up=False, answers={}, form_version=98)

    def test_resubmit_after_close_returns_previous_result(self):
        key = uuid.uuid4()
        kwargs = dict(user=self.customer, respondent_name="", respondent_email="", consent_follow_up=False,
                      answers={f"question_{self.q['門市'].id}": "c1"}, idempotency_key=key,
                      form_version=self.survey.published_version)
        first = submit_survey_payload(self.survey, **kwargs)
        Survey.objects.filter(pk=self.survey.pk).update(is_active=False)
        again = submit_survey_payload(Survey.objects.get(pk=self.survey.pk), **kwargs)
        self.assertEqual((again["submission_id"], again["reused"]), (first["submission_id"], True))

    def test_same_key_with_other_survey_answers_or_consent_rejected(self):
        key = uuid.uuid4()
        base = dict(user=self.customer, respondent_name="", respondent_email="", consent_follow_up=False,
                    answers={f"question_{self.q['門市'].id}": "c1"}, idempotency_key=key,
                    form_version=self.survey.published_version)
        submit_survey_payload(self.survey, **base)
        other = published(make_survey("other"))
        variants = [
            (other, {**base, "form_version": other.published_version}),
            (self.survey, {**base, "answers": {f"question_{self.q['門市'].id}": "c2"}}),
            (self.survey, {**base, "consent_follow_up": True}),
        ]
        for survey, kwargs in variants:
            with self.assertRaisesMessage(ValueError, "idempotency key 已由其他填答使用"):
                submit_survey_payload(survey, **kwargs)

    def test_customer_http_resend_after_close_or_archive_redirects_to_success(self):
        self.client.force_login(self.customer)
        key = str(uuid.uuid4())
        self.assertEqual(self.post(key=key).status_code, 302)
        for change in ({"is_active": False}, {"archived_at": "2026-10-03T00:00:00+00:00"}):
            Survey.objects.filter(pk=self.survey.pk).update(**change)
            response = self.post(key=key)
            self.assertRedirects(response, reverse("feedback:survey-success", args=[self.survey.slug]),
                                 fetch_redirect_response=False)
        self.assertEqual(FeedbackSubmission.objects.count(), 1)

    def test_view_checked_then_closed_before_write_is_rejected(self):
        self.client.force_login(self.customer)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        Survey.objects.filter(pk=self.survey.pk).update(is_active=False)
        self.assertContains(self.post(), "這份問卷目前未開放填答。")
        self.assertFalse(FeedbackSubmission.objects.exists())

    # --- drafts and preview ----------------------------------------------------------------------
    def test_draft_shows_not_open_notice(self):
        draft = make_survey("draft")
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(reverse("feedback:survey-detail", args=[draft.slug])), "問卷尚未開放")

    def test_manager_preview_of_draft_has_no_submit(self):
        draft = make_survey("draft")
        self.client.force_login(self.manager)
        page = self.client.get(reverse("feedback:survey-detail", args=[draft.slug]) + "?preview=1")
        self.assertContains(page, "預覽模式")
        self.assertContains(page, "門市")
        self.assertNotContains(page, "送出回饋")

    def test_preview_header_sameorigin_only_for_manager_preview(self):
        draft = make_survey("draft")
        url = reverse("feedback:survey-detail", args=[draft.slug]) + "?preview=1"
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url)["X-Frame-Options"], "SAMEORIGIN")
        self.client.force_login(self.customer)
        customer_view = self.client.get(url)
        self.assertEqual(customer_view["X-Frame-Options"], "DENY")
        self.assertContains(customer_view, "問卷尚未開放")
        self.assertEqual(self.client.get(self.url)["X-Frame-Options"], "DENY")

    def test_preview_post_rejected(self):
        draft = make_survey("draft")
        self.client.force_login(self.manager)
        response = self.client.post(reverse("feedback:survey-detail", args=[draft.slug]) + "?preview=1", {})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FeedbackSubmission.objects.exists())
