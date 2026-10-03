# 雲端同步 C2：收件匣 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 指派給節點且開啟收件匣的問卷，顧客在雲端送出的回覆先進雲端收件匣（正文暫存、收據長期保存），本機節點拉回寫成一般 `FeedbackSubmission`／`Answer` 後逐筆 ACK，雲端才刪除正文。

**Architecture:** 雲端 `cloudapi` 新增收件封套、雜湊、收件服務（問卷列鎖內完成冪等、版本核對、原子額度、回覆序號、語意鎖旗標）、收件 API（領取、ACK、隔離）與管理頁；本機 `cloudsync` 新增收件同步（逐筆交易寫入、`PendingAck` 只刪逐筆成功者、回覆水位）。沿用 C1 的 `NodeDevice` 權杖、`node_api`、`CloudClient`、`run_cycle`。

**Tech Stack:** Django 6.0.8、requests 2.32.5、keyring 25.7.0、SQLite／PostgreSQL 17（併發測試）。

**Spec:** [雲端同步規格](../specs/2026-10-01-cloud-sync-design.md)（本計畫涵蓋 §1 數量定義中的已收件／待收／已同步／衝突／已放棄、§2 語意鎖旗標／問卷列鎖／填答版本核對／`response_sequence`／雜湊、§3 收件相關模型與 `CLOUD_INBOX_ENABLED`、§4 `inbox/`／`inbox/ack/`／`inbox/quarantine/`／`heartbeat/` 收件欄位、§5 收件與水位、§6、§8 內容衝突、§9、§13 對應測試）。

**前置：** C1（PR #19，分支 `feat/cloud-sync-c1`）。本計畫分支 `feat/cloud-sync-c2` 從 C1 分支開出；C1 合併後 rebase 到 `main`。

**不在本計畫：** C3 結果上傳與 `SurveyAnalysisState` 展示指標、「已分析／尚未分析／排除」計數；C4 搬移與 `migration_baseline`；加密收件匣；改善通知與顧客通知的雲端代寄。

## Global Constraints

- 收件只在 `settings.CLOUD_INBOX_ENABLED`（環境變數，預設 `False`）**且** `survey.owner_node_id` **且** `survey.inbox_since` 皆成立時啟用；否則填答維持現行 `submit_survey_payload`。判斷集中在 `cloudapi.inbox.uses_inbox(survey) -> bool`。
- 正式網站不開啟 `CLOUD_INBOX_ENABLED`、不設定 `inbox_since`（規格 §12）；`enable_survey_inbox` 指令在 `CLOUD_SYNC_PROTOTYPE_ENABLED` 或 `CLOUD_INBOX_ENABLED` 任一關閉時拒絕執行。
- 雜湊 `HASH_VERSION = 1`：標準 JSON＝`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")` 取 SHA-256 hex；**不做任何字串正規化**。
  - `answers_hash` 只對 `answers` 物件；`payload_hash` 對 `{"survey_uuid", "definition_version", "consent_follow_up", "is_complete", "voided_at", "answers"}`。
- 封套 `answers` 以題目 uuid 字串為鍵，值保留 JSON 型態：複選題 `list[str]`（依選項原順序）、整數題 `int`、小數題 `str`（`format(Decimal, "f")`）、其他 `str`；`None`、`""`、`[]` 不放入。
- 本機 `Answer.value`：`list` 以 `", ".join(...)`、其他 `str(value)`（與現行填答一致）。
- 容量（可調）：`CLOUD_INBOX_MAX_COUNT = 20000`、`CLOUD_INBOX_MAX_BYTES = 100 * 1024 * 1024`、`CLOUD_INBOX_MAX_ITEM_BYTES = 64 * 1024`；`size_bytes`＝封套標準 JSON 的位元組數。占用包含 `pending` 與 `quarantined`；只有 ACK `acked` 與「放棄」扣減。
- 處理期限：最舊 `pending` 滿 25 天 `warn`、滿 30 天 `critical`，不自動刪除。資料庫用量 400 MB 警示。
- 使用者可見訊息（逐字）：
  - 版本不符的新提交：「問卷已更新，請確認後重新送出」
  - 額度已滿或單筆過大：「目前暫停收件，請稍後再試」
  - 同 ID 但內容、提交者不同，或已放棄：「這份回覆無法重複送出，請重新填寫」
  - 放棄前確認：「放棄後這筆回覆會永久遺失，確定放棄？」
- 隔離原因代碼：`content_conflict`、`definition_unavailable`。ACK 逐筆狀態：`acked`、`already_acked`、`conflict`、`not_found`。
- `cloud_user_ref`＝`django.utils.crypto.salted_hmac("cloudapi.respondent", str(user.pk)).hexdigest()`；冪等比對提交者時直接比 `SubmissionReceipt.user_id`，不比 ref。本機 `FeedbackSubmission.user` 一律 `None`，ref 存 `respondent_ref`。
- log 不記錄答案正文；雲端管理頁不顯示答案正文。
- 跨端只用 UUID；C1 的 `upsert_definition` 仍是本機定義的唯一寫入入口。
- 測試指令：cloud `.\.venv\Scripts\python.exe manage.py test <labels> --settings=config.settings_test`；node 另設 `$env:DEPLOYMENT_MODE='node'; $env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-test"`。驗證雲端行為的測試加 `feedback.test_utils.cloud_only`。
- commit／push／開 PR 須依 AGENTS.md 取得使用者在執行當次的授權。

## Review Focus

1. **顧客回應遺失後重按送出，而問卷已更新**：依原版本重算 `payload_hash` 與既有收據相符 → 回原成功結果、不占額度、收據仍記舊版本（Task 3 `test_resend_after_definition_update_returns_original`）。
2. **本機寫入後、ACK 前崩潰**：下次同步先重送 `PendingAck`，不重複寫入、不遺漏（Task 7 `test_leftover_pending_ack_is_resent_first`、Task 9 端對端）。
3. **兩個 ACK 同時處理同一筆**：只有一個 `acked`、只扣減一次（Task 5 PostgreSQL 測試）。
4. **滿額時同時送出**：不超收；未保存時不顯示成功（Task 3 PostgreSQL 測試、`test_full_inbox_rejects_without_writing`）。
5. **隔離項目卡住水位**：放回並寫入後水位越過；放棄後經心跳越過（Task 7、Task 8）。

---

### Task 1: 收件封套與雜湊

**Files:**
- Create: `cloudapi/envelope.py`
- Test: `cloudapi/tests/test_envelope.py`

