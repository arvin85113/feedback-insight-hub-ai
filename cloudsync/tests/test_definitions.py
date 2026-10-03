from django.test import TestCase, override_settings

from cloudapi.models import SurveyDefinitionRevision
from cloudsync.client import GONE, CloudError
from cloudsync.definitions import sync_definitions, upsert_definition
from cloudsync.models import CloudLink
from feedback.models import Answer, FeedbackSubmission, Question, Survey

SURVEY_UUID = "66666666-6666-6666-6666-666666666666"
Q1 = "77777777-7777-7777-7777-777777777777"


def definition(version, *, title="門市", questions=None, category="門市", slug="store", archived_at=None):
    return {"survey_uuid": SURVEY_UUID, "version": version, "title": title, "slug": slug, "description": "",
            "is_active": True, "analysis_enabled": True, "thank_you_email_enabled": True,
            "improvement_tracking_enabled": True, "category": category, "archived_at": archived_at,
            "questions": questions if questions is not None else [question(Q1)]}


def question(uuid_text, *, is_active=True, title="感想"):
    return {"uuid": uuid_text, "code": "q1", "title": title, "help_text": "", "kind": "long_text", "data_type": "text",
            "options_text": "", "is_required": True, "enable_keyword_tracking": False, "is_active": is_active, "order": 1}


class UpsertTests(TestCase):
    def test_creates_then_ignores_older_or_same_versions(self):
        survey, changed = upsert_definition(definition(2))
        self.assertTrue(changed)
        self.assertEqual((str(survey.uuid), survey.definition_version, survey.category.name), (SURVEY_UUID, 2, "門市"))
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=2).exists())
        _, changed = upsert_definition(definition(2, title="不會套用"))
        self.assertFalse(changed)
        _, changed = upsert_definition(definition(1, title="更舊"))
        self.assertFalse(changed)
        self.assertEqual(Survey.objects.get().title, "門市")

    def test_deactivated_question_keeps_local_answers(self):
        survey, _ = upsert_definition(definition(1))
        local_question = Question.objects.get(uuid=Q1)
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=survey), question=local_question, value="好")
        upsert_definition(definition(2, questions=[question(Q1, is_active=False)]))
        local_question.refresh_from_db()
        self.assertFalse(local_question.is_active)
        self.assertEqual(Answer.objects.count(), 1)

    def test_category_cleared_by_cloud(self):
        upsert_definition(definition(1))
        survey, _ = upsert_definition(definition(2, category=None))
        self.assertIsNone(survey.category)

    def test_failed_apply_leaves_no_half_written_definition(self):
        upsert_definition(definition(1))
        broken = definition(2, title="新名稱", questions=[question(Q1), question(Q1.replace("7", "8"), title="")])
        with self.assertRaises(Exception):
            upsert_definition(broken)  # second question fails validation after the survey fields are known
        survey = Survey.objects.get()
        self.assertEqual((survey.title, survey.definition_version), ("門市", 1))
        upsert_definition(definition(2, title="新名稱"))  # the same version applies cleanly later
        self.assertEqual(Survey.objects.get().definition_version, 2)

    def test_local_survey_with_the_same_slug_is_renamed(self):
        local = Survey.objects.create(title="本機舊問卷", slug="store")
        synced, _ = upsert_definition(definition(1))
        local.refresh_from_db()
        self.assertEqual(synced.slug, "store")
        self.assertEqual(local.slug, f"store-local-{local.pk}")


class FakeClient:
    def __init__(self, snapshot, pages, gone_once=False):
        self.snapshot, self.pages, self.gone_once, self.calls = snapshot, list(pages), gone_once, []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "surveys/snapshot/":
            return self.snapshot
        if self.gone_once:
            self.gone_once = False
            raise CloudError(GONE)
        return self.pages.pop(0)


class SyncDefinitionsTests(TestCase):
    def test_first_sync_uses_snapshot_and_stores_cursor(self):
        link = CloudLink.relink("https://c", "dddddddd-dddd-dddd-dddd-dddddddddddd")
        client = FakeClient({"cursor": "n:5", "surveys": [definition(1)]}, [])
        self.assertEqual(sync_definitions(client, link), 1)
        self.assertEqual(CloudLink.load().cursor, "n:5")

    def test_pages_advance_cursor_only_after_commit(self):
        link = CloudLink.relink("https://c", "dddddddd-dddd-dddd-dddd-dddddddddddd")
        CloudLink.update_if_current(link.generation, cursor="n:5")
        link = CloudLink.load()
        pages = [
            {"changes": [{"seq": 6, "definition": definition(1)}], "next_cursor": "n:6", "has_more": True},
            {"changes": [{"seq": 7, "definition": {"broken": True}}], "next_cursor": "n:7", "has_more": False},
        ]
        with self.assertRaises(Exception):
            sync_definitions(FakeClient({}, pages), link)
        self.assertEqual(CloudLink.load().cursor, "n:6")  # second page rolled back, cursor kept
        self.assertEqual(Survey.objects.get().definition_version, 1)

    def test_relink_during_sync_stops_the_old_run(self):
        link = CloudLink.relink("https://old", "dddddddd-dddd-dddd-dddd-dddddddddddd")
        CloudLink.update_if_current(link.generation, cursor="n:5")
        link = CloudLink.load()

        class RelinkingClient(FakeClient):
            def get(self, path, params=None):
                CloudLink.relink("https://new", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
                return {"changes": [{"seq": 6, "definition": definition(1)}], "next_cursor": "n:6", "has_more": False}

        from cloudsync.models import StaleLink

        with self.assertRaises(StaleLink):
            sync_definitions(RelinkingClient({}, []), link)
        fresh = CloudLink.load()
        self.assertEqual((fresh.api_url, fresh.cursor), ("https://new", ""))
        self.assertFalse(Survey.objects.exists())  # the old run's page rolled back

    def test_gone_cursor_falls_back_to_snapshot(self):
        link = CloudLink.relink("https://c", "dddddddd-dddd-dddd-dddd-dddddddddddd")
        CloudLink.update_if_current(link.generation, cursor="n:1")
        link = CloudLink.load()
        client = FakeClient({"cursor": "n:9", "surveys": [definition(3)]}, [], gone_once=True)
        sync_definitions(client, link)
        self.assertEqual(CloudLink.load().cursor, "n:9")
        self.assertEqual(Survey.objects.get().definition_version, 3)
