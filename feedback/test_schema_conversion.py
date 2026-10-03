"""Migration 0024 against data created with the 0022 historical models (survey builder plan Task 3)."""

from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from feedback.schema_conversion import ConversionAborted

BEFORE = [("feedback", "0022_surveyanalysisstate_analyzed_through_sequence_and_more"),
          ("cloudapi", "0003_publishedresultrecord")]
AFTER_SCHEMA = [("feedback", "0023_survey_builder_fields")]
AFTER = [("feedback", "0024_convert_survey_definitions")]
MAPPING_PATH = settings.BASE_DIR / "feedback" / "import_mappings" / "tripadvisor_hotel_reviews.json"


class ConversionMigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        self.old = executor.loader.project_state(BEFORE).apps
        self.counter = 0

    def tearDown(self):
        executor = MigrationExecutor(connection)
        if AFTER[0] not in executor.loader.applied_migrations:
            # Rows that made 0024 abort would block the final migrate; remove them first.
            current = executor.loader.project_state(list(executor.loader.applied_migrations)).apps
            current.get_model("feedback", "Answer").objects.all().delete()
            current.get_model("feedback", "Question").objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    # --- helpers on the 0022 historical models -------------------------------------------------
    def old_survey(self, *, slug=None, definition_version=0):
        self.counter += 1
        return self.old.get_model("feedback", "Survey").objects.create(
            title=f"舊問卷{self.counter}", slug=slug or f"old-{self.counter}", definition_version=definition_version)

    def old_question(self, *, survey=None, code=None, kind="short_text", data_type="text", options_text="", **extra):
        survey = survey or self.old_survey()
        self.counter += 1
        return self.old.get_model("feedback", "Question").objects.create(
            survey=survey, code=code or f"legacy-{self.counter}", title=f"題目{self.counter}", kind=kind,
            data_type=data_type, options_text=options_text, order=self.counter, **extra)

    def old_answer(self, question, value):
        submission = self.old.get_model("feedback", "FeedbackSubmission").objects.create(survey_id=question.survey_id)
        return self.old.get_model("feedback", "Answer").objects.create(submission=submission, question=question, value=value)

    def migrate_to(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        return MigrationExecutor(connection).loader.project_state(target).apps

    def migrate_to_0024(self):
        self.new = self.migrate_to(AFTER)
        return self.new

    def new_model(self, name, app="feedback"):
        return self.new.get_model(app, name)

    # --- question conversion ---------------------------------------------------------------------
    def test_choice_questions_get_codes_and_scores(self):
        q = self.old_question(kind="single_choice", data_type="ordinal", options_text="很快\n普通\n很久")
        self.migrate_to_0024()
        new = self.new_model("Question").objects.get(pk=q.pk)
        self.assertEqual([(c["code"], c["label"], c["score"]) for c in new.choices],
                         [("c1", "很快", 1), ("c2", "普通", 2), ("c3", "很久", 3)])
        self.assertTrue(new.ordered)
        self.assertEqual(new.next_choice_number, 4)

    def test_integer_scale_becomes_range(self):
        q = self.old_question(kind="scale", data_type="ordinal", options_text="1\n2\n3\n4\n5")
        self.migrate_to_0024()
        new = self.new_model("Question").objects.get(pk=q.pk)
        self.assertEqual((new.scale_min, new.scale_max, new.choices), (1, 5, []))

    def test_text_scale_without_answers_becomes_ordered_choice(self):
        q = self.old_question(kind="scale", data_type="ordinal", options_text="非常滿意\n滿意\n普通")
        self.migrate_to_0024()
        new = self.new_model("Question").objects.get(pk=q.pk)
        self.assertEqual((new.kind, new.ordered, new.display, new.data_type), ("single_choice", True, "radio", "ordinal"))

    def test_question_counter_skips_existing_codes(self):
        survey = self.old_survey()
        self.old_question(survey=survey, code="q2")
        self.old_question(survey=survey, code="overall-scale")
        self.migrate_to_0024()
        self.assertEqual(self.new_model("Survey").objects.get(pk=survey.pk).next_question_number, 3)

    def test_existing_surveys_become_published_with_revision(self):
        survey = self.old_survey(definition_version=0)
        self.old_question(survey=survey)
        self.migrate_to_0024()
        new = self.new_model("Survey").objects.get(pk=survey.pk)
        self.assertEqual((new.definition_version, new.published_version, new.analysis_definition_version), (1, 1, 1))
        self.assertEqual(new.published_at, new.created_at)
        revision = self.new_model("SurveyDefinitionRevision", "cloudapi").objects.get(survey_id=survey.pk, version=1)
        self.assertEqual(revision.definition["schema_version"], 2)
        self.assertTrue(revision.definition["published"])

    def test_single_choice_answers_get_codes(self):
        q = self.old_question(kind="single_choice", data_type="nominal", options_text="很快\n普通")
        answer = self.old_answer(q, "普通")
        self.migrate_to_0024()
        self.assertEqual(self.new_model("Answer").objects.get(pk=answer.pk).choice_codes, ["c2"])

    def test_updated_at_untouched(self):
        survey = self.old_survey()
        self.old_question(survey=survey, kind="single_choice", data_type="nominal", options_text="甲\n乙")
        before = self.old.get_model("feedback", "Survey").objects.values_list("updated_at", flat=True).get(pk=survey.pk)
        self.migrate_to_0024()
        self.assertEqual(self.new_model("Survey").objects.values_list("updated_at", flat=True).get(pk=survey.pk), before)

    # --- guards: abort without any partial conversion ---------------------------------------------
    def assert_aborts_unchanged(self, question, slug):
        with self.assertRaisesMessage(ConversionAborted, slug):
            self.migrate_to_0024()
        schema_only = MigrationExecutor(connection).loader.project_state(AFTER_SCHEMA).apps
        survey = schema_only.get_model("feedback", "Survey").objects.get(pk=question.survey_id)
        self.assertIsNone(survey.published_version)
        self.assertEqual(schema_only.get_model("feedback", "Question").objects.get(pk=question.pk).choices, [])

    def test_aborts_on_multiple_choice_answers(self):
        q = self.old_question(kind="multiple_choice", data_type="nominal", options_text="A\nB")
        self.old_answer(q, "A, B")
        self.assert_aborts_unchanged(q, q.survey.slug)

    def test_aborts_on_unmatched_single_choice_answer(self):
        q = self.old_question(kind="single_choice", data_type="nominal", options_text="A\nB")
        self.old_answer(q, "其他")
        self.assert_aborts_unchanged(q, q.survey.slug)

    def test_aborts_on_text_scale_with_answers(self):
        q = self.old_question(kind="scale", data_type="ordinal", options_text="非常滿意\n滿意")
        self.old_answer(q, "滿意")
        self.assert_aborts_unchanged(q, q.survey.slug)

    # --- TripAdvisor: published results stay current ---------------------------------------------
    def old_tripadvisor_survey(self):
        from feedback.importing.mapping import load_mapping
        from feedback.importing.service import _question_codes

        mapping = load_mapping(MAPPING_PATH)
        survey = self.old_survey(slug="tripadvisor-hotel-review-ratings", definition_version=0)
        Question = self.old.get_model("feedback", "Question")
        for order, (spec, code) in enumerate(zip(mapping.questions, _question_codes(mapping)), start=1):
            Question.objects.create(
                survey=survey, code=code, title=spec.title, kind=spec.kind, data_type=spec.data_type,
                options_text="\n".join(spec.options), is_required=spec.required,
                enable_keyword_tracking=spec.enable_keyword_tracking, is_active=True, order=order)
        return survey, mapping

    def publish_fixture_results(self, survey):
        Source = self.old.get_model("feedback", "SurveyAnalysisSource")
        Version = self.old.get_model("feedback", "ExternalDatasetVersion")
        source = Source.objects.create(survey=survey, kind="external")
        version = Version.objects.create(
            source=source, source_ref="tripadvisor", source_version="v1", source_revision="r1", cleaning_version="c1",
            content_sha256="a" * 64, mapping_key="tripadvisor-hotel-reviews", mapping_version="v1", row_count=10)
        Source.objects.filter(pk=source.pk).update(active_external_version=version)
        snapshot = self.old.get_model("feedback", "SurveyAIReportSnapshot").objects.create(
            survey=survey, data_fingerprint="b" * 64, snapshot_schema_version="1", prompt_version="1",
            model_name="fixture", status="succeeded")
        stage = self.old.get_model("feedback", "SurveyAIAnalysisStage").objects.create(
            snapshot=snapshot, stage_type="synthesis", status="succeeded", input_hash="c" * 64, schema_version="1",
            prompt_version="1", model_name="fixture")
        pointer = {"input_version": 3, "config_version": 2, "pipeline_version": "p1", "source_kind": "external",
                   "source_ref": "tripadvisor", "source_version": "v1"}
        self.old.get_model("feedback", "SurveyAnalysisState").objects.create(
            survey=survey, input_version=3, config_version=2, pipeline_version="p1", published_snapshot=snapshot,
            published_ai_stage=stage, published_at=timezone.now(),
            publication_manifest={"statistics": pointer, "text": pointer,
                                  "ai": {**pointer, "snapshot_id": snapshot.pk, "stage_id": stage.pk}})
        return stage

    def test_tripadvisor_published_results_stay_current_after_migration(self):
        from feedback.importing.service import mapping_compatibility_errors
        from feedback.models import Survey, SurveyAIAnalysisStage
        from feedback.published_analysis import _stage_is_current, is_published_ai_stage_current

        old_survey, mapping = self.old_tripadvisor_survey()
        stage = self.publish_fixture_results(old_survey)
        self.migrate_to_0024()
        survey = Survey.objects.get(pk=old_survey.pk)
        state = survey.analysis_state
        for key in ("statistics", "text"):
            self.assertTrue(_stage_is_current(state, key), key)
        self.assertTrue(is_published_ai_stage_current(SurveyAIAnalysisStage.objects.get(pk=stage.pk)))
        self.assertEqual(state.config_version, 2)
        self.assertEqual(mapping_compatibility_errors(mapping, survey), [])
        rating = survey.questions.get(code__startswith="overall")
        self.assertEqual((rating.scale_min, rating.scale_max), (1, 5))

    # --- report ----------------------------------------------------------------------------------
    def test_conversion_report_is_read_only(self):
        from feedback.models import Question, Survey

        survey = self.old_survey()
        self.old_question(survey=survey, kind="single_choice", data_type="nominal", options_text="甲\n乙")
        self.migrate_to_0024()
        before = (Survey.objects.count(), Question.objects.count(),
                  list(Survey.objects.values_list("updated_at", "definition_version")))
        out = StringIO()
        call_command("survey_conversion_report", stdout=out)
        self.assertIn(survey.slug, out.getvalue())
        self.assertEqual((Survey.objects.count(), Question.objects.count(),
                          list(Survey.objects.values_list("updated_at", "definition_version"))), before)


class ConversionOutputValidityTests(ConversionMigrationTests):
    """Migration 0024 refuses data whose converted definition the builder would reject (final review #5)."""

    def test_aborts_when_converted_scale_range_is_invalid(self):
        q = self.old_question(kind="scale", data_type="ordinal", options_text="2\n3\n4\n5\n6")
        self.assert_aborts_unchanged(q, q.survey.slug)

    def test_aborts_when_text_scale_has_a_single_option(self):
        q = self.old_question(kind="scale", data_type="ordinal", options_text="還可以")
        self.assert_aborts_unchanged(q, q.survey.slug)