**Interfaces:**
- Produces:
  - `HASH_VERSION = 1`
  - `canonical_bytes(obj) -> bytes`、`sha256_hex(obj) -> str`
  - `answers_hash(answers: dict) -> str`
  - `payload_hash(*, survey_uuid: str, definition_version: int, consent_follow_up: bool, is_complete: bool, voided_at: str | None, answers: dict) -> str`
  - `encode_answers(survey, cleaned: dict) -> dict[str, object]`（`cleaned` 為 `SurveyFormBuilder.cleaned_data`，鍵 `question_<id>`；只取 `is_active=True` 的題目）
  - `answer_text(value) -> str`（本機 `Answer.value`）
  - `build_envelope(*, submission_uuid, survey, definition_version, response_sequence, submitted_at, consent_follow_up, respondent_ref, name, email, answers) -> dict`（`definition_history="recorded"`、`is_complete=True`、`voided_at=None`）
  - `envelope_payload_hash(envelope) -> str`

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_envelope.py
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
```

- [ ] **Step 2: 執行確認失敗** — Run: `manage.py test cloudapi.tests.test_envelope`；Expected: FAIL（`No module named 'cloudapi.envelope'`）
- [ ] **Step 3: 實作 `cloudapi/envelope.py`**（簽名與值見 Interfaces 與 Global Constraints；`build_envelope` 的 `respondent` 為 `{"cloud_user_ref", "name", "email"}`，`submitted_at` 存 ISO 8601 字串）
- [ ] **Step 4: 執行確認通過** — 同 Step 2 指令；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudapi/envelope.py cloudapi/tests/test_envelope.py`；`feat(cloudapi): inbox envelope encoding and canonical hashes`

---

### Task 2: 收件 schema 與開關

**Files:**
- Modify: `cloudapi/models.py`（`InboxSubmission`、`SubmissionReceipt`、`InboxCounter`）、`feedback/models.py`（`Survey.inbox_since`、`Survey.response_sequence`、`FeedbackSubmission.submitted_at`、`FeedbackSubmission.respondent_ref`）
- Create: migrations（`feedback/0021`、`cloudapi/0002`）、`cloudapi/management/commands/enable_survey_inbox.py`
- Modify: `config/settings.py`（`CLOUD_INBOX_ENABLED` 與容量常數）
- Test: `cloudapi/tests/test_inbox_models.py`

**Interfaces:**
- Consumes: Task 1 無；C1 `NodeDevice`
- Produces:
  - `Survey.inbox_since`（`DateTimeField(null=True, blank=True)`）、`Survey.response_sequence`（`PositiveBigIntegerField(default=0)`）
  - `FeedbackSubmission.submitted_at = DateTimeField(default=timezone.now)`（取代 `auto_now_add`）、`FeedbackSubmission.respondent_ref = CharField(max_length=128, blank=True)`
  - `InboxSubmission`：`submission_uuid`（`UUIDField(unique=True)`）、`node`（FK `NodeDevice`，`PROTECT`）、`survey`（FK `feedback.Survey`，`PROTECT`）、`envelope`（JSON）、`answers_hash`、`payload_hash`（`CharField(64)`）、`hash_version`、`payload_version`（預設 1）、`size_bytes`、`received_at`（`auto_now_add`）、`state`（`State.PENDING="pending"`／`State.QUARANTINED="quarantined"`）、`quarantine_reason`（`CharField(32, blank=True)`）；索引 `(node, state, received_at, submission_uuid)`
  - `SubmissionReceipt`：`submission_uuid`（唯一）、`node`、`survey`、`user`（FK `AUTH_USER_MODEL`，`SET_NULL`）、`submitted_at`、`consent_follow_up`、`definition_version`、`response_sequence`、`payload_hash`、`status`（`received`／`synced`／`quarantined`／`abandoned`）、`synced_at`、`last_known_improvement_status`（`CharField(20, blank=True)`）、`quarantine_reason`、`resolution`（`""`／`unresolved`／`requeued`／`abandoned`）、`resolved_at`、`resolved_by`（FK user，`SET_NULL`）
  - `InboxCounter`：`node`（`OneToOneField`）、`occupied_count`、`occupied_bytes`（`PositiveBigIntegerField(default=0)`）；`InboxCounter.for_node(node) -> InboxCounter`（`get_or_create`）
  - settings：`CLOUD_INBOX_ENABLED`、`CLOUD_INBOX_MAX_COUNT`、`CLOUD_INBOX_MAX_BYTES`、`CLOUD_INBOX_MAX_ITEM_BYTES`、`CLOUD_INBOX_WARN_DAYS = 25`、`CLOUD_INBOX_CRITICAL_DAYS = 30`、`CLOUD_DB_WARN_BYTES = 400 * 1024 * 1024`
  - 指令 `enable_survey_inbox --survey <slug>`：兩個開關都開、問卷已有 `owner_node` 才執行；設 `inbox_since=timezone.now()`（已設定則不變）並印出時間

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_inbox_models.py
from datetime import datetime, timezone as dt_timezone

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from cloudapi.models import InboxCounter, NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import FeedbackSubmission, Survey


class SchemaTests(TestCase):
    def test_submitted_at_can_be_written_and_defaults_to_now(self):
        survey = Survey.objects.create(title="S", slug="s")
        original = datetime(2026, 1, 2, 3, 4, tzinfo=dt_timezone.utc)
        kept = FeedbackSubmission.objects.create(survey=survey, submitted_at=original, respondent_ref="ref")
        kept.refresh_from_db()
        self.assertEqual((kept.submitted_at, kept.respondent_ref), (original, "ref"))
        self.assertIsNotNone(FeedbackSubmission.objects.create(survey=survey).submitted_at)

    def test_counter_is_created_lazily_per_node(self):
        node, _ = NodeDevice.issue("office")
        self.assertEqual((InboxCounter.for_node(node).occupied_count, InboxCounter.objects.count()), (0, 1))


class EnableInboxCommandTests(TestCase):
    def setUp(self):
        node, _ = NodeDevice.issue("office")
        assign_survey_to_node(Survey.objects.create(title="S", slug="s"), node)

    def test_refuses_when_either_switch_is_off(self):
        for switches in ({"CLOUD_SYNC_PROTOTYPE_ENABLED": False, "CLOUD_INBOX_ENABLED": True},
                         {"CLOUD_SYNC_PROTOTYPE_ENABLED": True, "CLOUD_INBOX_ENABLED": False}):
            with self.subTest(switches), override_settings(**switches), self.assertRaises(CommandError):
                call_command("enable_survey_inbox", survey="s")
        self.assertIsNone(Survey.objects.get(slug="s").inbox_since)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
    def test_sets_inbox_since_once(self):
        call_command("enable_survey_inbox", survey="s")
        first = Survey.objects.get(slug="s").inbox_since
        call_command("enable_survey_inbox", survey="s")
        self.assertEqual(Survey.objects.get(slug="s").inbox_since, first)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
    def test_requires_owner_node(self):
        Survey.objects.create(title="P", slug="p")
        with self.assertRaises(CommandError):
            call_command("enable_survey_inbox", survey="p")
