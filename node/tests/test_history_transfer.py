import copy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import uuid
import zipfile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cloudapi.definition import serialize_definition
from cloudapi.models import SurveyDefinitionRevision
from cloudsync.models import PendingAck, ResultUpload, SurveySyncState
from feedback.analysis_jobs import suppress_analysis_scheduling
from feedback.history_transfer import HistoryBundle, HistoryError, digest, encoded, export_history, import_history
from feedback.models import AnalysisJob, Answer, DatasetImportBatch, FeedbackSubmission, ImportedSubmissionSource, Question, Survey
from organizations.models import Organization, OrganizationMembership

ORIGIN = "https://example.onrender.com"


class HistoryTransferTests(TransactionTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / "history.zip"
        with suppress_analysis_scheduling():
            self.survey = Survey.objects.create(title="線上問卷", slug="legacy", definition_version=2,
                published_version=2, published_at=timezone.now(), analysis_definition_version=2)
            self.question = Question.objects.create(survey=self.survey, title="原始評論", code="q1", kind="long_text", data_type="text")
            self.sub = FeedbackSubmission.objects.create(survey=self.survey, submitted_at=timezone.now(),
                definition_version=None, respondent_name="敏感姓名", respondent_email="private@example.com", consent_follow_up=True)
            self.answer = Answer.objects.create(submission=self.sub, question=self.question, value="原文 e\u0301\n不改寫",
                analysis_text="舊文字快取", sentiment_score=0.7, analysis_version="old-version")

    def export(self, path=None, *, origin=ORIGIN, contacts=False):
        with override_settings(IS_NODE=False):
            return export_history([self.survey.slug], path or self.path, origin=origin, contacts=contacts)

    def imported(self, path=None):
        with HistoryBundle(path or self.path) as bundle:
            return import_history(bundle)

    def rewrite(self, mutate):
        with zipfile.ZipFile(self.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            entry = manifest["surveys"][0]
            rows = [json.loads(line) for line in archive.read(entry["records_file"]).splitlines()]
        mutate(manifest, rows)
        entry = manifest["surveys"][0]
        raw = b"".join(encoded(row) + b"\n" for row in rows)
        import hashlib
        entry.update(records_sha256=hashlib.sha256(raw).hexdigest(), submission_count=len(rows),
                     answer_count=sum(len(r["answers"]) for r in rows))
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("manifest.json", encoded(manifest))
            archive.writestr(entry["records_file"], raw)

    def test_export_is_select_only_and_excludes_accounts_contacts_and_cache(self):
        with CaptureQueriesContext(connection) as queries:
            result = self.export()
        self.assertEqual(result, {"surveys": 1, "submissions": 1, "answers": 1})
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        with HistoryBundle(self.path) as bundle:
            row = next(bundle.records(bundle.surveys[0]))[1]
            self.assertEqual(row["answers"][0]["value"], self.answer.value)
            self.assertEqual((row["respondent_name"], row["respondent_email"], row["definition_version"]), ("", "", None))
            text = encoded(row).decode()
            for forbidden in ("user_id", "private@example.com", "analysis_text", "old-version"):
                self.assertNotIn(forbidden, text)

    def test_copy_is_idempotent_preserves_time_and_never_changes_cloud_or_schedules(self):
        self.export()
        first = self.imported()
        again = self.imported()
        row = first["surveys"][0]
        local = Survey.objects.get(slug=row["local_slug"])
        sub = local.submissions.get()
        self.assertEqual((row["copied"], again["surveys"][0]["duplicates"]), (1, 1))
        self.assertEqual((sub.submitted_at, sub.definition_version, sub.consent_follow_up, sub.user_id),
                         (self.sub.submitted_at, None, True, None))
        self.assertEqual(sub.answers.get().value, self.answer.value)
        self.assertIsNone(sub.answers.get().analysis_version)
        self.assertFalse(local.accepts_responses)
        self.assertNotEqual(local.uuid, self.survey.uuid)
        self.survey.refresh_from_db()
        self.assertTrue(self.survey.is_active)
        self.assertIsNone(self.survey.owner_node_id)
        self.assertEqual(self.survey.submissions.count(), 1)
        for model in (AnalysisJob, PendingAck, ResultUpload, SurveySyncState, SurveyDefinitionRevision):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_contacts_are_opt_in_and_preserved_only_when_included(self):
        self.export(contacts=True)
        self.imported()
        copied = FeedbackSubmission.objects.exclude(pk=self.sub.pk).get()
        self.assertEqual((copied.respondent_name, copied.respondent_email), (self.sub.respondent_name, self.sub.respondent_email))

    def test_manual_existing_worker_can_publish_local_copy_without_upload(self):
        from cloudsync.publication_status import decorate_publications, local_history_surveys, publication_issues
        from feedback.analysis_jobs import schedule_survey_analysis
        from feedback.models import SurveyAnalysisState
        self.export()
        report = self.imported()
        local = Survey.objects.get(slug=report["surveys"][0]["local_slug"])
        self.assertFalse(AnalysisJob.objects.exists())
        job = schedule_survey_analysis(local.pk, change="input")
        call_command("run_analysis_worker", worker_id="history-fixture", output=str(self.path.parent / "artifacts"),
                     once=True, stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error_code)
        state = SurveyAnalysisState.objects.get(survey=local)
        self.assertEqual(state.published_snapshot.response_count, 1)
        self.assertFalse(ResultUpload.objects.exists())
        self.assertTrue(local_history_surveys().filter(pk=local.pk).exists())
        decorate_publications([local], None)
        self.assertEqual(local.ui_upload_label, "僅本機")
        self.assertEqual(local.cloud_setup_url, "")
        self.assertFalse(publication_issues().filter(survey=local).exists())

    def test_original_import_provenance_is_preserved_without_freeform_metadata(self):
        batch = DatasetImportBatch.objects.create(survey=self.survey, source_name="fixture", source_version="v1",
            input_file_sha256="a" * 64, mapping_version="v1")
        ImportedSubmissionSource.objects.create(submission=self.sub, batch=batch, source_namespace="fixture",
            source_record_key="b" * 64, content_sha256="c" * 64, source_version="v1", metadata={"user_id": "must-not-export"})
        self.export()
        self.imported()
        source = ImportedSubmissionSource.objects.exclude(submission=self.sub).get()
        self.assertEqual(source.metadata["original_provenance"]["source_record_key"], "b" * 64)
        self.assertNotIn("must-not-export", encoded(source.metadata).decode())

    def test_same_source_changed_content_fails_without_overwriting(self):
        self.export()
        self.imported()
        baseline = (Survey.objects.count(), FeedbackSubmission.objects.count(), DatasetImportBatch.objects.count())
        self.rewrite(lambda manifest, rows: rows[0]["answers"][0].update(value="內容變更"))
        with self.assertRaises(HistoryError):
            self.imported()
        self.assertEqual(baseline, (Survey.objects.count(), FeedbackSubmission.objects.count(), DatasetImportBatch.objects.count()))
        self.assertEqual(Answer.objects.exclude(pk=self.answer.pk).get().value, self.answer.value)

    def test_local_corruption_is_not_reported_as_successful_duplicate(self):
        self.export()
        self.imported()
        Answer.objects.exclude(pk=self.answer.pk).update(value="本機改動")
        with self.assertRaises(HistoryError):
            self.imported()

    def test_second_row_conflict_rolls_back_preceding_new_row(self):
        self.export()
        self.imported()
        def mutate(manifest, rows):
            new = copy.deepcopy(rows[0])
            new["submission_uuid"] = str(uuid.uuid4())
            rows.insert(0, new)
            rows[1]["answers"][0]["value"] = "衝突"
        self.rewrite(mutate)
        before = FeedbackSubmission.objects.count()
        with self.assertRaises(HistoryError):
            self.imported()
        self.assertEqual(FeedbackSubmission.objects.count(), before)
        self.assertEqual(DatasetImportBatch.objects.filter(source_name="cloud-history").count(), 1)

    def test_definition_change_rejects_not_overwrites(self):
        self.export()
        self.imported()
        def mutate(manifest, rows):
            item = manifest["surveys"][0]
            item["definition"]["questions"][0]["title"] = "新語意"
            item["definition_sha256"] = digest(item["definition"])
        self.rewrite(mutate)
        with self.assertRaises(HistoryError):
            self.imported()

    def test_unknown_question_rejected_before_import(self):
        self.export()
        self.rewrite(lambda manifest, rows: rows[0]["answers"][0].update(question_uuid=str(uuid.uuid4())))
        with self.assertRaises(HistoryError):
            HistoryBundle(self.path)
        self.assertFalse(DatasetImportBatch.objects.exists())

    def test_unknown_fields_are_rejected(self):
        self.export()
        self.rewrite(lambda manifest, rows: rows[0].update(user_id="not-allowed"))
        with self.assertRaises(HistoryError):
            HistoryBundle(self.path)

    def test_duplicate_submission_in_package_is_rejected(self):
        self.export()
        self.rewrite(lambda manifest, rows: rows.append(copy.deepcopy(rows[0])))
        with self.assertRaises(HistoryError):
            HistoryBundle(self.path)

    def test_manifest_count_tamper_rejected(self):
        self.export()
        with zipfile.ZipFile(self.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            item = manifest["surveys"][0]
            raw = archive.read(item["records_file"])
        item["submission_count"] += 1
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("manifest.json", encoded(manifest))
            archive.writestr(item["records_file"], raw)
        with self.assertRaises(HistoryError):
            HistoryBundle(self.path)

    def test_checksum_tamper_and_unknown_zip_entry_rejected(self):
        self.export()
        with zipfile.ZipFile(self.path, "a") as archive:
            archive.writestr("../../outside", "bad")
        with self.assertRaises(HistoryError):
            HistoryBundle(self.path)

    def test_frozen_bundle_does_not_reread_mutated_source_path(self):
        self.export()
        with HistoryBundle(self.path) as bundle:
            self.path.write_bytes(b"changed after verification")
            report = import_history(bundle)
        self.assertEqual(report["surveys"][0]["copied"], 1)

    def test_wrong_modes_and_nested_export_are_rejected(self):
        with self.assertRaises(HistoryError):
            export_history([self.survey.slug], self.path, origin=ORIGIN)
        self.export()
        with override_settings(IS_NODE=False), HistoryBundle(self.path) as bundle:
            with self.assertRaises(HistoryError):
                import_history(bundle)

    def test_output_never_overwrites_existing_file_and_invalid_origin_is_rejected(self):
        self.path.write_bytes(b"keep")
        with self.assertRaises(HistoryError):
            self.export()
        self.assertEqual(self.path.read_bytes(), b"keep")
        with self.assertRaises(HistoryError):
            self.export(path=self.path.with_name("other.zip"), origin="https://secret@example.com/?token=secret")

    def test_preview_command_has_no_database_writes_and_import_requires_hash(self):
        self.export()
        output = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("import_survey_history", package=str(self.path), stdout=output)
        self.assertEqual(len(queries), 0)
        preview = json.loads(output.getvalue())
        with self.assertRaises(CommandError):
            call_command("import_survey_history", package=str(self.path), confirm=True, stdout=StringIO())
        call_command("import_survey_history", package=str(self.path), confirm=True,
                     expected_sha256=preview["sha256"], stdout=StringIO())
        self.assertEqual(DatasetImportBatch.objects.count(), 1)

    def test_row_limits_are_checked_without_partial_export(self):
        with patch("feedback.history_transfer.MAX_LINE", 32):
            with self.assertRaises(HistoryError):
                self.export()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.part")), [])

    def test_postgres_snapshot_is_explicitly_read_only_repeatable_read(self):
        # SQL contract test; actual PostgreSQL acceptance is separately required.
        sql = []
        def collect(execute, query, params, many, context):
            sql.append(query)
            if query.startswith("SET TRANSACTION"):
                return None
            return execute(query, params, many, context)
        with patch.object(connection, "vendor", "postgresql"), connection.execute_wrapper(collect):
            self.export()
        self.assertIn("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", sql)
        self.assertLess(sql.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"),
                        next(i for i, s in enumerate(sql) if s.startswith("SELECT")))

    def login_owner(self):
        user = get_user_model().objects.create_user(username="owner@example.com", email="owner@example.com",
            password="fixture", role=get_user_model().Role.MANAGER)
        org = Organization.objects.create(name="fixture")
        OrganizationMembership.objects.create(user=user, organization=org, role=OrganizationMembership.Role.OWNER)
        self.client.force_login(user)
        return user

    def test_ui_preview_confirmation_and_copy_are_separate(self):
        self.export()
        self.login_owner()
        url = reverse("node:history-transfer")
        response = self.client.post(url, {"package_path": str(self.path), "action": "preview"})
        self.assertContains(response, "確認建立本機歷史副本")
        self.assertFalse(DatasetImportBatch.objects.exists())
        token = response.context["form"]["confirmation"].value()
        response = self.client.post(url, {"package_path": str(self.path), "action": "import", "confirmation": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DatasetImportBatch.objects.count(), 1)

    def test_ui_import_without_preview_and_non_owner_are_rejected(self):
        self.export()
        self.login_owner()
        url = reverse("node:history-transfer")
        response = self.client.post(url, {"package_path": str(self.path), "action": "import"})
        self.assertContains(response, "未匯入")
        self.assertFalse(DatasetImportBatch.objects.exists())
        OrganizationMembership.objects.update(role=OrganizationMembership.Role.ADMIN)
        self.assertEqual(self.client.get(url).status_code, 403)


class HistoryTwoDatabaseTests(TransactionTestCase):
    def test_cloud_file_snapshot_to_independent_node_database_and_replay(self):
        """Two isolated DBs, offline package, no server or production credentials."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cloud_db = root / "cloud.sqlite3"
            package = root / "history.zip"
            env = {key: value for key, value in os.environ.items() if key not in {
                "DATABASE_URL", "NODE_DATABASE_URL", "FEEDBACK_HUB_NODE_HOME", "GOOGLE_API_KEY"}}
            env.update(DEPLOYMENT_MODE="cloud", DJANGO_SETTINGS_MODULE="config.settings_test",
                       DATABASE_URL="sqlite:///:memory:", PYTHONIOENCODING="utf-8")
            preamble = "\n".join([
                "from django.conf import settings",
                "assert settings.DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3'",
                f"settings.DATABASES['default']['NAME'] = {str(cloud_db)!r}",
                "import django; django.setup()",
                "from feedback.models import Survey, Question, FeedbackSubmission, Answer",
                "from feedback.analysis_jobs import suppress_analysis_scheduling",
                "from feedback.history_transfer import export_history",
                "import json", "",
            ])
            create = "\n".join([
                "from django.core.management import call_command",
                "call_command('migrate', verbosity=0, interactive=False)",
                "with suppress_analysis_scheduling():",
                "    s=Survey.objects.create(title='隔離雲端 fixture', slug='old', definition_version=1, published_version=1, analysis_definition_version=1)",
                "    q=Question.objects.create(survey=s, title='評語', code='q1', kind='long_text', data_type='text')",
                "    for value in ('原文一', '原文二'):",
                "        sub=FeedbackSubmission.objects.create(survey=s)",
                "        Answer.objects.create(submission=sub, question=q, value=value)", "",
            ])
            def child(code):
                result = subprocess.run([sys.executable, "-c", preamble + code], env=env,
                    capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])
                return json.loads(result.stdout.strip().splitlines()[-1])
            first = child(create + f"print(json.dumps(export_history(['old'], {str(package)!r}, origin={ORIGIN!r})))")
            self.assertEqual(first, {"surveys": 1, "submissions": 2, "answers": 2})
            self.assertFalse(Survey.objects.exists())
            with HistoryBundle(package) as bundle:
                report = import_history(bundle)
                again = import_history(bundle)
            self.assertEqual(report["surveys"][0]["copied"], 2)
            self.assertEqual(again["surveys"][0]["duplicates"], 2)
            self.assertEqual(FeedbackSubmission.objects.count(), 2)
            facts = child("print(json.dumps({'surveys':Survey.objects.count(), 'submissions':FeedbackSubmission.objects.count(), 'answers':Answer.objects.count(), 'owner':Survey.objects.get().owner_node_id}))")
            self.assertEqual(facts, {"surveys": 1, "submissions": 2, "answers": 2, "owner": None})
