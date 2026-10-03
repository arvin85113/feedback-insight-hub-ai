"""Dataset import with the builder schema (builder spec §6, §7.1)."""

from pathlib import Path

from django.test import TestCase

from cloudapi.models import SurveyDefinitionRevision
from feedback.importing.mapping import load_mapping
from feedback.importing.service import _ensure_survey_and_questions, _question_codes, import_dataset
from feedback.models import Answer, FeedbackSubmission, Question, Survey
from feedback.question_schema import derive_data_type

HERE = Path(__file__).resolve().parent
FIXTURE_DATA = HERE / "fixtures" / "external_import_test.jsonl"
FIXTURE_MAPPING = HERE / "fixtures" / "external_import_test_mapping.json"
TRIPADVISOR = HERE / "import_mappings" / "tripadvisor_hotel_reviews.json"
AMAZON = HERE / "import_mappings" / "amazon_beauty_reviews_2023.json"


class ImportSchemaTests(TestCase):
    def test_both_mappings_pass_derive_data_type(self):
        for path in (TRIPADVISOR, AMAZON):
            for spec in load_mapping(path).questions:
                self.assertEqual(spec.data_type, derive_data_type(spec.kind, ordered=spec.data_type == "ordinal"),
                                 f"{path.name}: {spec.title}")

    def test_tripadvisor_scales_become_ranges_and_published(self):
        survey, questions = _ensure_survey_and_questions(load_mapping(TRIPADVISOR))
        self.assertIsNotNone(survey.published_version)
        overall = next(q for q in questions if q.code.startswith("overall"))
        self.assertEqual((overall.kind, overall.scale_min, overall.scale_max, overall.choices), ("scale", 1, 5, []))
        self.assertEqual(overall.analysis_options, ["1", "2", "3", "4", "5"])

    def test_title_question_not_text_tracked(self):
        _survey, questions = _ensure_survey_and_questions(load_mapping(TRIPADVISOR))
        tracked = {q.kind: q.enable_keyword_tracking for q in questions if q.kind in ("short_text", "long_text")}
        self.assertEqual(tracked, {"short_text": False, "long_text": True})

    def test_new_import_versions_and_revision_consistent(self):
        import_dataset(FIXTURE_DATA, load_mapping(FIXTURE_MAPPING), limit=20, seed=42, batch_size=5)
        survey = Survey.objects.get()
        self.assertEqual((survey.definition_version, survey.published_version, survey.analysis_definition_version),
                         (1, 1, 1))
        self.assertEqual(list(SurveyDefinitionRevision.objects.filter(survey=survey).values_list("version", flat=True)),
                         [1])
        self.assertEqual(set(FeedbackSubmission.objects.values_list("definition_version", flat=True)), {1})

    def test_small_import_writes_choice_codes(self):
        mapping = load_mapping(FIXTURE_MAPPING)
        import_dataset(FIXTURE_DATA, mapping, limit=20, seed=42, batch_size=5)
        choice_answers = Answer.objects.filter(question__kind="single_choice")
        self.assertTrue(choice_answers.exists())
        for answer in choice_answers:
            labels = {c["code"]: c["label"] for c in answer.question.choices}
            self.assertEqual([labels[code] for code in answer.choice_codes], [answer.value])

    def test_existing_converted_questions_are_compatible(self):
        """Questions shaped like migration 0024's output are accepted on a re-import."""

        mapping = load_mapping(TRIPADVISOR)
        survey = Survey.objects.create(title=mapping.survey.title, slug="tripadvisor-hotel-review-ratings",
                                       published_version=1, definition_version=1, analysis_definition_version=1)
        for order, (spec, code) in enumerate(zip(mapping.questions, _question_codes(mapping)), start=1):
            Question.objects.create(survey=survey, code=code, title=spec.title, kind=spec.kind, data_type=spec.data_type,
                                    options_text="\n".join(spec.options), is_required=spec.required,
                                    enable_keyword_tracking=spec.enable_keyword_tracking, order=order)
        again, questions = _ensure_survey_and_questions(mapping)
        self.assertEqual((again.pk, len(questions)), (survey.pk, len(mapping.questions)))