```

- [ ] **Step 2: 執行確認失敗** — Run: `manage.py test cloudapi.tests.test_inbox_models`；Expected: FAIL（`InboxCounter` 不存在）
- [ ] **Step 3: 實作模型、設定與指令**（欄位見 Interfaces）
- [ ] **Step 4: 產生 migration** — `makemigrations feedback cloudapi --noinput`；`submitted_at` 只改 default，不需資料遷移
- [ ] **Step 5: 執行確認通過與 migration 檢查** — Step 2 指令 PASS；cloud 與 node 各跑 `makemigrations --check --dry-run --noinput` → `No changes detected`；`feedback` 全套件 PASS（`submitted_at` 行為不變）
- [ ] **Step 6: Commit** — `git add cloudapi feedback/models.py feedback/migrations config/settings.py`；`feat(cloudapi): inbox, receipt and counter models with inbox switch`

---

### Task 3: 雲端收件服務

**Files:**
- Create: `cloudapi/inbox.py`
- Test: `cloudapi/tests/test_inbox_accept.py`、`cloudapi/test_postgres.py`（加一個類別）

**Interfaces:**
- Consumes: Task 1（`encode` 已在呼叫端完成、`payload_hash`、`build_envelope`、`canonical_bytes`）、Task 2 模型
- Produces:
  - `uses_inbox(survey) -> bool`
  - 例外（皆繼承 `InboxRejected`，屬性 `user_message` 為 Global Constraints 的逐字訊息）：`DefinitionOutdated`、`InboxFull`、`ResendRejected`
  - `accept_submission(survey, *, user, submission_uuid, form_version: int, consent_follow_up: bool, answers: dict) -> AcceptResult`
  - `AcceptResult`（dataclass）：`receipt: SubmissionReceipt`、`reused: bool`

`accept_submission` 依序（全部在一個交易，第一步以 `select_for_update` 鎖 `Survey` 列）：
1. 計算 `incoming_hash = payload_hash(survey_uuid, form_version, consent_follow_up, True, None, answers)`。
2. 已有同 `submission_uuid` 的收據：同問卷、同 `user_id`、`payload_hash == incoming_hash` 且 `status != "abandoned"` → `AcceptResult(receipt, reused=True)`；否則 `ResendRejected`。**這一步在版本核對之前。**
3. `form_version != survey.definition_version` → `DefinitionOutdated`（不寫入、不占額度）。
4. 建封套（`response_sequence = survey.response_sequence + 1`），`size_bytes > CLOUD_INBOX_MAX_ITEM_BYTES` → `InboxFull`。
5. 條件式占用：
```python
taken = InboxCounter.objects.filter(
    node=survey.owner_node,
    occupied_count__lte=settings.CLOUD_INBOX_MAX_COUNT - 1,
    occupied_bytes__lte=settings.CLOUD_INBOX_MAX_BYTES - size,
).update(occupied_count=F("occupied_count") + 1, occupied_bytes=F("occupied_bytes") + size)
if not taken:
    raise InboxFull()
```
   （先 `InboxCounter.for_node(node)` 確保列存在。）
6. `survey.response_sequence += 1`（`save(update_fields=...)`）；`Question.objects.filter(survey=survey, uuid__in=answers.keys()).update(has_received_answer=True)`；建 `InboxSubmission(pending)` 與 `SubmissionReceipt(received)`。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_inbox_accept.py
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from cloudapi.definition import serialize_definition, update_survey
from cloudapi.inbox import DefinitionOutdated, InboxFull, ResendRejected, accept_submission
from cloudapi.models import InboxCounter, InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node, change_definition
from feedback.models import FeedbackSubmission, Question, Survey

User = get_user_model()


@override_settings(CLOUD_INBOX_ENABLED=True)
class AcceptTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey  # version 1
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now())
        self.survey.refresh_from_db()
        self.user = User.objects.create_user(username="c", password="x")
        self.answers = {str(self.question.uuid): "很好"}

    def accept(self, **overrides):
        kwargs = {"user": self.user, "submission_uuid": uuid.uuid4(), "form_version": 1,
                  "consent_follow_up": False, "answers": self.answers, **overrides}
        return accept_submission(self.survey, **kwargs)

    def test_accept_writes_inbox_receipt_sequence_flag_and_counter(self):
        result = self.accept()
        self.assertFalse(result.reused)
        self.assertEqual((result.receipt.status, result.receipt.response_sequence), ("received", 1))
        item = InboxSubmission.objects.get()
        self.assertEqual((item.state, item.envelope["answers"]), ("pending", self.answers))
        self.assertTrue(Question.objects.get(pk=self.question.pk).has_received_answer)
        counter = InboxCounter.objects.get(node=self.node)
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (1, item.size_bytes))
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_old_form_is_rejected_without_writing(self):
        definition = serialize_definition(self.survey)
        update_survey(definition, {"title": "新版"})
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        with self.assertRaises(DefinitionOutdated):
            self.accept()
        self.assertFalse(SubmissionReceipt.objects.exists())
        self.assertEqual(InboxCounter.for_node(self.node).occupied_count, 0)

    def test_resend_after_definition_update_returns_original(self):
        submission_uuid = uuid.uuid4()
        first = self.accept(submission_uuid=submission_uuid)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        update_survey(definition, {"title": "新版"})
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        again = self.accept(submission_uuid=submission_uuid)  # same content, form still says v1
        self.assertTrue(again.reused)
        self.assertEqual((again.receipt.pk, again.receipt.definition_version), (first.receipt.pk, 1))
        self.assertEqual(InboxCounter.for_node(self.node).occupied_count, 1)

    def test_same_uuid_with_other_content_user_or_abandoned_is_rejected(self):
        submission_uuid = uuid.uuid4()
        self.accept(submission_uuid=submission_uuid)
        other = User.objects.create_user(username="d", password="x")
        for overrides in ({"consent_follow_up": True}, {"user": other}):
            with self.subTest(overrides), self.assertRaises(ResendRejected):
                self.accept(submission_uuid=submission_uuid, **overrides)
        SubmissionReceipt.objects.update(status="abandoned")
        with self.assertRaises(ResendRejected):
            self.accept(submission_uuid=submission_uuid)

    @override_settings(CLOUD_INBOX_MAX_COUNT=1)
    def test_full_inbox_rejects_without_writing(self):
        self.accept()
        with self.assertRaises(InboxFull):
            self.accept()
        self.assertEqual((SubmissionReceipt.objects.count(), Survey.objects.get(pk=self.survey.pk).response_sequence), (1, 1))

    @override_settings(CLOUD_INBOX_MAX_ITEM_BYTES=10)
    def test_oversized_item_is_rejected(self):
        with self.assertRaises(InboxFull):
            self.accept()
```

在 `cloudapi/test_postgres.py` 加入（沿用該檔的 `SkipTest` 與執行緒寫法）：

```python
class InboxCapacityPostgreSQLTests(TransactionTestCase):
    """CLOUD_INBOX_MAX_COUNT=3, eight threads accept at once: exactly 3 receipts, counter 3, sequences 1..3."""

    def test_concurrent_accepts_never_exceed_the_limit(self):  # body described below
```

