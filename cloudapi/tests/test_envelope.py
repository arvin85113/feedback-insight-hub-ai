from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from cloudapi.envelope import answer_text, answers_hash, canonical_bytes, encode_answers, payload_hash
from feedback.models import Question, Survey

BASE = {"survey_uuid": "s", "definition_version": 1, "consent_follow_up": False, "is_complete": True,
        "voided_at": None}


class HashTests(SimpleTestCase):
    def test_key_order_does_not_matter(self):
        self.assertEqual(answers_hash({"a": "1", "b": ["x", "y"]}), answers_hash({"b": ["x", "y"], "a": "1"}))

    def test_whitespace_and_unicode_form_are_significant(self):
        self.assertNotEqual(answers_hash({"a": "好 "}), answers_hash({"a": "好"}))
        self.assertNotEqual(answers_hash({"a": "é"}), answers_hash({"a": "é"}))

    def test_consent_changes_payload_but_not_answers_hash(self):
        answers = {"a": "1"}
        self.assertNotEqual(payload_hash(**BASE, answers=answers),
                            payload_hash(**{**BASE, "consent_follow_up": True}, answers=answers))

    def test_canonical_form_is_compact_sorted_utf8(self):
        self.assertEqual(canonical_bytes({"b": 1, "a": "好"}), '{"a":"好","b":1}'.encode("utf-8"))


class EncodeTests(TestCase):
    def test_types_follow_question_kind_and_empty_values_are_dropped(self):
        survey = Survey.objects.create(title="S", slug="s")
        multi = Question.objects.create(survey=survey, title="M", kind="multiple_choice", data_type="nominal",
                                        options_text="甲\n乙", order=1)
        number = Question.objects.create(survey=survey, title="N", kind="integer", data_type="discrete", order=2)
        price = Question.objects.create(survey=survey, title="P", kind="decimal", data_type="continuous", order=3)
        note = Question.objects.create(survey=survey, title="T", kind="short_text", data_type="text", order=4)
        encoded = encode_answers(survey, {f"question_{multi.id}": ["甲", "乙"], f"question_{number.id}": 3,
                                          f"question_{price.id}": Decimal("3.50"), f"question_{note.id}": ""})
        self.assertEqual(encoded, {str(multi.uuid): ["甲", "乙"], str(number.uuid): 3, str(price.uuid): "3.50"})
        self.assertEqual([answer_text(["甲", "乙"]), answer_text(3), answer_text("3.50")], ["甲, 乙", "3", "3.50"])