（實作此測試：`override_settings(CLOUD_INBOX_ENABLED=True, CLOUD_INBOX_MAX_COUNT=3)`、8 個執行緒各用不同使用者與 `submission_uuid` 呼叫 `accept_submission`，收集 `InboxFull`；斷言收據 3 筆、`InboxCounter.occupied_count == 3`、`sorted(response_sequence) == [1, 2, 3]`、`InboxFull` 5 次。）

再加 `SemanticLockRacePostgreSQLTests.test_first_answer_and_semantic_edit_are_serialized`：題目尚無回答；執行緒 A 呼叫 `accept_submission` 回答該題，執行緒 B 同時以 `change_definition` 把該題 `options_text` 改掉（以 `threading.Barrier(2)` 同時起跑）。斷言恰好一種結果成立：
- A 先：B 拋 `SemanticLockViolation`，題目 `options_text` 未變、`has_received_answer` 為真；或
- B 先：A 拋 `DefinitionOutdated`（表單版本已過期），沒有收據。

兩種情況都滿足不變量「`has_received_answer` 為真的題目，語意欄位等於收件當時的版本」。

- [ ] **Step 2: 執行確認失敗** — Run: `manage.py test cloudapi.tests.test_inbox_accept`；Expected: FAIL（`No module named 'cloudapi.inbox'`）
- [ ] **Step 3: 實作 `cloudapi/inbox.py`**（順序見上；`uses_inbox` 見 Global Constraints）
- [ ] **Step 4: 執行確認通過** — Run: `manage.py test cloudapi`；Expected: PASS（`test_postgres` 在 SQLite 下 skipped）
- [ ] **Step 5: Commit** — `git add cloudapi`；`feat(cloudapi): inbox intake with resend check, version check and atomic capacity`

---

### Task 4: 雲端填答頁與顧客、管理者看到的收件

**Files:**
- Modify: `feedback/views.py`（`SurveyDetailView.dispatch`／`get_context_data`／`post`、`_survey_catalog_rows`、`SurveyBuilderView.get_context_data`）、`feedback/local_service.py`（`get_customer_home_payload`）、`templates/feedback/survey_detail.html`
- Create: `cloudapi/receipts.py`
- Test: `cloudapi/tests/test_inbox_pages.py`

**Interfaces:**
- Consumes: Task 1 `encode_answers`、Task 3 `uses_inbox`／`accept_submission`／例外
- Produces:
  - `cloudapi.receipts.has_submitted(survey, user) -> bool`（`FeedbackSubmission` 或非 `abandoned` 收據）
  - `cloudapi.receipts.received_counts(survey_ids) -> dict[int, int]`（已收件：既有 `FeedbackSubmission` 與非 `abandoned` 收據依 uuid 去重）
  - `cloudapi.receipts.receipt_rows(user) -> list[dict]`：每筆與 `serialize_submission` 同鍵（`id` 為 `None`、`answers.count` 為 `None`、`display_name` 取收據使用者），另加 `"via_inbox": True`
  - 填答表單隱藏欄位 `definition_version`（只在 `uses_inbox(survey)` 時輸出）

行為：
- `SurveyDetailView.post`：`uses_inbox(survey)` 時，以 `encode_answers` 轉換後呼叫 `accept_submission`（`form_version` 取 POST 的 `definition_version`，缺少或非整數視為 `DefinitionOutdated`）；`InboxRejected` → `messages.error(user_message)` 並以使用者已填內容重新顯示表單（`DefinitionOutdated` 時表單的 `definition_version` 換成目前版本）。成功與 `reused` 都轉到 `survey-success`；感謝信只在非 `reused` 時寄出（與現行一致）。
- 「你已填答過這份問卷。」改用 `has_submitted`。
- 顧客首頁：`submission_rows`／`submissions`／`submission_count` 併入 `receipt_rows`（依 `submitted_at` 新到舊；以 uuid 去重）。
- 問卷管理列表與編排頁的回覆數改用 `received_counts`（外部資料來源的問卷維持原邏輯）。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_inbox_pages.py
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cloudapi.definition import serialize_definition, update_survey
from cloudapi.models import InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.receipts import received_counts
from cloudapi.writes import assign_survey_to_node, change_definition
from feedback.models import FeedbackSubmission, Question, Survey
from feedback.test_utils import cloud_only

User = get_user_model()


@cloud_only
@override_settings(CLOUD_INBOX_ENABLED=True)
class InboxFillPageTests(TestCase):
    def setUp(self):
        node, _ = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, node).survey
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now())
        self.customer = User.objects.create_user(username="c", password="x")
        self.client.force_login(self.customer)
        self.url = reverse("feedback:survey-detail", args=["s"])

    def submit(self, version=1, **extra):
        return self.client.post(self.url, {"definition_version": version, f"question_{self.question.id}": "好",
                                           "meta-idempotency_key": extra.pop("key", "11111111-1111-1111-1111-111111111111"),
                                           **extra}, follow=True)

    def test_form_carries_version_and_submit_goes_to_inbox(self):
        self.assertContains(self.client.get(self.url), 'name="definition_version" value="1"')
        self.submit()
        self.assertEqual(InboxSubmission.objects.count(), 1)
        self.assertFalse(FeedbackSubmission.objects.exists())
        self.assertContains(self.client.get(self.url), "你已填答過這份問卷。")

    def test_outdated_form_shows_message_and_keeps_input(self):
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        update_survey(definition, {"title": "新版"})
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        response = self.submit(version=1)
        self.assertContains(response, "問卷已更新，請確認後重新送出")
        self.assertContains(response, 'value="好"')
        self.assertFalse(SubmissionReceipt.objects.exists())

    @override_settings(CLOUD_INBOX_MAX_COUNT=0)
    def test_full_inbox_message(self):
        self.assertContains(self.submit(), "目前暫停收件，請稍後再試")

    def test_receipt_appears_in_customer_home_and_counts(self):
        self.submit()
        home = self.client.get(reverse("feedback:customer-home"))
        self.assertEqual(home.context["submission_count"], 1)
        self.assertEqual(received_counts([self.survey.pk]), {self.survey.pk: 1})

    @override_settings(CLOUD_INBOX_ENABLED=False)
    def test_switch_off_keeps_the_old_flow(self):
        self.submit()
        self.assertEqual((FeedbackSubmission.objects.count(), InboxSubmission.objects.count()), (1, 0))
```

（若 `customer-home` 的 URL 名稱不同，以 `feedback/urls.py` 中 `CustomerHomeView` 的名稱為準。）

- [ ] **Step 2: 執行確認失敗** — Run: `manage.py test cloudapi.tests.test_inbox_pages`；Expected: FAIL（`No module named 'cloudapi.receipts'`）
- [ ] **Step 3: 實作**（見上方行為；範本在 `{% csrf_token %}` 後加 `{% if uses_inbox %}<input type="hidden" name="definition_version" value="{{ inbox_definition_version }}">{% endif %}`，context 由 view 提供）
- [ ] **Step 4: 執行確認通過與回歸** — Run: `manage.py test cloudapi feedback`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudapi feedback templates/feedback`；`feat(cloudapi): route node-owned survey submissions through the inbox`

---

### Task 5: 收件 API（領取、ACK、隔離、心跳）

**Files:**
- Modify: `cloudapi/views.py`、`cloudapi/urls.py`、`cloudapi/inbox.py`（`ack_items`、`quarantine_items`、`inbox_summary`）
- Test: `cloudapi/tests/test_inbox_api.py`、`cloudapi/test_postgres.py`（加一個類別）

**Interfaces:**
- Consumes: C1 `node_api`、Task 2–3
- Produces（HTTP，皆需 bearer；原型關閉 503）：
  - `GET inbox/?limit=100`（上限 100）→ `{"items": [envelope], "has_more": bool}`；只回本節點 `pending`，依 `(received_at, submission_uuid)`
  - `POST inbox/ack/` body `{"items": [{"submission_uuid", "payload_hash"}]}`（上限 200 筆）→ `{"results": [{"submission_uuid", "status"}]}`，狀態依規格 §6
  - `POST inbox/quarantine/` body `{"items": [{"submission_uuid", "reason"}]}`（`reason` 只接受兩個代碼，否則 400）→ `{"results": [{"submission_uuid", "status": "quarantined" | "not_found"}]}`
  - `POST heartbeat/` 回應增加：`inbox`：`{"pending_count", "quarantined_count", "occupied_count", "occupied_bytes", "max_count", "max_bytes", "oldest_pending_at", "deadline_state": "ok" | "warn" | "critical"}`、`database_bytes`（PostgreSQL 為 `pg_database_size(current_database())`，其他為 `None`）、`surveys`：`[{"survey_uuid", "response_sequence", "abandoned_sequences": [int]}]`
  - `cloudapi.inbox.ack_items(node, items) -> list[dict]`、`quarantine_items(node, items) -> list[dict]`、`inbox_summary(node, now=None) -> dict`

ACK 每筆在自己的交易：
```python
deleted, _ = InboxSubmission.objects.filter(
    submission_uuid=uid, node=node, state=InboxSubmission.State.PENDING, payload_hash=hash_
).delete()
if deleted:
    # same transaction: receipt -> synced (synced_at=now), counter -= 1 / -= size (F expressions)
    status = "acked"
else:
    receipt = SubmissionReceipt.objects.filter(submission_uuid=uid, node=node).first()
    if receipt is None or receipt.status in ("quarantined", "abandoned"):
        status = "not_found"
    elif receipt.status == "synced" and receipt.payload_hash == hash_:
        status = "already_acked"
    else:
        status = "conflict"  # never downgrade a synced receipt
```
（`size_bytes` 在刪除前以 `values_list` 取得，與刪除同一交易。）隔離：`pending` → `quarantined`、收據 `status="quarantined"`、`resolution="unresolved"`、`quarantine_reason` 兩邊都寫；占用不變。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_inbox_api.py
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from cloudapi.inbox import accept_submission
from cloudapi.models import InboxCounter, InboxSubmission, NodeDevice, SubmissionReceipt
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
from feedback.test_utils import cloud_only

BASE = "/api/node/v1/"


@cloud_only
@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)
class InboxApiTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.other, self.other_token = NodeDevice.issue("other")
        survey = Survey.objects.create(title="S", slug="s")
        question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(inbox_since=timezone.now())
        user = get_user_model().objects.create_user(username="c", password="x")
        self.receipt = accept_submission(Survey.objects.get(pk=self.survey.pk), user=user, submission_uuid=uuid.uuid4(),
                                         form_version=1, consent_follow_up=False,
                                         answers={str(question.uuid): "好"}).receipt

    def call(self, method, path, body=None, token=None):
        kwargs = {"content_type": "application/json", "data": json.dumps(body)} if body is not None else {}
        return getattr(self.client, method)(BASE + path, HTTP_AUTHORIZATION=f"Bearer {token or self.token}", **kwargs)

    def ack(self, payload_hash=None, token=None):
        item = {"submission_uuid": str(self.receipt.submission_uuid), "payload_hash": payload_hash or self.receipt.payload_hash}
        return self.call("post", "inbox/ack/", {"items": [item]}, token=token).json()["results"][0]["status"]

    def test_list_is_scoped_to_the_node(self):
        self.assertEqual(len(self.call("get", "inbox/").json()["items"]), 1)
        self.assertEqual(self.call("get", "inbox/", token=self.other_token).json()["items"], [])

    def test_ack_then_repeat_then_wrong_hash(self):
        self.assertEqual(self.ack(), "acked")
        self.assertEqual(self.ack(), "already_acked")
        self.assertEqual(self.ack(payload_hash="0" * 64), "conflict")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, "synced")  # not downgraded
        counter = InboxCounter.objects.get(node=self.node)
        self.assertEqual((counter.occupied_count, counter.occupied_bytes), (0, 0))
        self.assertFalse(InboxSubmission.objects.exists())

    def test_other_node_gets_not_found(self):
        self.assertEqual(self.ack(token=self.other_token), "not_found")
        self.assertTrue(InboxSubmission.objects.exists())

    def test_quarantined_items_leave_the_batch_and_keep_occupancy(self):
        body = {"items": [{"submission_uuid": str(self.receipt.submission_uuid), "reason": "content_conflict"}]}
        self.assertEqual(self.call("post", "inbox/quarantine/", body).json()["results"][0]["status"], "quarantined")
        self.assertEqual(self.call("get", "inbox/").json()["items"], [])
        self.assertEqual(self.ack(), "not_found")
        self.assertEqual(InboxCounter.objects.get(node=self.node).occupied_count, 1)
        self.assertEqual(self.call("post", "inbox/quarantine/", {"items": [{"submission_uuid": str(uuid.uuid4()),
                                                                             "reason": "bogus"}]}).status_code, 400)

    def test_heartbeat_reports_inbox_and_survey_sequences(self):
        body = self.call("post", "heartbeat/", {}).json()
        self.assertEqual((body["inbox"]["pending_count"], body["inbox"]["deadline_state"]), (1, "ok"))
        self.assertEqual(body["surveys"], [{"survey_uuid": str(self.survey.uuid), "response_sequence": 1,
                                            "abandoned_sequences": []}])
```

在 `cloudapi/test_postgres.py` 加 `ConcurrentAckPostgreSQLTests.test_two_acks_for_one_item_ack_once`：兩執行緒同時 ACK 同一筆，結果集合為 `{"acked", "already_acked"}`、`occupied_count == 0`（不為負數）。

- [ ] **Step 2: 執行確認失敗** — Run: `manage.py test cloudapi.tests.test_inbox_api`；Expected: FAIL（404）
- [ ] **Step 3: 實作**（URL 名稱 `inbox`、`inbox-ack`、`inbox-quarantine`；`deadline_state` 依最舊 `pending` 的 `received_at` 與 25／30 天）
- [ ] **Step 4: 執行確認通過** — Run: `manage.py test cloudapi`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudapi`；CI `postgres` job 不需改標籤（同一檔案）；`feat(cloudapi): inbox fetch, per-item ack, quarantine and inbox heartbeat`

---

### Task 6: 雲端收件匣管理頁

**Files:**
- Create: `cloudapi/manage_views.py`、`cloudapi/manage_urls.py`、`templates/cloudapi/inbox_manage.html`
- Modify: `cloudapi/inbox.py`（`requeue(receipt, by)`、`abandon(receipt, by)`）、`config/urls.py`（cloud 模式掛 `dashboard/inbox/`）、`feedback/views.py`（`DASHBOARD` 導覽在 `CLOUD_INBOX_ENABLED` 時加「收件匣」）
- Test: `cloudapi/tests/test_inbox_manage.py`

**Interfaces:**
- Consumes: Task 2、Task 5 `inbox_summary`
- Produces:
  - URL `cloudapi-manage:inbox`（`/dashboard/inbox/`，`ManagerRequiredMixin`）
  - `requeue(receipt, by) -> None`：收件匣 `state="pending"`、收據 `status="received"`、`resolution="requeued"`、`resolved_at`／`resolved_by`；占用不變
  - `abandon(receipt, by) -> None`：刪正文、計數扣減一次（F 運算）、收據 `status="abandoned"`、`resolution="abandoned"`；只處理 `quarantined`，其他狀態拋 `ValueError`
  - 頁面：每節點一段，顯示待收、衝突、已同步、已放棄筆數與占用／上限、最舊待收時間與警示（25 天、30 天）、資料庫用量（400 MB 警示）；隔離清單欄位只有 ID、問卷、原因、時間；「放棄」按鈕帶 `onsubmit` 確認文字（Global Constraints）

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_inbox_manage.py — setUp 同 Task 5 InboxApiTests（再加 manager 登入、把該筆標為 quarantined）
class InboxManageTests(TestCase):
    def test_page_lists_quarantine_without_answer_text(self):
        page = self.client.get(reverse("cloudapi-manage:inbox"))
        self.assertContains(page, str(self.receipt.submission_uuid))
        self.assertContains(page, "content_conflict")
        self.assertNotContains(page, "好")  # the answer body

    def test_requeue_keeps_occupancy(self):
        self.client.post(reverse("cloudapi-manage:inbox"), {"action": "requeue", "submission_uuid": self.receipt.submission_uuid})
        self.assertEqual(InboxSubmission.objects.get().state, "pending")
        self.assertEqual(InboxCounter.objects.get(node=self.node).occupied_count, 1)

    def test_abandon_frees_once_and_blocks_resend(self):
        self.client.post(reverse("cloudapi-manage:inbox"), {"action": "abandon", "submission_uuid": self.receipt.submission_uuid})
        self.client.post(reverse("cloudapi-manage:inbox"), {"action": "abandon", "submission_uuid": self.receipt.submission_uuid})
        self.assertEqual(InboxCounter.objects.get(node=self.node).occupied_count, 0)
        self.receipt.refresh_from_db()
        self.assertEqual((self.receipt.status, self.receipt.resolution), ("abandoned", "abandoned"))
        with self.assertRaises(ResendRejected):
            accept_submission(Survey.objects.get(pk=self.survey.pk), user=self.customer,
                              submission_uuid=self.receipt.submission_uuid, form_version=1,
                              consent_follow_up=False, answers=self.answers)

    def test_customer_is_forbidden(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("cloudapi-manage:inbox"))
        self.assertIn(response.status_code, (302, 403))
```

（`setUp` 另存 `self.customer`、`self.answers` 供重送測試使用；以 `quarantine_items(self.node, [{"submission_uuid": ..., "reason": "content_conflict"}])` 建立隔離項目；類別加 `@cloud_only` 與 `override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True, CLOUD_INBOX_ENABLED=True)`。）

- [ ] **Step 2: 執行確認失敗** — Run: `manage.py test cloudapi.tests.test_inbox_manage`；Expected: FAIL（`NoReverseMatch`）
- [ ] **Step 3: 實作**（樣式沿用 `feedback/dashboard_base.html` 的 `manager-panel`）
- [ ] **Step 4: 執行確認通過** — Run: `manage.py test cloudapi feedback`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudapi config/urls.py feedback/views.py templates/cloudapi`；`feat(cloudapi): manager inbox page with requeue and abandon`

---

### Task 7: 本機收件

**Files:**
- Modify: `cloudsync/models.py`（`SyncedSubmissionSource`、`SurveySyncState`、`PendingAck`）
- Create: `cloudsync/inbox.py`、migration `cloudsync/0002`
- Test: `cloudsync/tests/test_inbox.py`

**Interfaces:**
- Consumes: Task 1 `answer_text`、`envelope_payload_hash`；Task 5 HTTP 契約；C1 `upsert_definition`、`CloudClient`
- Produces:
  - `SyncedSubmissionSource`：`submission`（`OneToOneField(FeedbackSubmission, CASCADE, related_name="synced_source")`）、`definition_version`、`definition_history`、`response_sequence`、`answers_hash`、`payload_hash`、`hash_version`、`original_answers`（JSON）
  - `SurveySyncState`：`survey`（`OneToOneField`）、`synced_through_sequence`（預設 0）、`abandoned_sequences`（JSON list，只存大於水位者）；`SurveySyncState.advance(survey) -> int`
  - `PendingAck`：`submission_uuid`（唯一）、`payload_hash`、`created_at`、`last_status`（`""`／`conflict`／`not_found`）
  - `intake(envelope, client) -> str`：回 `"written"`、`"duplicate"` 或隔離原因代碼
  - `sync_inbox(client) -> InboxResult`（dataclass：`written`、`duplicates`、`quarantined`、`acked`、`problems`）

`intake`（每筆一個交易，`suppress_analysis_scheduling()` 內）：
1. 本機已有 `FeedbackSubmission(idempotency_key=uuid)`：其 `synced_source.payload_hash` 與 `envelope_payload_hash(envelope)` 相同 → 補 `PendingAck`，回 `"duplicate"`；不同 → 回 `"content_conflict"`（不寫入、不 ACK）。
2. 本機問卷版本低於封套 `definition_version`：`client.get(f"surveys/{survey_uuid}/revisions/{v}/")` 後 `upsert_definition`；取不到（`CloudError`）或封套任一題 uuid 在本機不存在 → `"definition_unavailable"`。
3. 建 `FeedbackSubmission`（`idempotency_key`、原始 `submitted_at`、`consent_follow_up`、`is_complete`、`voided_at`、`respondent_ref`、`respondent_name`、`respondent_email`；`user=None`）、每題 `Answer(value=answer_text(v))`、`SyncedSubmissionSource`、`PendingAck`，再 `SurveySyncState.advance(survey)`。
4. 交易提交後，對受影響問卷各呼叫一次 `schedule_survey_analysis(survey.pk, change="input")`（在 `sync_inbox` 結尾）。

水位：
```python
def advance(cls, survey):
    state, _ = cls.objects.select_for_update().get_or_create(survey=survey)
    done = set(SyncedSubmissionSource.objects.filter(
        submission__survey=survey, response_sequence__gt=state.synced_through_sequence
    ).values_list("response_sequence", flat=True)) | set(state.abandoned_sequences)
    n = state.synced_through_sequence
    while n + 1 in done:
        n += 1
    state.synced_through_sequence = n
    state.abandoned_sequences = sorted(s for s in state.abandoned_sequences if s > n)
    state.save()
    return n
```

`sync_inbox`：先送出殘留的 `PendingAck`；再以 `GET inbox/?limit=100` 逐頁領取，逐筆 `intake`；隔離原因以 `POST inbox/quarantine/` 回報；本頁寫入與重複者送 `POST inbox/ack/`。**只刪除回傳 `acked`／`already_acked` 的 `PendingAck`**；`conflict`／`not_found` 保留並寫 `last_status`。`has_more` 為假時結束；一頁全部都被隔離時也結束（避免同一頁無限重領）。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_inbox.py
# FakeInboxClient(pages, ack_status="acked", revisions=None)：get("inbox/") 依序回傳 pages；
# get("surveys/<uuid>/revisions/<v>/") 從 revisions 取或拋 CloudError(CLIENT)；post 記錄 path 與 body，
# "inbox/ack/" 依 ack_status 對每筆回覆，"inbox/quarantine/" 回 quarantined。
# envelope(seq, uuid_text, answers, version=1, consent=False) 依 Global Constraints 建立，payload 雜湊與雲端一致。

class IntakeTests(TestCase):
    # setUp: upsert_definition(definition(1)) 建立問卷 S（題目 Q1 short_text、Q2 multiple_choice 選項 甲/乙）

    def test_written_submission_keeps_original_fields_and_raw_answers(self):
        env = envelope(1, U1, {Q1: "好", Q2: ["甲", "乙"]})
        self.assertEqual(intake(env, FakeInboxClient([])), "written")
        sub = FeedbackSubmission.objects.get(idempotency_key=U1)
        self.assertEqual((sub.submitted_at.isoformat(), sub.user, sub.respondent_ref), (env["submitted_at"], None, "ref-1"))
        self.assertEqual(Answer.objects.get(submission=sub, question__uuid=Q2).value, "甲, 乙")
        self.assertEqual(sub.synced_source.original_answers[Q2], ["甲", "乙"])
        self.assertTrue(PendingAck.objects.filter(submission_uuid=U1).exists())
        self.assertEqual(SurveySyncState.objects.get().synced_through_sequence, 1)

    def test_duplicate_and_conflict(self):
        intake(envelope(1, U1, {Q1: "好"}), FakeInboxClient([]))
        PendingAck.objects.all().delete()
        self.assertEqual(intake(envelope(1, U1, {Q1: "好"}), FakeInboxClient([])), "duplicate")
        self.assertTrue(PendingAck.objects.exists())
        self.assertEqual(intake(envelope(1, U1, {Q1: "差"}), FakeInboxClient([])), "content_conflict")
        self.assertEqual(Answer.objects.get().value, "好")

    def test_missing_revision_is_fetched_or_quarantined(self):
        newer = definition(2, questions=[question(Q1), question(Q2, kind="multiple_choice"), question(Q3)])
        self.assertEqual(intake(envelope(1, U1, {Q3: "x"}, version=2), FakeInboxClient([], revisions={2: newer})), "written")
        self.assertEqual(intake(envelope(2, U2, {Q1: "x"}, version=3), FakeInboxClient([])), "definition_unavailable")
        self.assertFalse(FeedbackSubmission.objects.filter(idempotency_key=U2).exists())

    def test_watermark_waits_for_gaps_and_abandoned(self):
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([]))
        self.assertEqual(SurveySyncState.objects.get().synced_through_sequence, 1)
        state = SurveySyncState.objects.get(); state.abandoned_sequences = [2]; state.save()
        self.assertEqual(SurveySyncState.advance(state.survey), 3)


class SyncInboxTests(TestCase):
    def test_only_acked_items_drop_their_pending_ack(self):
        client = FakeInboxClient([{"items": [envelope(1, U1, {Q1: "a"})], "has_more": False}], ack_status="conflict")
        sync_inbox(client)
        self.assertEqual(PendingAck.objects.get().last_status, "conflict")

    def test_leftover_pending_ack_is_resent_first(self):
        PendingAck.objects.create(submission_uuid=U9, payload_hash="h" * 64)
        client = FakeInboxClient([{"items": [], "has_more": False}])
        sync_inbox(client)
        self.assertEqual(client.posts[0][0], "inbox/ack/")
        self.assertFalse(PendingAck.objects.exists())

    def test_quarantine_reason_is_reported_and_not_acked(self):
        client = FakeInboxClient([{"items": [envelope(1, U1, {Q1: "a"}, version=9)], "has_more": False}])
        result = sync_inbox(client)
        self.assertEqual(result.quarantined, 1)
        self.assertEqual([p for p, _ in client.posts], ["inbox/quarantine/"])

    def test_new_submissions_schedule_local_analysis_once(self):
        page = {"items": [envelope(1, U1, {Q1: "a"}), envelope(2, U2, {Q1: "b"})], "has_more": False}
        sync_inbox(FakeInboxClient([page]))
        survey = Survey.objects.get()
        self.assertEqual(AnalysisJob.objects.filter(survey=survey, status="pending").count(), 1)
```

（`definition()`／`question()` 輔助函式沿用 `cloudsync/tests/test_definitions.py` 的格式，`question()` 加 `kind` 參數；複選題 `data_type="nominal"`、`options_text="甲\n乙"`。）

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_inbox`；Expected: FAIL（`No module named 'cloudsync.inbox'`）
- [ ] **Step 3: 實作模型、migration 與 `cloudsync/inbox.py`**
- [ ] **Step 4: 執行確認通過** — Run（node）：`manage.py test cloudsync`；Expected: PASS；node 模式 `makemigrations --check` 無變更
- [ ] **Step 5: Commit** — `git add cloudsync`；`feat(cloudsync): local inbox intake with pending acks, quarantine and response watermark`

---

### Task 8: 同步週期、心跳與本機總覽

**Files:**
- Modify: `cloudsync/models.py`（`CloudLink.inbox_status` JSON，預設 `{}`）、migration `cloudsync/0003`、`cloudsync/runner.py`、`node/status.py`、`node/views.py`、`templates/cloudsync/connection.html`
- Test: `cloudsync/tests/test_runner.py`（加測試）、`node/tests/test_status.py`

**Interfaces:**
- Consumes: Task 5 心跳回應、Task 7 `sync_inbox`／`SurveySyncState`
- Produces:
  - `run_cycle` 依序：`sync_definitions` → `sync_inbox` → `heartbeat`；心跳回應以 `update_if_current` 存入 `CloudLink.inbox_status`，並把每份問卷的 `abandoned_sequences` 併入本機 `SurveySyncState` 後呼叫 `advance`
  - `node.status.inbox_status(link=None) -> StatusItem`（key `"inbox"`）：未連結 `off`；`deadline_state` 為 `warn`／`critical`、有隔離項目、或有 `PendingAck.last_status` 非空 → `warn`；否則 `ok`，摘要「待收 N 筆」
  - `PENDING_MESSAGES["inbox"] = "收件匣需要處理：請到「雲端連線」查看待收期限與衝突項目。"`
  - 雲端連線頁顯示待收、衝突、占用／上限、最舊待收時間、ACK 警示筆數

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_runner.py 加入
    def test_cycle_runs_inbox_and_stores_heartbeat(self):
        heartbeat = {"node_uuid": "...", "inbox": {"pending_count": 2, "deadline_state": "ok"}, "surveys": []}
        client = FakeClient(heartbeat=heartbeat)
        with patch("cloudsync.runner.client_for_link", return_value=client), \
             patch("cloudsync.runner.sync_definitions", return_value=0), \
             patch("cloudsync.runner.sync_inbox") as sync_inbox:
            self.assertEqual(run_cycle(), "ok")
        sync_inbox.assert_called_once()
        self.assertEqual(CloudLink.load().inbox_status["pending_count"], 2)

    def test_abandoned_sequences_advance_the_watermark(self):
        survey = make_synced_survey(sequences=[1, 3])  # helper: upsert_definition + intake of two envelopes
        heartbeat = {"surveys": [{"survey_uuid": str(survey.uuid), "response_sequence": 3, "abandoned_sequences": [2]}]}
        with patch("cloudsync.runner.client_for_link", return_value=FakeClient(heartbeat=heartbeat)), \
             patch("cloudsync.runner.sync_definitions", return_value=0), patch("cloudsync.runner.sync_inbox"):
            run_cycle()
        self.assertEqual(SurveySyncState.objects.get(survey=survey).synced_through_sequence, 3)
```

（`FakeClient.post("heartbeat/")` 改為回傳建構時給的 `heartbeat`，預設 `{}`；既有測試不受影響。）

```python
# node/tests/test_status.py 加入
    def test_inbox_status_warns_on_deadline_and_ack_problems(self):
        linked = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999",
                           inbox_status={"pending_count": 4, "deadline_state": "warn"})
        self.assertEqual(inbox_status(linked).state, "warn")
        ok = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999",
                       inbox_status={"pending_count": 4, "deadline_state": "ok"})
        self.assertEqual((inbox_status(ok).state, inbox_status(ok).summary), ("ok", "待收 4 筆"))
```

（`PendingAck` 判斷需資料庫；把 `test_inbox_status_*` 放在 `TestCase` 類別。）

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_runner node.tests.test_status`；Expected: FAIL
- [ ] **Step 3: 實作**（總覽 `items` 在 `cloud_status()` 後加 `inbox_status()`）
- [ ] **Step 4: 執行確認通過** — Run（node）：`manage.py test cloudsync node`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudsync node templates/cloudsync`；`feat(cloudsync): inbox in the sync cycle, heartbeat status and overview warnings`

---

### Task 9: 端對端與文件

**Files:**
- Modify: `cloudsync/testing.py`（`CloudServer(..., inbox=True)` 時子程序加 `CLOUD_INBOX_ENABLED=True`）
- Create: `cloudsync/tests/test_e2e_inbox.py`
- Modify: `docs/architecture.md`、`docs/next-actions.md`、`README.md`

**Interfaces:**
- Consumes: 全部前述任務；C1 `CloudServer`
- Produces: 無新介面

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_e2e_inbox.py（類別結構同 C1 test_e2e_definitions：共用一個 CloudServer(inbox=True)）
SEED = """
from django.contrib.auth import get_user_model
from django.utils import timezone
from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
survey = Survey.objects.create(title="門市問卷", slug="{slug}")
Question.objects.create(survey=survey, title="感想", kind="long_text", data_type="text", order=1)
survey = assign_survey_to_node(survey, NodeDevice.objects.get(name="e2e")).survey
Survey.objects.filter(pk=survey.pk).update(inbox_since=timezone.now())
get_user_model().objects.get_or_create(username="{slug}-customer")
print(survey.uuid)
"""

SUBMIT = """
import uuid
from django.contrib.auth import get_user_model
from cloudapi.inbox import accept_submission
from feedback.models import Survey
s = Survey.objects.get(uuid='{survey}')
q = s.questions.get(title='感想')
r = accept_submission(s, user=get_user_model().objects.get(username='{slug}-customer'), submission_uuid=uuid.UUID('{sub}'),
                      form_version=s.definition_version, consent_follow_up=True, answers={{str(q.uuid): '{text}'}})
print(r.receipt.response_sequence)
"""

class InboxEndToEndTests(TestCase):
    def test_submit_sync_ack_and_counts(self):
        # submit on cloud -> run_cycle(force=True) -> local FeedbackSubmission with original submitted_at and
        # consent_follow_up True, Answer "很好"; cloud: receipt synced, inbox empty, counter 0;
        # local SurveySyncState.synced_through_sequence == 1; a local AnalysisJob is pending for the survey

    def test_crash_after_write_before_ack_does_not_duplicate(self):
        # patch CloudClient.post so the first "inbox/ack/" raises CloudError(TRANSIENT) -> run_cycle returns "transient";
        # local row exists with PendingAck; cloud item still pending.
        # next run_cycle(force=True) -> PendingAck resent first, acked; still exactly one local FeedbackSubmission

    def test_conflicting_content_is_quarantined_on_both_sides(self):
        # write a local FeedbackSubmission+SyncedSubmissionSource with the same uuid but another payload_hash,
        # submit on cloud, run_cycle -> cloud receipt "quarantined" with reason content_conflict; local row unchanged
```

（三個測試的斷言即註解所列；雲端狀態以 `self.cloud.shell(...)` 讀取。）

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_e2e_inbox`；Expected: FAIL（`CloudServer` 不接受 `inbox`）
- [ ] **Step 3: 實作 `CloudServer(inbox=False)` 參數**
- [ ] **Step 4: 執行確認通過與兩模式全套件**
  - node：`manage.py test cloudsync.tests.test_e2e_inbox` PASS
  - cloud：`manage.py test feedback accounts config cloudapi` PASS
  - node：`manage.py test feedback accounts config node organizations cloudapi cloudsync` PASS
- [ ] **Step 5: 文件（只寫現況）**
  - `docs/architecture.md` 的同步段落補：收件匣（`CLOUD_INBOX_ENABLED`、`inbox_since`、收據、逐筆 ACK、隔離、容量與期限），仍為明文原型、正式網站不開啟。
  - `docs/next-actions.md`：雲端同步改為「C1、C2 已完成；接續 C3 結果、C4 搬移」。
  - `README.md`：雲端以 `enable_survey_inbox --survey <slug>` 開啟單一問卷收件匣（需兩個開關），只用於隔離環境。
- [ ] **Step 6: Commit** — `git add cloudsync docs README.md`；`test(cloudsync): end-to-end inbox sync, lost ack recovery and quarantine`
