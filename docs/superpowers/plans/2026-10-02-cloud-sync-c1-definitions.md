# 雲端同步 C1：識別碼、版本與問卷定義同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 指派給節點的問卷以雲端為正本、帶版本號；雲端網站與本機主控台都能經條件式寫入修改它，本機以提交安全的變更序列同步定義，離線時唯讀。

**Architecture:** 新增兩個 app。`cloudapi`（兩種模式都安裝，因為 `Survey.owner_node` 與 revision 需要它的模型；API 網址只在 cloud 模式掛載）負責定義的序列化、條件式寫入、變更序列與節點 API。`cloudsync`（只在 node 模式）負責雲端連線、HTTP client、定義同步與本機問卷頁改經 API 寫入。兩端用同一組「定義 dict」：編輯先改 dict，再由 `apply_definition()` 寫回模型。

**Tech Stack:** Django 6.0.8、requests 2.32.5、keyring 25.7.0、SQLite／PostgreSQL 17（併發測試）。

**Spec:** [雲端同步規格](../specs/2026-10-01-cloud-sync-design.md)（本計畫涵蓋 §1 問卷定義與分類、§2 識別碼／版本／語意鎖／不硬刪、§3 `NodeDevice`／`owner_node`／`ChangeClock`／`SurveyChange`、§4 問卷相關端點與 heartbeat 基本版、§5 `CloudLink`／定義同步／本機問卷頁、§8 錯誤分類、§13 對應測試）

**計畫切分**（規格涵蓋四個可各自測試的子系統，分四份計畫，依序執行）：
- **C1（本文件）**：識別碼、版本、語意鎖、變更序列、問卷 API、本機定義同步與本機問卷頁、端對端測試骨架。
- **C2**：收件匣——收件封套、填答版本核對、收據、`InboxCounter`、ACK／隔離、本機收件與 `SyncedSubmissionSource`、水位、`CLOUD_INBOX_ENABLED`。
- **C3**：結果——輸入擷取三段式、`ResultUpload`、結果 API、`SurveyAnalysisState` 展示指標原子切換、新鮮度與數量顯示。
- **C4**：搬移——搬移基準版本、分批轉入、對帳報告、清理。

## Global Constraints

- `cloudapi` 在兩種模式都在 `INSTALLED_APPS`；`/api/node/v1/` 只在 cloud 模式掛載。`cloudsync` 只在 node 模式安裝。
- **原型閘門**：新增設定 `CLOUD_SYNC_PROTOTYPE_ENABLED`（環境變數，預設 `False`）。關閉時節點 API 一律回 503 `{"error": "prototype_disabled"}`，`assign_survey_node` 指令拒絕執行。正式網站不得開啟（規格 §12：本階段不設定任何問卷的 `owner_node`）。
- 版本號、revision、語意鎖、不硬刪只套用在 `owner_node` 已設定的問卷（node 模式下的所有問卷都是同步問卷）；未指派節點的雲端問卷維持現行行為。
- 語意欄位固定為 `kind`、`data_type`、`options_text`。
- 跨端只用 UUID 對應，不使用數字主鍵。
- API 回傳的問卷定義一律取自不可變的 `SurveyDefinitionRevision`，不從即時資料重新組裝（避免混合版本）。
- 本機寫入定義只經 `cloudsync.definitions.upsert_definition`：版本核對、問卷與題目寫入、revision 保存在同一個鎖定交易。
- `CloudLink.generation` 在每次連結／中斷時遞增；同步中的工作只透過 `update_if_current` 寫入，世代不符即停止。
- 裝置權杖只經 HTTPS 傳送；明文 HTTP 只在 `CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True` 且主機為 loopback 時允許（僅隔離測試）；HTTP client 不跟隨重新導向。
- 權杖只由管理指令（`create_node_device`、`rotate_node_token`）在終端顯示一次；不得經 Django messages、log 或資料庫明文傳遞。
- 定義驗證在任何資料庫寫入之前完成：完整結構、型別、長度、合法題型、題型與資料型態相容、選擇題需有選項、題目 UUID 不重複；不合格回 400 且整筆不落庫。
- 使用者可見訊息（逐字）：
  - 版本不一致：「版本不一致，請重新載入」
  - 語意鎖：「此題已有回覆，請新增題目取代並停用舊題」
  - 離線：「離線中，問卷唯讀」
  - 未連結：「尚未連結雲端，問卷唯讀」
  - 權杖失效：「雲端連線已撤銷，請重新連結」
- 錯誤分類：401 權杖失效（停止同步）、404 視為格式／權限錯誤、409 版本不一致、410 游標失效（改走 snapshot）、422 語意鎖、429／5xx／連線失敗為暫時性（漸進重試 1、2、4… 分鐘，上限 30 分鐘；`Retry-After` 至少等其指定秒數）。
- `SurveyChange` 保留 90 天。
- 同步週期 5 分鐘；主控台提供「立即同步」。
- 測試指令：cloud `.\.venv\Scripts\python.exe manage.py test <labels> --settings=config.settings_test`；node 另設 `$env:DEPLOYMENT_MODE='node'; $env:FEEDBACK_HUB_NODE_HOME="$env:TEMP\fih-node-test"`。
- commit／push／開 PR 須依 AGENTS.md 取得使用者在執行當次的授權；分支 `feat/cloud-sync-c1`。

## Review Focus

1. **雲端網站與本機同時改同一份問卷**：後送出者看到「版本不一致，請重新載入」，不覆蓋（Task 3 `ChangeDefinitionTests`、Task 9 端對端）。
2. **雲端在指派節點的問卷「刪除」一題尚未有回覆的題目**：仍只停用、不硬刪，以免連帶刪掉未搬移的舊答案（Task 5）。
3. **同步到一半斷線**：游標不前進，下次重跑以版本冪等套用，不重複、不遺漏（Task 7）。
4. **本機在連結雲端前已有同 slug 的本機問卷**：同步進來的雲端問卷取得原 slug，舊的本機問卷改名為 `<slug>-local-<pk>`，不中斷同步（Task 7）。
5. **雲端刪除分類**：用到該分類的同步問卷各自產生新版本、本機分類清空，問卷不被刪除（Task 5、Task 7）。

---

### Task 1: `cloudapi` app 與共用欄位

**Files:**
- Create: `cloudapi/__init__.py`、`cloudapi/apps.py`、`cloudapi/models.py`、`cloudapi/admin.py`、`cloudapi/migrations/__init__.py`
- Create: `cloudapi/management/__init__.py`、`cloudapi/management/commands/__init__.py`、`cloudapi/management/commands/create_node_device.py`、`cloudapi/management/commands/rotate_node_token.py`
- Create: `cloudapi/tests/__init__.py`、`cloudapi/tests/test_models.py`
- Modify: `feedback/models.py`（`Survey`、`Question` 新欄位）
- Create: migrations（`cloudapi/0001`、`feedback/0020`、`cloudapi/0002` 由 makemigrations 產生後依 Step 5 調整）
- Modify: `config/settings.py`（`INSTALLED_APPS`、`CLOUD_SYNC_PROTOTYPE_ENABLED`）
- Modify: `.github/workflows/ci.yml`（測試標籤）

**Interfaces:**
- Produces:
  - `cloudapi.models.NodeDevice`（`uuid`、`name`、`token_hash`、`status`、`last_seen_at`；`NodeDevice.Status.ACTIVE/REVOKED`；`NodeDevice.hash_token(token) -> str`；`NodeDevice.issue(name) -> (device, token)`；`device.rotate() -> token`）
  - `cloudapi.models.ChangeClock`（單列 pk=1：`value`、`pruned_through`）
  - `cloudapi.models.SurveyDefinitionRevision`（`survey`、`version`、`definition`、`created_at`；建立後 `save()` 拋 `RevisionImmutable`）
  - `cloudapi.models.SurveyChange`（`seq` 唯一、`survey`、`definition_version`、`created_at`）
  - `Survey.uuid`、`Survey.definition_version`（預設 0）、`Survey.owner_node`（FK `NodeDevice`，可空，`PROTECT`）
  - `Question.uuid`、`Question.has_received_answer`（預設 `False`）
  - settings `CLOUD_SYNC_PROTOTYPE_ENABLED: bool`

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_models.py
from django.test import TestCase, override_settings

from cloudapi.models import ChangeClock, NodeDevice, RevisionImmutable, SurveyDefinitionRevision
from feedback.models import Question, Survey


class NodeDeviceTests(TestCase):
    def test_issue_stores_only_the_hash(self):
        device, token = NodeDevice.issue("office")
        self.assertTrue(token.startswith("fih_"))
        self.assertNotEqual(device.token_hash, token)
        self.assertEqual(device.token_hash, NodeDevice.hash_token(token))
        self.assertEqual(device.status, NodeDevice.Status.ACTIVE)

    def test_rotate_invalidates_the_old_token(self):
        device, old = NodeDevice.issue("office")
        new = device.rotate()
        device.refresh_from_db()
        self.assertNotEqual(old, new)
        self.assertEqual(device.token_hash, NodeDevice.hash_token(new))

    def test_rotate_command_prints_token_once_and_admin_cannot_rotate(self):
        import io

        from django.contrib import admin as django_admin
        from django.core.management import call_command

        device, _ = NodeDevice.issue("office")
        out = io.StringIO()
        call_command("rotate_node_token", name="office", stdout=out)
        token = out.getvalue().strip().splitlines()[-1]
        device.refresh_from_db()
        self.assertEqual(device.token_hash, NodeDevice.hash_token(token))
        model_admin = django_admin.site._registry[NodeDevice]
        self.assertEqual(tuple(model_admin.actions), ("revoke",))


class SharedFieldTests(TestCase):
    def test_new_rows_get_distinct_uuids_and_defaults(self):
        first = Survey.objects.create(title="A", slug="a")
        second = Survey.objects.create(title="B", slug="b")
        self.assertNotEqual(first.uuid, second.uuid)
        self.assertEqual(first.definition_version, 0)
        self.assertIsNone(first.owner_node)
        question = Question.objects.create(survey=first, title="Q", kind="short_text", data_type="text")
        self.assertFalse(question.has_received_answer)
        self.assertIsNotNone(question.uuid)

    def test_change_clock_row_exists(self):
        clock = ChangeClock.objects.get(pk=1)
        self.assertEqual((clock.value, clock.pruned_through), (0, 0))


class RevisionTests(TestCase):
    def test_revisions_are_immutable(self):
        survey = Survey.objects.create(title="A", slug="a")
        revision = SurveyDefinitionRevision.objects.create(survey=survey, version=1, definition={"title": "A"})
        revision.definition = {"title": "changed"}
        with self.assertRaises(RevisionImmutable):
            revision.save()
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi --settings=config.settings_test`
Expected: FAIL（`No module named 'cloudapi'` 或 app 未安裝）

- [ ] **Step 3: 建立 app 與模型**

```python
# cloudapi/apps.py
from django.apps import AppConfig


class CloudApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cloudapi"
    verbose_name = "雲端同步"
```

```python
# cloudapi/models.py
import hashlib
import secrets
import uuid

from django.db import models


class RevisionImmutable(Exception):
    """A survey definition revision never changes after it is written."""


class NodeDevice(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "啟用"
        REVOKED = "revoked", "已撤銷"

    TOKEN_PREFIX = "fih_"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField("名稱", max_length=120, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "本機節點裝置"
        verbose_name_plural = "本機節點裝置"

    def __str__(self):
        return self.name

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def _new_token(cls):
        return cls.TOKEN_PREFIX + secrets.token_urlsafe(32)

    @classmethod
    def issue(cls, name):
        token = cls._new_token()
        device = cls.objects.create(name=name, token_hash=cls.hash_token(token))
        return device, token

    def rotate(self):
        token = self._new_token()
        self.token_hash = self.hash_token(token)
        self.status = self.Status.ACTIVE
        self.save(update_fields=["token_hash", "status"])
        return token


class ChangeClock(models.Model):
    """Single row (pk=1) that hands out survey change sequence numbers in commit order."""

    value = models.PositiveBigIntegerField(default=0)
    pruned_through = models.PositiveBigIntegerField(default=0)


class SurveyDefinitionRevision(models.Model):
    survey = models.ForeignKey("feedback.Survey", on_delete=models.PROTECT, related_name="definition_revisions")
    version = models.PositiveIntegerField()
    definition = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("survey", "version"), name="cloudapi_revision_survey_version_uniq"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RevisionImmutable("問卷版本建立後不可修改")
        super().save(*args, **kwargs)


class SurveyChange(models.Model):
    seq = models.PositiveBigIntegerField(unique=True)
    survey = models.ForeignKey("feedback.Survey", on_delete=models.PROTECT, related_name="+")
    definition_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["seq"]
```

```python
# cloudapi/admin.py
from django.contrib import admin

from .models import NodeDevice


@admin.register(NodeDevice)
class NodeDeviceAdmin(admin.ModelAdmin):
    """Read-only list plus revoke. Tokens are issued and rotated only by management
    commands, which print them once to the operator's terminal — never through
    Django messages, which are stored in the session/cookie."""

    list_display = ("name", "uuid", "status", "last_seen_at", "created_at")
    readonly_fields = ("uuid", "name", "token_hash", "created_at", "last_seen_at")
    actions = ("revoke",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="撤銷權杖")
    def revoke(self, request, queryset):
        queryset.update(status=NodeDevice.Status.REVOKED)
```

```python
# cloudapi/management/commands/rotate_node_token.py
from django.core.management.base import BaseCommand, CommandError

from cloudapi.models import NodeDevice


class Command(BaseCommand):
    help = "為單一節點重新產生權杖並顯示一次（最後一行為權杖本身）；舊權杖立即失效。"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        device = NodeDevice.objects.filter(name=options["name"]).first()
        if device is None:
            raise CommandError("找不到節點")
        token = device.rotate()
        self.stdout.write(f"裝置 {device.name} 的新權杖只顯示這一次：")
        self.stdout.write(token)
```

```python
# cloudapi/management/commands/create_node_device.py
from django.core.management.base import BaseCommand

from cloudapi.models import NodeDevice


class Command(BaseCommand):
    help = "建立本機節點裝置並顯示一次性權杖（最後一行為權杖本身）。"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        device, token = NodeDevice.issue(options["name"])
        self.stdout.write(f"裝置 {device.name}（{device.uuid}）已建立；權杖只顯示這一次：")
        self.stdout.write(token)
```

在 `feedback/models.py` 的 `Survey` 欄位最後（`updated_at` 之後）加入：

```python
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    definition_version = models.PositiveIntegerField(default=0)
    owner_node = models.ForeignKey(
        "cloudapi.NodeDevice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="surveys",
    )
```

`Question` 欄位最後（`order` 之後）加入：

```python
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    has_received_answer = models.BooleanField(default=False)
```

`config/settings.py`：`INSTALLED_APPS` 在 `"feedback",` 之後加 `"cloudapi",`；在 `ANALYSIS_AUTO_AI_ENABLED` 那行下方加：

```python
# Cloud sync phase 1 is an isolated prototype (spec §12); production keeps this off.
CLOUD_SYNC_PROTOTYPE_ENABLED = os.getenv("CLOUD_SYNC_PROTOTYPE_ENABLED", "False").lower() == "true"
```

- [ ] **Step 4: 產生 migration**

Run: `.\.venv\Scripts\python.exe manage.py makemigrations cloudapi feedback --settings=config.settings_test`
Expected: `cloudapi/migrations/0001_initial.py`、`feedback/migrations/0020_...py`，可能再有 `cloudapi/migrations/0002_...py`（Django 自動拆開循環依賴）。

- [ ] **Step 5: 修正 UUID 欄位的 migration（既有資料必須各自得到不同 UUID）**

`makemigrations` 對既有列用同一個預設值，會違反唯一限制。把 `feedback/migrations/0020_*.py` 中 `Survey.uuid` 與 `Question.uuid` 的兩個 `AddField` 換成下列三段（其餘 `AddField` 保留原樣），檔頭加 `import uuid`：

```python
def fill_uuids(apps, schema_editor):
    for model_name in ("Survey", "Question"):
        model = apps.get_model("feedback", model_name)
        for pk in model.objects.values_list("pk", flat=True):
            model.objects.filter(pk=pk).update(uuid=uuid.uuid4())


# operations 中，原本兩個 uuid 的 AddField 改為：
        migrations.AddField(model_name="survey", name="uuid", field=models.UUIDField(null=True, editable=False)),
        migrations.AddField(model_name="question", name="uuid", field=models.UUIDField(null=True, editable=False)),
        migrations.RunPython(fill_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="survey", name="uuid", field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
        ),
        migrations.AlterField(
            model_name="question", name="uuid", field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
        ),
```

再在 `cloudapi` 最後一個 migration 的 operations 末尾加入建立時鐘列：

```python
def create_clock(apps, schema_editor):
    apps.get_model("cloudapi", "ChangeClock").objects.get_or_create(pk=1)


# operations 末尾：
        migrations.RunPython(create_clock, migrations.RunPython.noop),
```

- [ ] **Step 6: 執行測試與檢查**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi --settings=config.settings_test`
Expected: PASS
Run（cloud 與 node 各一次）：`manage.py makemigrations --check --dry-run --settings=config.settings_test`
Expected: `No changes detected`

- [ ] **Step 7: CI 標籤**

`.github/workflows/ci.yml`：`test` job 的測試指令改為 `python manage.py test feedback accounts config cloudapi --settings=config.settings_test`；
`node-mode` job 改為 `python manage.py test feedback accounts config node organizations cloudapi cloudsync --settings=config.settings_test`
（`cloudsync` 於 Task 6 建立；在那之前 node job 先只加 `cloudapi`，Task 6 再補 `cloudsync`）。

- [ ] **Step 8: Commit（需使用者授權）**

```bash
git add cloudapi feedback/models.py feedback/migrations config/settings.py .github/workflows/ci.yml
git commit -m "feat(cloudapi): node devices, survey/question UUIDs, definition versions and change clock"
```

---

### Task 2: 定義 dict（序列化、編輯、套用）

**Files:**
- Create: `cloudapi/errors.py`、`cloudapi/definition.py`
- Test: `cloudapi/tests/test_definition.py`

**Interfaces:**
- Consumes: Task 1 模型
- Produces:
  - `cloudapi.errors`：`DefinitionCommitError`（`user_message`）、`VersionConflict(current_version)`、`SemanticLockViolation(question_uuid)`、`DefinitionError`
  - `cloudapi.definition.SEMANTIC_FIELDS = ("kind", "data_type", "options_text")`
  - `serialize_definition(survey) -> dict`
  - `validate_definition(definition) -> None`（不合格拋 `DefinitionError`）
  - `apply_definition(survey, definition, *, version) -> None`（寫回模型；缺少的題目停用、絕不刪除）
  - 編輯函式（就地修改 dict）：`add_question(definition, data)`、`update_question(definition, question_uuid, data)`、
    `set_question_active(definition, question_uuid, active)`、`move_question(definition, question_uuid, direction)`、
    `update_survey(definition, data)`、`archive_survey(definition, when)`
  - 定義 dict 格式：
    `{"survey_uuid", "version", "title", "slug", "description", "is_active", "analysis_enabled", "thank_you_email_enabled", "improvement_tracking_enabled", "category", "archived_at", "questions": [{"uuid", "code", "title", "help_text", "kind", "data_type", "options_text", "is_required", "enable_keyword_tracking", "is_active", "order"}]}`

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_definition.py
from datetime import datetime, timezone as dt_timezone

from django.test import TestCase, override_settings

from cloudapi.definition import (
    add_question,
    apply_definition,
    archive_survey,
    move_question,
    serialize_definition,
    set_question_active,
    update_question,
    update_survey,
    validate_definition,
)
from cloudapi.errors import DefinitionError
from feedback.models import Answer, FeedbackSubmission, Question, Survey, SurveyCategory

QUESTION = {
    "title": "滿意度",
    "help_text": "",
    "kind": "single_choice",
    "data_type": "ordinal",
    "options_text": "好\n普通\n差",
    "is_required": True,
    "enable_keyword_tracking": False,
    "order": 1,
}


class DefinitionRoundTripTests(TestCase):
    def setUp(self):
        self.category = SurveyCategory.objects.create(name="門市")
        self.survey = Survey.objects.create(title="門市問卷", slug="store", category=self.category, definition_version=3)
        self.question = Question.objects.create(survey=self.survey, title="感想", kind="long_text", data_type="text", order=1)

    def test_serialize_contains_uuids_category_name_and_version(self):
        definition = serialize_definition(self.survey)
        self.assertEqual(definition["survey_uuid"], str(self.survey.uuid))
        self.assertEqual(definition["version"], 3)
        self.assertEqual(definition["category"], "門市")
        self.assertIsNone(definition["archived_at"])
        self.assertEqual(definition["questions"][0]["uuid"], str(self.question.uuid))
        validate_definition(definition)

    def test_apply_updates_adds_and_deactivates_but_never_deletes(self):
        submission = FeedbackSubmission.objects.create(survey=self.survey)
        Answer.objects.create(submission=submission, question=self.question, value="很好")
        definition = serialize_definition(self.survey)
        definition["questions"] = []  # the old question disappears from the definition
        add_question(definition, QUESTION)
        apply_definition(self.survey, definition, version=4)

        self.survey.refresh_from_db()
        self.assertEqual(self.survey.definition_version, 4)
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)
        self.assertEqual(Answer.objects.count(), 1)
        added = Question.objects.get(survey=self.survey, title="滿意度")
        self.assertEqual(added.options, ["好", "普通", "差"])

    def test_category_by_name_and_clearing(self):
        definition = serialize_definition(self.survey)
        update_survey(definition, {"category": None})
        apply_definition(self.survey, definition, version=4)
        self.survey.refresh_from_db()
        self.assertIsNone(self.survey.category)
        definition["category"] = "外送"
        apply_definition(self.survey, definition, version=5)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.category.name, "外送")

    def test_invalid_question_is_rejected(self):
        definition = serialize_definition(self.survey)
        add_question(definition, {**QUESTION, "kind": "multiple_choice", "data_type": "ordinal"})
        with self.assertRaises(DefinitionError):
            apply_definition(self.survey, definition, version=4)

    def test_rejected_definition_writes_nothing(self):
        definition = serialize_definition(self.survey)
        definition["title"] = "不該寫入"
        bad = {**QUESTION, "kind": "dropdown"}  # unknown kind
        add_question(definition, bad)
        with self.assertRaises(DefinitionError):
            apply_definition(self.survey, definition, version=4)
        self.survey.refresh_from_db()
        self.assertEqual((self.survey.title, self.survey.definition_version), ("門市問卷", 3))

    def test_strict_validation(self):
        good = serialize_definition(self.survey)
        cases = {
            "unknown kind": lambda d: d["questions"][0].update(kind="dropdown"),
            "duplicate uuid": lambda d: d["questions"].append(dict(d["questions"][0])),
            "string flag": lambda d: d.update(is_active="yes"),
            "bool order": lambda d: d["questions"][0].update(order=True),
            "long title": lambda d: d.update(title="x" * 256),
            "bad slug": lambda d: d.update(slug="有中文"),
            "bad archived": lambda d: d.update(archived_at="yesterday"),
            "bad version": lambda d: d.update(version=-1),
            "choice without options": lambda d: d["questions"][0].update(kind="single_choice", data_type="nominal",
                                                                        options_text=""),
        }
        for name, mutate in cases.items():
            broken = {**good, "questions": [dict(q) for q in good["questions"]]}
            mutate(broken)
            with self.subTest(name), self.assertRaises(DefinitionError):
                validate_definition(broken)


class EditHelperTests(TestCase):
    def setUp(self):
        survey = Survey.objects.create(title="S", slug="s")
        self.definition = serialize_definition(survey)
        add_question(self.definition, {**QUESTION, "order": 1})
        add_question(self.definition, {**QUESTION, "title": "第二題", "order": 2})
        self.first, self.second = self.definition["questions"]

    def test_update_and_toggle(self):
        update_question(self.definition, self.first["uuid"], {**QUESTION, "title": "新標題"})
        self.assertEqual(self.first["title"], "新標題")
        set_question_active(self.definition, self.first["uuid"], False)
        self.assertFalse(self.first["is_active"])

    def test_move_swaps_order_with_neighbour(self):
        move_question(self.definition, self.second["uuid"], "up")
        self.assertEqual((self.first["order"], self.second["order"]), (2, 1))
        move_question(self.definition, self.second["uuid"], "up")  # already first: no change
        self.assertEqual((self.first["order"], self.second["order"]), (2, 1))

    def test_archive(self):
        archive_survey(self.definition, datetime(2026, 10, 2, tzinfo=dt_timezone.utc))
        self.assertFalse(self.definition["is_active"])
        self.assertFalse(self.definition["analysis_enabled"])
        self.assertTrue(self.definition["archived_at"].startswith("2026-10-02"))

    def test_unknown_question_is_rejected(self):
        with self.assertRaises(DefinitionError):
            set_question_active(self.definition, "00000000-0000-0000-0000-000000000000", False)

    def test_validate_rejects_missing_keys(self):
        broken = dict(self.definition)
        broken.pop("title")
        with self.assertRaises(DefinitionError):
            validate_definition(broken)
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi.tests.test_definition --settings=config.settings_test`
Expected: FAIL（`No module named 'cloudapi.definition'`）

- [ ] **Step 3: 實作**

```python
# cloudapi/errors.py
class DefinitionCommitError(Exception):
    """A survey definition change that could not be committed; `user_message` is shown on the page."""

    user_message = "問卷儲存失敗"


class VersionConflict(DefinitionCommitError):
    user_message = "版本不一致，請重新載入"

    def __init__(self, current_version):
        super().__init__(f"current version is {current_version}")
        self.current_version = current_version


class SemanticLockViolation(DefinitionCommitError):
    user_message = "此題已有回覆，請新增題目取代並停用舊題"

    def __init__(self, question_uuid):
        super().__init__(f"question {question_uuid} already has answers")
        self.question_uuid = question_uuid


class DefinitionError(DefinitionCommitError, ValueError):
    user_message = "問卷內容無效"
```

```python
# cloudapi/definition.py
"""One dict format for a survey definition, shared by the cloud and the node.

Editing always changes the dict first; `apply_definition` then writes it onto
the models.  Questions are never deleted here: a question missing from the
definition is deactivated, so answers that point at it survive.
"""

import re
import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from feedback.models import Question, SurveyCategory

from .errors import DefinitionError

SEMANTIC_FIELDS = ("kind", "data_type", "options_text")
SURVEY_FIELDS = (
    "title",
    "slug",
    "description",
    "is_active",
    "analysis_enabled",
    "thank_you_email_enabled",
    "improvement_tracking_enabled",
)
QUESTION_FIELDS = (
    "code",
    "title",
    "help_text",
    "kind",
    "data_type",
    "options_text",
    "is_required",
    "enable_keyword_tracking",
    "is_active",
    "order",
)
EDITABLE_QUESTION_FIELDS = (
    "title",
    "help_text",
    "kind",
    "data_type",
    "options_text",
    "is_required",
    "enable_keyword_tracking",
    "order",
)
EDITABLE_SURVEY_FIELDS = ("title", "description", "is_active", "analysis_enabled", "thank_you_email_enabled")


def serialize_definition(survey):
    questions = survey.questions.order_by("order", "id")
    return {
        "survey_uuid": str(survey.uuid),
        "version": survey.definition_version,
        **{field: getattr(survey, field) for field in SURVEY_FIELDS},
        "category": survey.category.name if survey.category_id else None,
        "archived_at": survey.archived_at.isoformat() if survey.archived_at else None,
        "questions": [
            {"uuid": str(question.uuid), **{field: getattr(question, field) for field in QUESTION_FIELDS}}
            for question in questions
        ],
    }


MAX_QUESTIONS = 200
SLUG_RE = re.compile(r"^[-a-zA-Z0-9_]*$")
# Same compatibility table as Question.clean(), plus a closed set of kinds.
ALLOWED_DATA_TYPES = {
    "short_text": {"text"},
    "long_text": {"text"},
    "single_choice": {"nominal", "ordinal"},
    "multiple_choice": {"nominal"},
    "integer": {"discrete"},
    "decimal": {"continuous"},
    "scale": {"ordinal"},
}


def _require(condition, message):
    if not condition:
        raise DefinitionError(message)


def _text(value, name, *, max_length, allow_empty=True):
    _require(isinstance(value, str), f"{name} 必須是字串")
    _require(allow_empty or value.strip(), f"{name} 不可空白")
    _require(len(value) <= max_length, f"{name} 超過 {max_length} 字")


def _flag(value, name):
    _require(isinstance(value, bool), f"{name} 必須是布林值")


def _uuid(value, name):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise DefinitionError(f"{name} 格式錯誤") from exc


def validate_definition(definition):
    """Reject anything that is not a complete, well-typed definition. Runs before any database write."""

    _require(isinstance(definition, dict), "定義必須是物件")
    required = ("survey_uuid", "version", *SURVEY_FIELDS, "category", "archived_at", "questions")
    missing = [key for key in required if key not in definition]
    _require(not missing, f"缺少欄位：{', '.join(missing)}")
    _uuid(definition["survey_uuid"], "survey_uuid")
    version = definition["version"]
    _require(isinstance(version, int) and not isinstance(version, bool) and version >= 0, "version 必須是非負整數")
    _text(definition["title"], "title", max_length=255, allow_empty=False)
    _text(definition["slug"], "slug", max_length=50)
    _require(SLUG_RE.match(definition["slug"]) is not None, "slug 只能包含英數字、- 與 _")
    _text(definition["description"], "description", max_length=10000)
    for name in ("is_active", "analysis_enabled", "thank_you_email_enabled", "improvement_tracking_enabled"):
        _flag(definition[name], name)
    category = definition["category"]
    _require(category is None or (isinstance(category, str) and 0 < len(category.strip()) <= 100), "category 格式錯誤")
    archived = definition["archived_at"]
    if archived is not None:
        _require(isinstance(archived, str), "archived_at 格式錯誤")
        try:
            datetime.fromisoformat(archived)
        except ValueError as exc:
            raise DefinitionError("archived_at 格式錯誤") from exc
    questions = definition["questions"]
    _require(isinstance(questions, list), "questions 必須是陣列")
    _require(len(questions) <= MAX_QUESTIONS, f"題目不得超過 {MAX_QUESTIONS} 題")
    seen = set()
    for item in questions:
        _require(isinstance(item, dict), "題目必須是物件")
        absent = [key for key in ("uuid", *QUESTION_FIELDS) if key not in item]
        _require(not absent, f"題目缺少欄位：{', '.join(absent)}")
        key = _uuid(item["uuid"], "題目 uuid")
        _require(key not in seen, "題目 uuid 重複")
        seen.add(key)
        _text(item["code"], "code", max_length=80)
        _text(item["title"], "題目名稱", max_length=255, allow_empty=False)
        _text(item["help_text"], "補充說明", max_length=255)
        _require(item["kind"] in ALLOWED_DATA_TYPES, "作答形式不合法")
        _require(item["data_type"] in ALLOWED_DATA_TYPES[item["kind"]], "資料型態與作答形式不相容")
        _text(item["options_text"], "選項內容", max_length=10000)
        if item["kind"] in {"single_choice", "multiple_choice"}:
            _require(any(line.strip() for line in item["options_text"].splitlines()), "單選與多選題至少需要一個選項")
        for name in ("is_required", "enable_keyword_tracking", "is_active"):
            _flag(item[name], name)
        order = item["order"]
        _require(isinstance(order, int) and not isinstance(order, bool) and 0 <= order <= 10000, "排序必須是 0–10000 的整數")


@transaction.atomic
def apply_definition(survey, definition, *, version):
    validate_definition(definition)
    for field in SURVEY_FIELDS:
        if field == "slug" and survey.pk and not definition["slug"]:
            continue
        setattr(survey, field, definition[field])
    name = definition["category"]
    survey.category = SurveyCategory.objects.get_or_create(name=name)[0] if name else None
    archived = definition["archived_at"]
    survey.archived_at = datetime.fromisoformat(archived) if archived else None
    survey.definition_version = version
    survey.save()

    existing = {str(question.uuid): question for question in survey.questions.all()}
    seen = set()
    for item in definition["questions"]:
        key = str(item["uuid"])
        question = existing.get(key) or Question(survey=survey, uuid=key)
        for field in QUESTION_FIELDS:
            setattr(question, field, item[field])
        try:
            question.clean()
        except ValidationError as exc:
            raise DefinitionError("; ".join(exc.messages)) from exc
        question.save()
        seen.add(key)
    for key, question in existing.items():
        if key not in seen and question.is_active:
            question.is_active = False
            question.save(update_fields=["is_active"])


def _question(definition, question_uuid):
    for item in definition["questions"]:
        if item["uuid"] == str(question_uuid):
            return item
    raise DefinitionError("找不到這一題")


def add_question(definition, data):
    item = {"uuid": str(uuid.uuid4()), "code": "", "is_active": True}
    item.update({field: data.get(field, "") for field in EDITABLE_QUESTION_FIELDS})
    item["is_required"] = bool(data.get("is_required", True))
    item["enable_keyword_tracking"] = bool(data.get("enable_keyword_tracking", False))
    item["order"] = int(data.get("order") or len(definition["questions"]) + 1)
    definition["questions"].append(item)
    return item


def update_question(definition, question_uuid, data):
    item = _question(definition, question_uuid)
    for field in EDITABLE_QUESTION_FIELDS:
        if field in data:
            item[field] = data[field]


def set_question_active(definition, question_uuid, active):
    _question(definition, question_uuid)["is_active"] = bool(active)


def move_question(definition, question_uuid, direction):
    target = _question(definition, question_uuid)
    ordered = sorted(definition["questions"], key=lambda item: item["order"])
    index = ordered.index(target)
    neighbour = index - 1 if direction == "up" else index + 1
    if 0 <= neighbour < len(ordered):
        ordered[index]["order"], ordered[neighbour]["order"] = ordered[neighbour]["order"], ordered[index]["order"]


def update_survey(definition, data):
    for field in EDITABLE_SURVEY_FIELDS:
        if field in data:
            definition[field] = data[field]
    if "category" in data:
        category = data["category"]
        definition["category"] = getattr(category, "name", category) or None


def archive_survey(definition, when):
    definition["is_active"] = False
    definition["analysis_enabled"] = False
    definition["archived_at"] = when.isoformat()
```

- [ ] **Step 4: 執行確認通過**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi.tests.test_definition --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudapi/errors.py cloudapi/definition.py cloudapi/tests/test_definition.py
git commit -m "feat(cloudapi): shared survey definition dict with edit helpers and non-destructive apply"
```

---

### Task 3: 雲端條件式寫入、語意鎖、變更序列

**Files:**
- Create: `cloudapi/writes.py`
- Create: `cloudapi/management/commands/assign_survey_node.py`、`cloudapi/management/commands/prune_survey_changes.py`
- Modify: `feedback/analysis_jobs.py`（雲端不為同步問卷排程分析）
- Test: `cloudapi/tests/test_writes.py`、`cloudapi/test_postgres.py`
- Modify: `.github/workflows/ci.yml`（PostgreSQL job 加 `cloudapi.test_postgres`）

**Interfaces:**
- Consumes: Task 1、Task 2
- Produces:
  - `tick_clock() -> int`（呼叫端須在交易內；持有時鐘列鎖直到提交）
  - `record_revision(survey) -> None`（寫 revision 與 `SurveyChange`）
  - `change_definition(survey_uuid, *, expected_version, definition) -> SurveyDefinitionRevision`（鎖問卷列；版本不符拋 `VersionConflict`；語意鎖拋 `SemanticLockViolation`；回傳本交易寫入的 revision，`.survey`、`.version`、`.definition`）
  - `create_node_survey(node, definition) -> (SurveyDefinitionRevision, created: bool)`（已存在時回傳該問卷目前版本的 revision）
  - `assign_survey_to_node(survey, node) -> SurveyDefinitionRevision`
  - `record_revision(survey) -> SurveyDefinitionRevision`
  - `unique_slug(text) -> str`

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_writes.py
from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition, update_question
from cloudapi.errors import SemanticLockViolation, VersionConflict
from cloudapi.models import ChangeClock, NodeDevice, SurveyChange, SurveyDefinitionRevision
from cloudapi.writes import assign_survey_to_node, change_definition, create_node_survey
from feedback.models import AnalysisJob, Answer, FeedbackSubmission, Question, Survey

QUESTION = {"title": "Q", "help_text": "", "kind": "single_choice", "data_type": "nominal", "options_text": "A\nB",
            "is_required": True, "enable_keyword_tracking": False, "order": 1}


class AssignTests(TestCase):
    def test_assign_backfills_answered_questions_and_records_version_1(self):
        node, _token = NodeDevice.issue("office")
        survey = Survey.objects.create(title="S", slug="s")
        answered = Question.objects.create(survey=survey, title="A", kind="short_text", data_type="text", order=1)
        empty = Question.objects.create(survey=survey, title="B", kind="short_text", data_type="text", order=2)
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=survey), question=answered, value="x")

        assign_survey_to_node(survey, node)

        survey.refresh_from_db()
        self.assertEqual((survey.owner_node, survey.definition_version), (node, 1))
        self.assertTrue(Question.objects.get(pk=answered.pk).has_received_answer)
        self.assertFalse(Question.objects.get(pk=empty.pk).has_received_answer)
        self.assertTrue(SurveyDefinitionRevision.objects.filter(survey=survey, version=1).exists())
        self.assertEqual(SurveyChange.objects.get().seq, ChangeClock.objects.get(pk=1).value)


class ChangeDefinitionTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        revision, _ = create_node_survey(self.node, {
            "survey_uuid": "11111111-1111-1111-1111-111111111111", "version": 0, "title": "門市", "slug": "",
            "description": "", "is_active": True, "analysis_enabled": True, "thank_you_email_enabled": True,
            "improvement_tracking_enabled": True, "category": None, "archived_at": None, "questions": [],
        })
        self.survey = revision.survey

    def test_created_survey_belongs_to_node_with_version_1(self):
        self.assertEqual((self.survey.owner_node, self.survey.definition_version), (self.node, 1))
        self.assertTrue(self.survey.slug)

    def test_matching_version_bumps_and_records_change(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        revision = change_definition(self.survey.uuid, expected_version=1, definition=definition)
        self.assertEqual((revision.survey.definition_version, revision.definition["version"]), (2, 2))
        self.assertEqual(len(revision.definition["questions"]), 1)
        self.assertEqual(list(SurveyChange.objects.values_list("definition_version", flat=True)), [1, 2])

    def test_stale_version_is_rejected_without_writing(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        with self.assertRaises(VersionConflict) as caught:
            change_definition(self.survey.uuid, expected_version=0, definition=definition)
        self.assertEqual(caught.exception.current_version, 1)
        self.assertFalse(Question.objects.filter(survey=self.survey).exists())

    def test_semantic_fields_of_answered_question_are_locked(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        Question.objects.filter(survey=self.survey).update(has_received_answer=True)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        question_uuid = definition["questions"][0]["uuid"]
        update_question(definition, question_uuid, {"options_text": "A\nB\nC"})
        with self.assertRaises(SemanticLockViolation):
            change_definition(self.survey.uuid, expected_version=2, definition=definition)
        update_question(definition, question_uuid, {"options_text": "A\nB", "title": "新標題"})
        change_definition(self.survey.uuid, expected_version=2, definition=definition)  # wording is free

    def test_unanswered_new_question_stays_editable(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        definition = serialize_definition(Survey.objects.get(pk=self.survey.pk))
        update_question(definition, definition["questions"][0]["uuid"], {"options_text": "A\nB\nC"})
        change_definition(self.survey.uuid, expected_version=2, definition=definition)

    def test_cloud_does_not_schedule_analysis_for_node_surveys(self):
        definition = serialize_definition(self.survey)
        add_question(definition, QUESTION)
        change_definition(self.survey.uuid, expected_version=1, definition=definition)
        self.assertFalse(AnalysisJob.objects.filter(survey=self.survey).exists())

    def test_create_is_idempotent_by_uuid(self):
        again, created = create_node_survey(self.node, serialize_definition(self.survey))
        self.assertEqual((again.survey.pk, again.version, created), (self.survey.pk, 1, False))
```

```python
# cloudapi/test_postgres.py
import threading
import time
from unittest import SkipTest

from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase

from cloudapi.definition import serialize_definition, update_survey
from cloudapi.models import ChangeClock, NodeDevice, SurveyChange
from cloudapi.writes import change_definition, create_node_survey


def blank(index):
    return {"survey_uuid": f"00000000-0000-0000-0000-00000000000{index}", "version": 0, "title": f"S{index}",
            "slug": "", "description": "", "is_active": True, "analysis_enabled": True,
            "thank_you_email_enabled": True, "improvement_tracking_enabled": True, "category": None,
            "archived_at": None, "questions": []}


def visible_seqs():
    """Read SurveyChange through a fresh connection, i.e. what another client sees right now."""

    observer = connections.create_connection("default")
    try:
        with observer.cursor() as cursor:
            cursor.execute(f"SELECT seq FROM {SurveyChange._meta.db_table} ORDER BY seq")
            return [row[0] for row in cursor.fetchall()]
    finally:
        observer.close()


class ChangeSequencePostgreSQLTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise SkipTest("需要使用隔離 PostgreSQL 執行")
        super().setUpClass()

    def setUp(self):
        ChangeClock.objects.get_or_create(pk=1)
        node, _ = NodeDevice.issue("pg")
        self.first = create_node_survey(node, blank(1))[0].survey
        self.second = create_node_survey(node, blank(2))[0].survey
        self.baseline = visible_seqs()  # seqs 1 and 2 from the two creates

    def edit(self, survey, *, hold=None, entered=None, errors):
        try:
            definition = serialize_definition(survey)
            update_survey(definition, {"title": survey.title + "!"})
            with transaction.atomic():
                change_definition(survey.uuid, expected_version=1, definition=definition)
                if entered:
                    entered.set()
                if hold:
                    hold.wait(10)  # keep the transaction (and the clock row lock) open
        except Exception as exc:  # pragma: no cover - reported by the test
            errors.append(exc)
        finally:
            close_old_connections()

    def test_no_later_sequence_becomes_visible_before_an_earlier_one_commits(self):
        hold, entered, errors = threading.Event(), threading.Event(), []
        first = threading.Thread(target=self.edit, args=(self.first,), kwargs={"hold": hold, "entered": entered, "errors": errors})
        first.start()
        self.assertTrue(entered.wait(10))  # first has taken seq 3 and is still uncommitted

        second = threading.Thread(target=self.edit, args=(self.second,), kwargs={"errors": errors})
        second.start()
        time.sleep(0.5)
        # The second writer must be blocked on the clock row, so nothing new is visible yet.
        self.assertTrue(second.is_alive())
        self.assertEqual(visible_seqs(), self.baseline)

        hold.set()
        first.join(10)
        second.join(10)
        self.assertEqual(errors, [])
        seqs = visible_seqs()
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        by_survey = dict(SurveyChange.objects.filter(seq__gt=2).values_list("survey_id", "seq"))
        self.assertLess(by_survey[self.first.pk], by_survey[self.second.pk])
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi.tests.test_writes --settings=config.settings_test`
Expected: FAIL（`No module named 'cloudapi.writes'`）

- [ ] **Step 3: 實作**

```python
# cloudapi/writes.py
"""Cloud-side writes to node-owned survey definitions.

Every change locks the survey row, checks the expected version and the
semantic lock, writes the models, then records an immutable revision and a
SurveyChange whose sequence number comes from the locked ChangeClock row, so
sequence order equals commit order (spec §4).
"""

from django.db import transaction
from django.utils.text import slugify

from feedback.models import Question, Survey

from .definition import SEMANTIC_FIELDS, apply_definition, serialize_definition, validate_definition
from .errors import SemanticLockViolation, VersionConflict
from .models import ChangeClock, SurveyChange, SurveyDefinitionRevision


def tick_clock():
    clock = ChangeClock.objects.select_for_update().get(pk=1)
    clock.value += 1
    clock.save(update_fields=["value"])
    return clock.value


def record_revision(survey):
    """Freeze the definition written in this transaction. Callers return this revision's
    definition instead of re-serializing later, so a reply never mixes versions."""

    revision = SurveyDefinitionRevision.objects.create(
        survey=survey, version=survey.definition_version, definition=serialize_definition(survey)
    )
    SurveyChange.objects.create(seq=tick_clock(), survey=survey, definition_version=survey.definition_version)
    return revision


def unique_slug(text):
    base = (slugify(text) or "survey")[:40]
    slug, counter = base, 2
    while Survey.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def check_semantic_lock(survey, definition):
    incoming = {str(item["uuid"]): item for item in definition["questions"]}
    for question in survey.questions.filter(has_received_answer=True):
        item = incoming.get(str(question.uuid))
        if item is not None and any(item[field] != getattr(question, field) for field in SEMANTIC_FIELDS):
            raise SemanticLockViolation(str(question.uuid))


@transaction.atomic
def change_definition(survey_uuid, *, expected_version, definition):
    validate_definition(definition)
    survey = Survey.objects.select_for_update().get(uuid=survey_uuid)
    if survey.definition_version != expected_version:
        raise VersionConflict(survey.definition_version)
    check_semantic_lock(survey, definition)
    apply_definition(survey, {**definition, "survey_uuid": str(survey.uuid), "slug": survey.slug},
                     version=survey.definition_version + 1)
    return record_revision(survey)


@transaction.atomic
def create_node_survey(node, definition):
    validate_definition(definition)
    existing = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
    if existing is not None:
        if existing.owner_node_id != node.pk:
            raise PermissionError("survey belongs to another node")
        revision = SurveyDefinitionRevision.objects.get(survey=existing, version=existing.definition_version)
        return revision, False
    slug = unique_slug(definition["slug"] or definition["title"])
    survey = Survey(uuid=definition["survey_uuid"], owner_node=node, slug=slug)
    apply_definition(survey, {**definition, "slug": slug}, version=1)
    return record_revision(survey), True


@transaction.atomic
def assign_survey_to_node(survey, node):
    survey = Survey.objects.select_for_update().get(pk=survey.pk)
    survey.owner_node = node
    survey.definition_version += 1
    survey.save(update_fields=["owner_node", "definition_version"])
    Question.objects.filter(survey=survey, answers__isnull=False).update(has_received_answer=True)
    return record_revision(survey)
```

`feedback/analysis_jobs.py` 的 `schedule_survey_analysis` 中，在 `if not survey: return None` 之後加入（檔頭補 `from django.conf import settings`，若已有則略過）：

```python
        if not settings.IS_NODE and survey.owner_node_id:
            # Node-owned surveys are analysed only on the node (spec §3).
            return None
```

```python
# cloudapi/management/commands/assign_survey_node.py
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Survey


class Command(BaseCommand):
    help = "把問卷指派給本機節點（原型限定，需 CLOUD_SYNC_PROTOTYPE_ENABLED）。"

    def add_arguments(self, parser):
        parser.add_argument("--survey", required=True, help="問卷 slug")
        parser.add_argument("--node", required=True, help="節點名稱")

    def handle(self, *args, **options):
        if not settings.CLOUD_SYNC_PROTOTYPE_ENABLED:
            raise CommandError("雲端同步原型未啟用（CLOUD_SYNC_PROTOTYPE_ENABLED）；正式網站本階段不得指派問卷。")
        survey = Survey.objects.filter(slug=options["survey"]).first()
        node = NodeDevice.objects.filter(name=options["node"]).first()
        if survey is None or node is None:
            raise CommandError("找不到問卷或節點")
        revision = assign_survey_to_node(survey, node)
        self.stdout.write(f"{survey.slug} 已指派給 {node.name}，版本 {revision.version}")
```

```python
# cloudapi/management/commands/prune_survey_changes.py
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from cloudapi.models import ChangeClock, SurveyChange


class Command(BaseCommand):
    help = "刪除超過保留期的問卷變更紀錄；更早的游標之後回 410。"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)

    @transaction.atomic
    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        old = SurveyChange.objects.filter(created_at__lt=cutoff)
        last = old.order_by("-seq").values_list("seq", flat=True).first()
        if last is None:
            self.stdout.write("沒有需要刪除的紀錄")
            return
        count = old.count()
        old.delete()
        clock = ChangeClock.objects.select_for_update().get(pk=1)
        clock.pruned_through = max(clock.pruned_through, last)
        clock.save(update_fields=["pruned_through"])
        self.stdout.write(f"已刪除 {count} 筆，保留期起點序號 {last}")
```

`.github/workflows/ci.yml` 的 PostgreSQL job 最後一行改為：
`- run: python manage.py test feedback.test_analysis_jobs_postgres cloudapi.test_postgres --settings=config.settings_postgres_test`

在 `cloudapi/tests/test_writes.py` 補一個指令閘門測試：

```python
class AssignCommandGateTests(TestCase):
    def test_command_refuses_when_prototype_disabled(self):
        from django.core.management import CommandError, call_command

        with override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=False), self.assertRaises(CommandError):
            call_command("assign_survey_node", survey="x", node="y")
```

- [ ] **Step 4: 執行確認通過**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi feedback.test_analysis_jobs --settings=config.settings_test`
Expected: PASS（`cloudapi.test_postgres` 在 SQLite 下 skipped）

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudapi feedback/analysis_jobs.py .github/workflows/ci.yml
git commit -m "feat(cloudapi): versioned definition writes with semantic lock and commit-ordered change sequence"
```

---

### Task 4: 節點 API（權杖、游標、問卷端點、heartbeat）

**Files:**
- Create: `cloudapi/auth.py`、`cloudapi/views.py`、`cloudapi/urls.py`
- Modify: `config/urls.py`（cloud 模式掛載 `api/node/v1/`）
- Test: `cloudapi/tests/test_api.py`

**Interfaces:**
- Consumes: Task 1–3
- Produces（HTTP，皆需 `Authorization: Bearer <權杖>`；原型關閉回 503）：
  - `GET surveys/snapshot/` → `{"cursor": str, "surveys": [definition]}`
  - `GET surveys/changes/?cursor=&limit=` → `{"changes": [{"seq": int, "definition": definition}], "next_cursor": str, "has_more": bool}`；游標無效 410
  - `GET surveys/<uuid>/revisions/<int:version>/` → `{"definition": definition}`
  - `POST surveys/`（body：definition）→ 201／200 `{"definition": definition}`
  - `PUT surveys/<uuid>/`（body：`{"expected_version", "definition"}`）→ 200 `{"definition"}`；409 `{"error": "version_conflict", "current_version"}`；422 `{"error": "semantic_lock", "question_uuid"}`；400 `{"error": "invalid_definition", "message"}`
  - `POST heartbeat/` → `{"node_uuid": str, "server_time": iso}`
  - 游標格式（不透明字串）：`"<node_uuid>:<seq>"`；`cloudapi.auth.make_cursor(device, seq)`、`parse_cursor(device, text) -> int`（失敗拋 `CursorInvalid`）

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_api.py
import json

from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition
from cloudapi.models import NodeDevice
from cloudapi.writes import create_node_survey
from feedback.models import Question, Survey

BASE = "/api/node/v1/"
QUESTION = {"title": "Q", "help_text": "", "kind": "short_text", "data_type": "text", "options_text": "",
            "is_required": True, "enable_keyword_tracking": False, "order": 1}


def blank(uuid_text, title="S"):
    return {"survey_uuid": uuid_text, "version": 0, "title": title, "slug": "", "description": "", "is_active": True,
            "analysis_enabled": True, "thank_you_email_enabled": True, "improvement_tracking_enabled": True,
            "category": None, "archived_at": None, "questions": []}


@override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
class NodeApiTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.other, self.other_token = NodeDevice.issue("other")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def call(self, method, path, body=None, token=None, **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token}", **extra}
        kwargs = {"content_type": "application/json", "data": json.dumps(body)} if body is not None else {}
        return getattr(self.client, method)(BASE + path, **kwargs, **headers)

    def test_missing_or_revoked_token_is_401(self):
        self.assertEqual(self.client.get(BASE + "surveys/snapshot/").status_code, 401)
        self.node.status = NodeDevice.Status.REVOKED
        self.node.save()
        self.assertEqual(self.call("get", "surveys/snapshot/").status_code, 401)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=False)
    def test_disabled_prototype_is_503(self):
        self.assertEqual(self.call("get", "surveys/snapshot/").status_code, 503)

    def test_create_then_snapshot_then_changes(self):
        response = self.call("post", "surveys/", blank("22222222-2222-2222-2222-222222222222"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.call("post", "surveys/", blank("22222222-2222-2222-2222-222222222222")).status_code, 200)

        snapshot = self.call("get", "surveys/snapshot/").json()
        self.assertEqual([s["survey_uuid"] for s in snapshot["surveys"]], ["22222222-2222-2222-2222-222222222222"])

        changes = self.call("get", f"surveys/changes/?cursor={snapshot['cursor']}").json()
        self.assertEqual(changes["changes"], [])

        survey = Survey.objects.get()
        definition = serialize_definition(survey)
        add_question(definition, QUESTION)
        updated = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        self.assertEqual(updated.status_code, 200)
        changes = self.call("get", f"surveys/changes/?cursor={snapshot['cursor']}").json()
        self.assertEqual([c["definition"]["version"] for c in changes["changes"]], [2])
        self.assertFalse(changes["has_more"])

    def test_put_conflict_and_semantic_lock(self):
        survey = create_node_survey(self.node, blank("33333333-3333-3333-3333-333333333333"))[0].survey
        definition = serialize_definition(survey)
        add_question(definition, {**QUESTION, "kind": "single_choice", "data_type": "nominal", "options_text": "A"})
        self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        stale = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": definition})
        self.assertEqual((stale.status_code, stale.json()["current_version"]), (409, 2))

        Question.objects.filter(survey=survey).update(has_received_answer=True)
        definition = serialize_definition(Survey.objects.get(pk=survey.pk))
        definition["questions"][0]["options_text"] = "A\nB"
        locked = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 2, "definition": definition})
        self.assertEqual(locked.status_code, 422)

    def test_invalid_definition_is_400_and_not_stored(self):
        bad = blank("abababab-abab-abab-abab-abababababab")
        bad["questions"] = [{"uuid": "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd", "code": "", "title": "Q", "help_text": "",
                             "kind": "dropdown", "data_type": "text", "options_text": "", "is_required": True,
                             "enable_keyword_tracking": False, "is_active": True, "order": 1}]
        response = self.call("post", "surveys/", bad)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Survey.objects.filter(uuid="abababab-abab-abab-abab-abababababab").exists())

    def test_snapshot_returns_the_frozen_revision(self):
        revision, _ = create_node_survey(self.node, blank("efefefef-efef-efef-efef-efefefefefef"))
        Question.objects.create(survey=revision.survey, title="未經版本化的直接寫入", kind="short_text", data_type="text")
        snapshot = self.call("get", "surveys/snapshot/").json()
        self.assertEqual(snapshot["surveys"], [revision.definition])

    def test_other_nodes_surveys_are_invisible(self):
        survey = create_node_survey(self.other, blank("44444444-4444-4444-4444-444444444444"))[0].survey
        self.assertEqual(self.call("get", "surveys/snapshot/").json()["surveys"], [])
        self.assertEqual(self.call("get", f"surveys/{survey.uuid}/revisions/1/").status_code, 404)
        response = self.call("put", f"surveys/{survey.uuid}/", {"expected_version": 1, "definition": serialize_definition(survey)})
        self.assertEqual(response.status_code, 404)

    def test_invalid_or_foreign_cursor_is_410(self):
        other_cursor = self.call("get", "surveys/snapshot/", token=self.other_token).json()["cursor"]
        self.assertEqual(self.call("get", f"surveys/changes/?cursor={other_cursor}").status_code, 410)
        self.assertEqual(self.call("get", "surveys/changes/?cursor=garbage").status_code, 410)

    def test_heartbeat_reports_node_and_updates_last_seen(self):
        body = self.call("post", "heartbeat/", {}).json()
        self.assertEqual(body["node_uuid"], str(self.node.uuid))
        self.node.refresh_from_db()
        self.assertIsNotNone(self.node.last_seen_at)
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi.tests.test_api --settings=config.settings_test`
Expected: FAIL（404，URL 未掛載）

- [ ] **Step 3: 實作**

```python
# cloudapi/auth.py
import json
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import NodeDevice


class CursorInvalid(Exception):
    pass


class BadRequest(Exception):
    pass


def make_cursor(device, seq):
    return f"{device.uuid}:{int(seq)}"


def parse_cursor(device, text):
    node_part, _, seq_part = (text or "").partition(":")
    if node_part != str(device.uuid) or not seq_part.isdigit():
        raise CursorInvalid(text)
    return int(seq_part)


def read_json(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError as exc:
        raise BadRequest("body 不是有效的 JSON") from exc


def node_api(view):
    """Authenticate the node by bearer token; responses never echo answer content."""

    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not settings.CLOUD_SYNC_PROTOTYPE_ENABLED:
            return JsonResponse({"error": "prototype_disabled"}, status=503)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return JsonResponse({"error": "unauthorized"}, status=401)
        device = NodeDevice.objects.filter(
            token_hash=NodeDevice.hash_token(header[len("Bearer "):].strip()), status=NodeDevice.Status.ACTIVE
        ).first()
        if device is None:
            return JsonResponse({"error": "unauthorized"}, status=401)
        NodeDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now())
        request.node_device = device
        try:
            return view(request, *args, **kwargs)
        except BadRequest as exc:
            return JsonResponse({"error": "bad_request", "message": str(exc)}, status=400)

    return wrapped
```

```python
# cloudapi/views.py
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from feedback.models import Survey

from .auth import CursorInvalid, make_cursor, node_api, parse_cursor, read_json
from .errors import DefinitionError, SemanticLockViolation, VersionConflict
from .models import ChangeClock, SurveyChange, SurveyDefinitionRevision
from .writes import change_definition, create_node_survey

MAX_PAGE = 200


def _owned(request, survey_uuid):
    return Survey.objects.filter(uuid=survey_uuid, owner_node=request.node_device).first()


@node_api
@require_GET
def survey_snapshot(request):
    device = request.node_device
    # Read the committed clock first, then the definitions (spec §4): every change
    # numbered <= cursor is already visible below; newer ones arrive again via changes.
    clock_value = ChangeClock.objects.values_list("value", flat=True).get(pk=1)
    # Never rebuild a definition from live rows here: return the immutable revision of
    # each survey's version, so a reply can't mix a v1 survey with v2 questions.
    versions = dict(Survey.objects.filter(owner_node=device).values_list("pk", "definition_version"))
    revisions = SurveyDefinitionRevision.objects.filter(survey_id__in=versions).order_by("survey_id")
    definitions = [rev.definition for rev in revisions if versions[rev.survey_id] == rev.version]
    return JsonResponse({"cursor": make_cursor(device, clock_value), "surveys": definitions})


@node_api
@require_GET
def survey_changes(request):
    device = request.node_device
    try:
        seq = parse_cursor(device, request.GET.get("cursor", ""))
    except CursorInvalid:
        return JsonResponse({"error": "cursor_invalid"}, status=410)
    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), MAX_PAGE))
    except ValueError:
        return JsonResponse({"error": "bad_request", "message": "limit 必須是整數"}, status=400)
    clock = ChangeClock.objects.get(pk=1)
    if seq < clock.pruned_through or seq > clock.value:
        return JsonResponse({"error": "cursor_invalid"}, status=410)
    rows = list(
        SurveyChange.objects.filter(seq__gt=seq, survey__owner_node=device).order_by("seq")[: limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    revisions = {
        (revision.survey_id, revision.version): revision.definition
        for revision in SurveyDefinitionRevision.objects.filter(
            survey_id__in={row.survey_id for row in rows}, version__in={row.definition_version for row in rows}
        )
    }
    changes = [{"seq": row.seq, "definition": revisions[(row.survey_id, row.definition_version)]} for row in rows]
    next_seq = rows[-1].seq if rows else seq
    return JsonResponse({"changes": changes, "next_cursor": make_cursor(device, next_seq), "has_more": has_more})


@node_api
@require_GET
def survey_revision(request, survey_uuid, version):
    survey = _owned(request, survey_uuid)
    revision = survey and SurveyDefinitionRevision.objects.filter(survey=survey, version=version).first()
    if not revision:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({"definition": revision.definition})


@node_api
@require_POST
def survey_create(request):
    definition = read_json(request)
    try:
        revision, created = create_node_survey(request.node_device, definition)
    except DefinitionError as exc:
        return JsonResponse({"error": "invalid_definition", "message": str(exc)}, status=400)
    except PermissionError:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({"definition": revision.definition}, status=201 if created else 200)


@node_api
@require_http_methods(["PUT"])
def survey_update(request, survey_uuid):
    if _owned(request, survey_uuid) is None:
        return JsonResponse({"error": "not_found"}, status=404)
    body = read_json(request)
    try:
        expected = int(body["expected_version"])
        definition = body["definition"]
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "bad_request", "message": "需要 expected_version 與 definition"}, status=400)
    try:
        revision = change_definition(survey_uuid, expected_version=expected, definition=definition)
    except DefinitionError as exc:
        return JsonResponse({"error": "invalid_definition", "message": str(exc)}, status=400)
    except VersionConflict as exc:
        return JsonResponse({"error": "version_conflict", "current_version": exc.current_version}, status=409)
    except SemanticLockViolation as exc:
        return JsonResponse({"error": "semantic_lock", "question_uuid": exc.question_uuid}, status=422)
    return JsonResponse({"definition": revision.definition})


@node_api
@require_POST
def heartbeat(request):
    return JsonResponse({"node_uuid": str(request.node_device.uuid), "server_time": timezone.now().isoformat()})
```

```python
# cloudapi/urls.py
from django.urls import path

from . import views

app_name = "cloudapi"

urlpatterns = [
    path("surveys/snapshot/", views.survey_snapshot, name="survey-snapshot"),
    path("surveys/changes/", views.survey_changes, name="survey-changes"),
    path("surveys/<uuid:survey_uuid>/revisions/<int:version>/", views.survey_revision, name="survey-revision"),
    path("surveys/<uuid:survey_uuid>/", views.survey_update, name="survey-update"),
    path("surveys/", views.survey_create, name="survey-create"),
    path("heartbeat/", views.heartbeat, name="heartbeat"),
]
```

`config/urls.py` 在 `urlpatterns += [path("", include("feedback.urls"))]` 之前加入：

```python
if not settings.IS_NODE:
    urlpatterns += [path("api/node/v1/", include("cloudapi.urls"))]
```

- [ ] **Step 4: 執行確認通過**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudapi config/urls.py
git commit -m "feat(cloudapi): token-authenticated node API for survey snapshot, changes and versioned writes"
```

---

### Task 5: 雲端網站對同步問卷改用版本化寫入

**Files:**
- Create: `cloudapi/builder.py`
- Create: `templates/feedback/_definition_version_field.html`
- Modify: `feedback/views.py`（`SurveyBuilderView.post`、`SurveyDeleteView.form_valid`、`SurveyCategoryDeleteView.post`）
- Modify: `templates/feedback/survey_builder.html`、`templates/feedback/survey_manager.html`
- Test: `cloudapi/tests/test_builder.py`

**Interfaces:**
- Consumes: Task 2（定義函式、errors）、Task 3（`change_definition`）
- Produces:
  - `cloudapi.builder.builder_post(view, request, commit) -> HttpResponse`：執行編排頁動作；`commit(survey, definition, expected_version)` 由呼叫端提供（雲端呼叫 `change_definition`，Task 8 的本機呼叫 API）
  - `cloudapi.builder.cloud_commit(survey, definition, expected_version) -> None`
  - 表單隱藏欄位 `definition_version`、`question_uuid`

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_builder.py
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Answer, FeedbackSubmission, Question, Survey, SurveyCategory

User = get_user_model()


class CloudBuilderForNodeSurveysTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="m", password="x", role=User.Role.MANAGER)
        self.client.force_login(self.manager)
        self.node, _ = NodeDevice.issue("office")
        self.category = SurveyCategory.objects.create(name="門市")
        survey = Survey.objects.create(title="S", slug="s", category=self.category)
        self.question = Question.objects.create(survey=survey, title="Q", kind="short_text", data_type="text", order=1)
        self.survey = assign_survey_to_node(survey, self.node).survey  # version 1
        self.url = reverse("feedback:survey-builder", args=["s"])

    def post(self, **data):
        return self.client.post(self.url, {"definition_version": self.survey.definition_version, **data})

    def test_builder_form_carries_version_and_question_uuid(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="definition_version" value="1"')
        self.assertContains(page, f'name="question_uuid" value="{self.question.uuid}"')

    def test_delete_without_answers_only_deactivates_and_bumps_version(self):
        self.post(action="delete-question", question_uuid=str(self.question.uuid), question_id=self.question.pk)
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.definition_version, 2)

    def test_stale_form_is_rejected(self):
        self.post(action="restore-question", question_uuid=str(self.question.uuid), question_id=self.question.pk)
        response = self.client.post(
            self.url,
            {"definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid)},
            follow=True,
        )
        self.assertContains(response, "版本不一致，請重新載入")
        self.question.refresh_from_db()
        self.assertTrue(self.question.is_active)

    def test_semantic_edit_of_answered_question_is_refused(self):
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=self.survey), question=self.question, value="x")
        Question.objects.filter(pk=self.question.pk).update(has_received_answer=True)
        response = self.client.post(self.url, {
            "definition_version": 1, "action": "edit-question", "question_uuid": str(self.question.uuid),
            "question_id": self.question.pk, "title": "Q", "help_text": "", "kind": "long_text", "data_type": "text",
            "options_text": "", "is_required": "on", "order": 1,
        }, follow=True)
        self.assertContains(response, "此題已有回覆，請新增題目取代並停用舊題")
        self.question.refresh_from_db()
        self.assertEqual(self.question.kind, "short_text")

    def test_archive_goes_through_a_version(self):
        self.client.post(reverse("feedback:survey-delete", args=["s"]), {"definition_version": 1})
        self.survey.refresh_from_db()
        self.assertIsNotNone(self.survey.archived_at)
        self.assertEqual(self.survey.definition_version, 2)

    def test_deleting_a_category_versions_its_node_surveys(self):
        self.client.post(reverse("feedback:category-delete", args=[self.category.pk]))
        self.survey.refresh_from_db()
        self.assertIsNone(self.survey.category)
        self.assertEqual(self.survey.definition_version, 2)
        self.assertTrue(Survey.objects.filter(pk=self.survey.pk).exists())

    def test_unassigned_surveys_keep_the_old_behaviour(self):
        plain = Survey.objects.create(title="P", slug="p")
        question = Question.objects.create(survey=plain, title="Q", kind="short_text", data_type="text", order=1)
        self.client.post(reverse("feedback:survey-builder", args=["p"]), {"action": "delete-question", "question_id": question.pk})
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())  # hard delete as before
        plain.refresh_from_db()
        self.assertEqual(plain.definition_version, 0)
```

- [ ] **Step 2: 執行確認失敗**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi.tests.test_builder --settings=config.settings_test`
Expected: FAIL（頁面沒有 `definition_version` 欄位；刪題直接硬刪）

- [ ] **Step 3: 實作**

```django
{# templates/feedback/_definition_version_field.html #}
{% if is_node or survey.owner_node_id %}<input type="hidden" name="definition_version" value="{{ survey.definition_version }}">{% endif %}
```

範本修改（逐字替換，可用下列 Python 片段一次完成）：

```python
from pathlib import Path

builder = Path("templates/feedback/survey_builder.html")
text = builder.read_text(encoding="utf-8")
text = text.replace(
    "{% csrf_token %}",
    '{% csrf_token %}{% include "feedback/_definition_version_field.html" %}',
)
text = text.replace(
    '<input type="hidden" name="question_id" value="{{ question.id }}">',
    '<input type="hidden" name="question_id" value="{{ question.id }}">'
    '<input type="hidden" name="question_uuid" value="{{ question.uuid }}">',
)
builder.write_bytes(text.encode("utf-8"))

manager = Path("templates/feedback/survey_manager.html")
text = manager.read_text(encoding="utf-8")
old = """action="{% url 'feedback:survey-delete' survey.slug %}"
                          onsubmit="return confirm('確定要刪除「{{ survey.title }}」？此操作無法復原。')">
                        {% csrf_token %}"""
assert text.count(old) == 1
manager.write_bytes(text.replace(old, old + '{% include "feedback/_definition_version_field.html" %}').encode("utf-8"))
```

```python
# cloudapi/builder.py
"""Survey builder actions for synced (node-owned) surveys.

The page edits a definition dict and hands it to `commit`; the cloud commits
with `change_definition`, the node sends it to the cloud API (cloudsync).
Questions are deactivated, never deleted (spec §2).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from feedback.forms import QuestionCreateForm, SurveyEditForm
from feedback.models import Question

from .definition import (
    add_question,
    archive_survey,
    move_question,
    serialize_definition,
    set_question_active,
    update_question,
    update_survey,
)
from .errors import DefinitionCommitError
from .writes import change_definition


def cloud_commit(survey, definition, expected_version):
    change_definition(survey.uuid, expected_version=expected_version, definition=definition)


def expected_version_from(request):
    try:
        return int(request.POST.get("definition_version", ""))
    except ValueError:
        return None


def builder_post(view, request, commit):
    survey = view.object
    action = request.POST.get("action")
    tab = "settings" if action == "update-survey" else "questions"
    back = redirect(reverse("feedback:survey-builder", args=[survey.slug]) + f"?tab={tab}")
    expected = expected_version_from(request)
    if expected is None:
        messages.error(request, "版本不一致，請重新載入")
        return back
    definition = serialize_definition(survey)
    question_uuid = request.POST.get("question_uuid", "")
    success = "問卷已更新。"

    if action == "move-question":
        move_question(definition, question_uuid, request.POST.get("direction"))
    elif action == "delete-question":
        set_question_active(definition, question_uuid, False)
        success = "題目已停用；同步問卷不刪除題目，歷史資料保留。"
    elif action == "restore-question":
        set_question_active(definition, question_uuid, True)
        success = "題目已恢復，會重新納入填答與分析。"
    elif action == "edit-question":
        question = get_object_or_404(Question, uuid=question_uuid, survey=survey)
        form = QuestionCreateForm(request.POST, instance=question)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(question_form=form, object=survey))
        update_question(definition, question_uuid, form.cleaned_data)
        success = "題目已更新。"
    elif action == "update-survey":
        form = SurveyEditForm(request.POST, instance=survey)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(survey_edit_form=form, object=survey))
        update_survey(definition, form.cleaned_data)
        success = "問卷設定已儲存。"
    else:
        form = QuestionCreateForm(request.POST)
        if not form.is_valid():
            return view.render_to_response(view.get_context_data(question_form=form, object=survey))
        add_question(definition, form.cleaned_data)
        success = "新題目已加入問卷。"

    try:
        commit(survey, definition, expected)
    except DefinitionCommitError as exc:
        messages.error(request, exc.user_message)
    else:
        messages.success(request, success)
    return back


def archive_post(request, survey, commit):
    expected = expected_version_from(request)
    definition = serialize_definition(survey)
    archive_survey(definition, timezone.now())
    try:
        if expected is None:
            raise DefinitionCommitError()
        commit(survey, definition, expected)
    except DefinitionCommitError as exc:
        messages.error(request, exc.user_message if expected is not None else "版本不一致，請重新載入")
        return False
    messages.success(request, f"問卷「{survey.title}」已封存，歷史資料與分析版本均已保留。")
    return True
```

`feedback/views.py`：

1. `SurveyBuilderView.post` 第一行 `self.object = self.get_object()` 之後加入：

```python
        if not settings.IS_NODE and self.object.owner_node_id:
            from cloudapi.builder import builder_post, cloud_commit

            return builder_post(self, request, cloud_commit)
```

2. `SurveyDeleteView.form_valid` 開頭（`survey = self.get_object()` 之後）加入：

```python
        if not settings.IS_NODE and survey.owner_node_id:
            from cloudapi.builder import archive_post, cloud_commit

            archive_post(self.request, survey, cloud_commit)
            return HttpResponseRedirect(self.get_success_url())
```

3. `SurveyCategoryDeleteView.post`，在 `category.delete()` 之前加入：

```python
        node_surveys = list(category.surveys.filter(owner_node__isnull=False))
        if node_surveys:
            from cloudapi.definition import serialize_definition, update_survey
            from cloudapi.writes import change_definition

            with transaction.atomic():
                for survey in node_surveys:
                    definition = serialize_definition(survey)
                    update_survey(definition, {"category": None})
                    change_definition(survey.uuid, expected_version=survey.definition_version, definition=definition)
                category.delete()
            messages.success(request, f"分類「{name}」已刪除。")
            return redirect("feedback:survey-manager")
```

（`transaction` 已在 `feedback/views.py` 匯入；`name = category.name` 這行須移到這段之前。）

- [ ] **Step 4: 執行確認通過與回歸**

Run: `.\.venv\Scripts\python.exe manage.py test cloudapi feedback --settings=config.settings_test`
Expected: PASS（未指派問卷的既有編排頁測試不受影響）

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudapi/builder.py cloudapi/tests/test_builder.py feedback/views.py templates/feedback
git commit -m "feat(cloudapi): versioned builder, archive and category delete for node-owned surveys"
```

---

### Task 6: `cloudsync` app——連線紀錄、權杖保存、HTTP client

**Files:**
- Create: `cloudsync/__init__.py`、`cloudsync/apps.py`、`cloudsync/models.py`、`cloudsync/tokens.py`、`cloudsync/client.py`、`cloudsync/migrations/__init__.py`
- Create: `cloudsync/tests/__init__.py`、`cloudsync/tests/utils.py`、`cloudsync/tests/test_client.py`
- Modify: `config/settings.py`（node 區塊 `INSTALLED_APPS += ["cloudsync"]`、`CLOUD_SYNC_ALLOW_LOOPBACK_HTTP`）
- Modify: `.github/workflows/ci.yml`（node job 標籤加 `cloudsync`）

**Interfaces:**
- Produces:
  - `cloudsync.models.CloudLink`（單例 pk=1：`generation`、`api_url`、`node_uuid`、`cursor`、`last_success_at`、`last_error_kind`、`last_error_message`、`consecutive_failures`、`next_attempt_at`；`CloudLink.load()`；`is_linked`；
    `CloudLink.relink(api_url, node_uuid) -> CloudLink`、`CloudLink.unlink() -> CloudLink`（兩者都遞增 `generation` 並清空游標與錯誤）；
    `CloudLink.update_if_current(generation, **fields) -> bool`（只在世代未變時寫入，否則回 `False`））
  - `cloudsync.models.StaleLink(Exception)`
  - `cloudsync.tokens.save_token(api_url, token)`、`load_token(api_url) -> str | None`、`delete_token(api_url)`
  - `cloudsync.client`：常數 `TRANSIENT="transient"`、`UNAUTHORIZED="unauthorized"`、`GONE="gone"`、`CONFLICT="conflict"`、`SEMANTIC="semantic"`、`CLIENT="client"`；
    `CloudError(kind, message="", *, status=None, retry_after=None, payload=None)`；`classify(status) -> str`；`parse_retry_after(value) -> float | None`；
    `backoff_seconds(failures, retry_after=None) -> float`；`check_api_url(api_url, *, allow_loopback_http=False) -> str`（不合格拋 `ValueError`）；
    `CloudClient(api_url, token, *, session=None, timeout=15, allow_loopback_http=False)` 方法 `get/post/put(path, ...) -> dict`（不跟隨重新導向）；
    `NotLinked(Exception)`；`client_for_link(link=None) -> CloudClient`
  - settings `CLOUD_SYNC_ALLOW_LOOPBACK_HTTP: bool`（預設 `False`；只有隔離測試開啟）
  - 測試工具 `cloudsync.tests.utils.memory_keyring()`（context manager）

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/utils.py
from contextlib import contextmanager

import keyring
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@contextmanager
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
```

```python
# cloudsync/tests/test_client.py
from django.test import SimpleTestCase, TestCase

from cloudsync.client import (
    CLIENT,
    CONFLICT,
    GONE,
    SEMANTIC,
    TRANSIENT,
    UNAUTHORIZED,
    CloudClient,
    CloudError,
    NotLinked,
    backoff_seconds,
    classify,
    client_for_link,
    parse_retry_after,
)
from cloudsync.models import CloudLink
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import delete_token, load_token, save_token


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = b"x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class ClassificationTests(SimpleTestCase):
    def test_status_classes(self):
        self.assertEqual([classify(s) for s in (401, 404, 409, 410, 422, 429, 500, 503, 400)],
                         [UNAUTHORIZED, CLIENT, CONFLICT, GONE, SEMANTIC, TRANSIENT, TRANSIENT, TRANSIENT, CLIENT])

    def test_retry_after_seconds_and_date(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("soon"))
        self.assertGreaterEqual(parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 0)

    def test_backoff_grows_caps_and_respects_retry_after(self):
        self.assertEqual([backoff_seconds(n) for n in (1, 2, 3, 6, 10)], [60, 120, 240, 1800, 1800])
        self.assertEqual(backoff_seconds(1, retry_after=7200), 7200)


class ClientTests(SimpleTestCase):
    def test_sends_bearer_and_parses_json(self):
        session = FakeSession(FakeResponse(200, {"ok": True}))
        body = CloudClient("https://cloud.example/", "tok", session=session).get("surveys/snapshot/")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("GET", "https://cloud.example/api/node/v1/surveys/snapshot/"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(body, {"ok": True})

    def test_error_carries_kind_payload_and_retry_after(self):
        session = FakeSession(FakeResponse(409, {"error": "version_conflict", "current_version": 3}, {"Retry-After": "5"}))
        with self.assertRaises(CloudError) as caught:
            CloudClient("https://c", "t", session=session).put("surveys/x/", {"a": 1})
        error = caught.exception
        self.assertEqual((error.kind, error.payload["current_version"], error.retry_after), (CONFLICT, 3, 5.0))

    def test_https_is_required_except_explicit_loopback(self):
        from cloudsync.client import check_api_url

        self.assertEqual(check_api_url("https://cloud.example/"), "https://cloud.example")
        for bad in ("http://cloud.example", "http://127.0.0.1:8000", "ftp://x", "cloud.example"):
            with self.subTest(bad), self.assertRaises(ValueError):
                check_api_url(bad)
        self.assertEqual(check_api_url("http://127.0.0.1:8000", allow_loopback_http=True), "http://127.0.0.1:8000")
        with self.assertRaises(ValueError):
            check_api_url("http://10.0.0.5", allow_loopback_http=True)
        with self.assertRaises(ValueError):
            CloudClient("http://cloud.example", "t", session=FakeSession(FakeResponse(200)))

    def test_redirects_are_not_followed(self):
        session = FakeSession(FakeResponse(302, {}, {"Location": "http://elsewhere"}))
        with self.assertRaises(CloudError) as caught:
            CloudClient("https://c", "t", session=session).get("x/")
        self.assertEqual(caught.exception.kind, CLIENT)
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_network_failure_is_transient(self):
        import requests

        with self.assertRaises(CloudError) as caught:
            CloudClient("https://c", "t", session=FakeSession(error=requests.ConnectionError("down"))).get("x/")
        self.assertEqual(caught.exception.kind, TRANSIENT)


class LinkAndTokenTests(TestCase):
    def test_tokens_live_in_the_keyring(self):
        with memory_keyring() as backend:
            save_token("https://c", "secret")
            self.assertEqual(load_token("https://c"), "secret")
            self.assertNotIn("secret", str(CloudLink.load().__dict__))
            delete_token("https://c")
            delete_token("https://c")  # idempotent
            self.assertIsNone(load_token("https://c"))

    def test_generation_guards_stale_writers(self):
        link = CloudLink.relink("https://c", "55555555-5555-5555-5555-555555555555")
        old_generation = link.generation
        self.assertTrue(CloudLink.update_if_current(old_generation, cursor="n:1"))
        CloudLink.unlink()
        self.assertFalse(CloudLink.update_if_current(old_generation, cursor="n:9"))
        fresh = CloudLink.load()
        self.assertEqual((fresh.cursor, fresh.is_linked), ("", False))

    def test_client_for_link_requires_link_and_token(self):
        with memory_keyring():
            with self.assertRaises(NotLinked):
                client_for_link()
            CloudLink.relink("https://c", "55555555-5555-5555-5555-555555555555")
            with self.assertRaises(NotLinked):
                client_for_link()
            save_token("https://c", "tok")
            self.assertEqual(client_for_link().token, "tok")
```

- [ ] **Step 2: 執行確認失敗**

Run（node）：`... manage.py test cloudsync --settings=config.settings_test`
Expected: FAIL（`No module named 'cloudsync'`）

- [ ] **Step 3: 實作**

```python
# cloudsync/apps.py
from django.apps import AppConfig


class CloudSyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cloudsync"
    verbose_name = "雲端連線"
```

```python
# cloudsync/models.py
from django.db import models


class StaleLink(Exception):
    """The link was changed (re-linked or disconnected) while a sync was running."""


class CloudLink(models.Model):
    """Singleton (pk=1): where this node syncs to and how the last attempt went. The token lives in keyring.

    `generation` changes on every link or unlink. A running sync remembers the generation it
    started with and writes only through `update_if_current`, so it can never resurrect an old
    link or overwrite a new one's URL, node or cursor.
    """

    generation = models.PositiveIntegerField(default=0)
    api_url = models.URLField(blank=True)
    node_uuid = models.UUIDField(null=True, blank=True)
    cursor = models.CharField(max_length=80, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_kind = models.CharField(max_length=20, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def is_linked(self):
        return bool(self.api_url and self.node_uuid)

    @classmethod
    def _reset(cls, **fields):
        cls.load()
        cls.objects.filter(pk=1).update(
            generation=models.F("generation") + 1,
            cursor="",
            last_error_kind="",
            last_error_message="",
            consecutive_failures=0,
            next_attempt_at=None,
            **fields,
        )
        return cls.load()

    @classmethod
    def relink(cls, api_url, node_uuid):
        return cls._reset(api_url=api_url, node_uuid=node_uuid)

    @classmethod
    def unlink(cls):
        return cls._reset(api_url="", node_uuid=None, last_success_at=None)

    @classmethod
    def update_if_current(cls, generation, **fields):
        return bool(cls.objects.filter(pk=1, generation=generation).update(**fields))
```

```python
# cloudsync/tokens.py
"""Device tokens are kept in the Windows Credential Manager (keyring), never in the database or .env."""

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "FeedbackInsightHub"


def _username(api_url):
    return f"cloud-token:{api_url.rstrip('/')}"


def save_token(api_url, token):
    keyring.set_password(SERVICE, _username(api_url), token)


def load_token(api_url):
    return keyring.get_password(SERVICE, _username(api_url))


def delete_token(api_url):
    try:
        keyring.delete_password(SERVICE, _username(api_url))
    except PasswordDeleteError:
        pass
```

```python
# cloudsync/client.py
"""HTTP client for the cloud node API with the error classes of spec §8."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from django.conf import settings

from .models import CloudLink
from .tokens import load_token

TRANSIENT = "transient"
UNAUTHORIZED = "unauthorized"
GONE = "gone"
CONFLICT = "conflict"
SEMANTIC = "semantic"
CLIENT = "client"
MAX_BACKOFF_SECONDS = 30 * 60


class NotLinked(Exception):
    pass


class CloudError(Exception):
    def __init__(self, kind, message="", *, status=None, retry_after=None, payload=None):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.payload = payload or {}


def classify(status):
    if status == 401:
        return UNAUTHORIZED
    if status == 409:
        return CONFLICT
    if status == 410:
        return GONE
    if status == 422:
        return SEMANTIC
    if status == 429 or status >= 500:
        return TRANSIENT
    return CLIENT


def parse_retry_after(value):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


def backoff_seconds(failures, retry_after=None):
    base = min(60 * 2 ** max(failures - 1, 0), MAX_BACKOFF_SECONDS)
    return max(base, retry_after or 0)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_api_url(api_url, *, allow_loopback_http=False):
    """Bearer tokens travel only over HTTPS; plain HTTP is allowed solely for loopback in isolated tests."""

    parts = urlsplit((api_url or "").strip())
    if parts.scheme == "https" and parts.hostname:
        return api_url.strip().rstrip("/")
    if parts.scheme == "http" and allow_loopback_http and parts.hostname in LOOPBACK_HOSTS:
        return api_url.strip().rstrip("/")
    raise ValueError("雲端網址必須使用 https://")


class CloudClient:
    def __init__(self, api_url, token, *, session=None, timeout=15, allow_loopback_http=False):
        self.base = check_api_url(api_url, allow_loopback_http=allow_loopback_http) + "/api/node/v1/"
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def request(self, method, path, *, params=None, body=None):
        try:
            response = self.session.request(
                method,
                self.base + path,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                allow_redirects=False,  # never resend the bearer token to another location
            )
        except requests.RequestException as exc:
            raise CloudError(TRANSIENT, str(exc)[:200]) from exc
        if 300 <= response.status_code < 400:
            raise CloudError(CLIENT, "unexpected redirect", status=response.status_code)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise CloudError(
                classify(response.status_code),
                str(payload.get("message") or payload.get("error") or response.status_code),
                status=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
                payload=payload,
            )
        return response.json() if response.content else {}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body=None):
        return self.request("POST", path, body=body if body is not None else {})

    def put(self, path, body):
        return self.request("PUT", path, body=body)


def client_for_link(link=None):
    link = link or CloudLink.load()
    if not link.is_linked:
        raise NotLinked()
    token = load_token(link.api_url)
    if not token:
        raise NotLinked()
    return CloudClient(link.api_url, token, allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
```

`config/settings.py` node 區塊 `INSTALLED_APPS += [...]` 那行加上 `"cloudsync"`，並在 node 區塊加入：

```python
    # Bearer tokens go over HTTPS only; isolated end-to-end tests opt in to loopback HTTP.
    CLOUD_SYNC_ALLOW_LOOPBACK_HTTP = os.getenv("CLOUD_SYNC_ALLOW_LOOPBACK_HTTP", "False").lower() == "true"
```

`config/settings_test.py` 不開啟此設定；端對端測試以 `override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)` 局部開啟。產生 migration：
`$env:DEPLOYMENT_MODE='node'; ... manage.py makemigrations cloudsync --settings=config.settings_test`。
CI node job 測試標籤加 `cloudsync`（見 Task 1 Step 7）。

- [ ] **Step 4: 執行確認通過**

Run（node）：`... manage.py test cloudsync --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudsync config/settings.py .github/workflows/ci.yml
git commit -m "feat(cloudsync): cloud link record, keyring token storage and classified HTTP client"
```

---

### Task 7: 本機定義同步與同步週期

**Files:**
- Create: `cloudsync/definitions.py`、`cloudsync/runner.py`
- Modify: `desktop_app/node_runtime.py`（`run_periodically`）、`desktop_app/node_launcher.py`（同步執行緒）
- Modify: `node/status.py`（`cloud_status` 讀 `CloudLink`）、`node/tests/test_status.py`
- Test: `cloudsync/tests/test_definitions.py`、`cloudsync/tests/test_runner.py`、`feedback/test_node_runtime.py`

**Interfaces:**
- Consumes: Task 2（`apply_definition`）、Task 6（client、`CloudLink`）
- Produces:
  - `cloudsync.definitions.upsert_definition(definition) -> (Survey, changed: bool)`
  - `cloudsync.definitions.sync_definitions(client, link) -> int`（套用的定義數）
  - `cloudsync.runner.run_cycle(*, force=False, now=None) -> str`（`"ok"`、`"not_linked"`、`"waiting"`、`"busy"`、`"unauthorized"`、`"stale"` 或 `CloudError.kind`）
  - `upsert_definition` 是定義寫入本機的唯一入口（背景同步、本機編輯回覆、本機建立問卷都用它），本身是鎖定問卷列的單一交易
  - `desktop_app.node_runtime.run_periodically(stop, interval, func) -> None`
  - `node.status.cloud_status(link=None) -> StatusItem`

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_definitions.py
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
```

```python
# cloudsync/tests/test_runner.py
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from cloudsync.client import TRANSIENT, UNAUTHORIZED, CloudError
from cloudsync.models import CloudLink
from cloudsync.runner import run_cycle
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.posts = []

    def post(self, path, body=None):
        if self.error:
            raise self.error
        self.posts.append(path)
        return {}


class RunCycleTests(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        CloudLink.relink("https://c", "88888888-8888-8888-8888-888888888888")
        save_token("https://c", "tok")

    def run_with(self, client, **kwargs):
        with patch("cloudsync.runner.client_for_link", return_value=client), \
             patch("cloudsync.runner.sync_definitions", return_value=0):
            return run_cycle(**kwargs)

    def test_success_records_time_and_clears_errors(self):
        client = FakeClient()
        self.assertEqual(self.run_with(client), "ok")
        link = CloudLink.load()
        self.assertIsNotNone(link.last_success_at)
        self.assertEqual((link.consecutive_failures, link.last_error_kind), (0, ""))
        self.assertEqual(client.posts, ["heartbeat/"])

    def test_transient_failure_backs_off_and_waits(self):
        now = timezone.now()
        self.assertEqual(self.run_with(FakeClient(CloudError(TRANSIENT, retry_after=None)), now=now), TRANSIENT)
        link = CloudLink.load()
        self.assertEqual(link.consecutive_failures, 1)
        self.assertEqual(link.next_attempt_at, now + timedelta(seconds=60))
        self.assertEqual(self.run_with(FakeClient(), now=now + timedelta(seconds=10)), "waiting")
        self.assertEqual(self.run_with(FakeClient(), now=now + timedelta(seconds=10), force=True), "ok")

    def test_unauthorized_stops_until_forced(self):
        self.assertEqual(self.run_with(FakeClient(CloudError(UNAUTHORIZED))), UNAUTHORIZED)
        self.assertEqual(self.run_with(FakeClient()), "unauthorized")
        self.assertEqual(CloudLink.load().last_error_kind, UNAUTHORIZED)

    def test_relink_mid_cycle_does_not_record_old_results(self):
        def relink_then_succeed(client, link):
            CloudLink.relink("https://new", "ffffffff-ffff-ffff-ffff-ffffffffffff")
            return 0

        with patch("cloudsync.runner.client_for_link", return_value=FakeClient()), \
             patch("cloudsync.runner.sync_definitions", side_effect=relink_then_succeed):
            run_cycle()
        fresh = CloudLink.load()
        self.assertEqual(fresh.api_url, "https://new")
        self.assertIsNone(fresh.last_success_at)  # the old run did not stamp the new link

    def test_not_linked(self):
        CloudLink.unlink()
        self.assertEqual(run_cycle(), "not_linked")
```

在 `feedback/test_node_runtime.py` 加入：

```python
class RunPeriodicallyTests(SimpleTestCase):
    def test_runs_until_stopped_and_survives_errors(self):
        import threading

        from desktop_app.node_runtime import run_periodically

        stop = threading.Event()
        calls = []

        def work():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) == 3:
                stop.set()

        run_periodically(stop, 0.01, work)
        self.assertEqual(len(calls), 3)
```

`node/tests/test_status.py` 中 `test_lan_and_cloud_are_off_in_this_release` 的 `cloud_status()` 改為
`cloud_status(CloudLink())`（檔頭 `from cloudsync.models import CloudLink`），並加入：

```python
    def test_cloud_status_reflects_link(self):
        from datetime import timedelta

        from django.utils import timezone

        linked = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999",
                           last_success_at=timezone.now() - timedelta(minutes=3))
        self.assertEqual(cloud_status(linked).state, "ok")
        failing = CloudLink(api_url="https://c", node_uuid="99999999-9999-9999-9999-999999999999",
                            last_error_kind="unauthorized", last_error_message="revoked")
        item = cloud_status(failing)
        self.assertEqual(item.state, "warn")
        self.assertIn("雲端連線已撤銷，請重新連結", item.summary)
```

- [ ] **Step 2: 執行確認失敗**

Run（node）：`... manage.py test cloudsync node.tests.test_status feedback.test_node_runtime --settings=config.settings_test`
Expected: FAIL（模組不存在、`cloud_status` 不接受參數）

- [ ] **Step 3: 實作**

```python
# cloudsync/definitions.py
"""Apply cloud survey definitions to the node's copy (spec §5 定義同步).

`upsert_definition` is the only way a definition reaches the node's models — background
sync, node edits and node survey creation all call it.  Version check, survey and question
writes and the revision are one transaction under a row lock, so a half-applied definition
is never left behind and an older reply can never overwrite a newer one.
"""

import logging

from django.db import IntegrityError, transaction

from cloudapi.definition import apply_definition, validate_definition
from cloudapi.models import SurveyDefinitionRevision
from feedback.models import Survey

from .client import GONE, CloudError
from .models import CloudLink, StaleLink

logger = logging.getLogger(__name__)
PAGE_SIZE = 50


def _free_slug(slug, survey_uuid):
    clash = Survey.objects.select_for_update().filter(slug=slug).exclude(uuid=survey_uuid).first()
    if clash is not None:
        clash.slug = f"{slug[:30]}-local-{clash.pk}"
        clash.save(update_fields=["slug"])
        logger.warning("local survey %s renamed to %s to make room for a synced survey", clash.pk, clash.slug)


def _upsert_locked(definition, version):
    survey = Survey.objects.select_for_update().filter(uuid=definition["survey_uuid"]).first()
    if survey is not None and survey.definition_version >= version:
        return survey, False
    if survey is None or survey.slug != definition["slug"]:
        _free_slug(definition["slug"], definition["survey_uuid"])
    if survey is None:
        survey = Survey(uuid=definition["survey_uuid"])
    apply_definition(survey, definition, version=version)
    SurveyDefinitionRevision.objects.get_or_create(survey=survey, version=version, defaults={"definition": definition})
    return survey, True


def upsert_definition(definition):
    validate_definition(definition)
    version = int(definition["version"])
    try:
        with transaction.atomic():
            return _upsert_locked(definition, version)
    except IntegrityError:
        # Another writer created the same survey uuid first; retry against its row.
        with transaction.atomic():
            return _upsert_locked(definition, version)


def _save_cursor(generation, cursor):
    if not CloudLink.update_if_current(generation, cursor=cursor):
        raise StaleLink()


def _snapshot(client, generation):
    data = client.get("surveys/snapshot/")
    with transaction.atomic():
        applied = sum(upsert_definition(item)[1] for item in data["surveys"])
        _save_cursor(generation, data["cursor"])
    return applied


def sync_definitions(client, link):
    """Pull definition changes. Each page and its cursor commit together; a re-link or
    disconnect during the run raises StaleLink and rolls the current page back."""

    generation = link.generation
    cursor = link.cursor
    if not cursor:
        return _snapshot(client, generation)
    applied = 0
    while True:
        try:
            page = client.get("surveys/changes/", params={"cursor": cursor, "limit": PAGE_SIZE})
        except CloudError as exc:
            if exc.kind != GONE:
                raise
            _save_cursor(generation, "")
            return applied + _snapshot(client, generation)
        with transaction.atomic():
            applied += sum(upsert_definition(change["definition"])[1] for change in page["changes"])
            _save_cursor(generation, page["next_cursor"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return applied
```

```python
# cloudsync/runner.py
"""One sync cycle; the launcher calls it every 5 minutes and the console's 「立即同步」 calls it with force=True."""

import logging
import threading
from datetime import timedelta

from django.utils import timezone

from .client import TRANSIENT, UNAUTHORIZED, CloudError, NotLinked, backoff_seconds, client_for_link
from .definitions import sync_definitions
from .models import CloudLink, StaleLink

logger = logging.getLogger(__name__)
_cycle_lock = threading.Lock()


def _record_success(link, now):
    CloudLink.update_if_current(
        link.generation,
        last_success_at=now,
        last_error_kind="",
        last_error_message="",
        consecutive_failures=0,
        next_attempt_at=None,
    )


def _record_failure(link, error, now):
    failures = link.consecutive_failures + 1 if error.kind == TRANSIENT else link.consecutive_failures
    next_attempt = (
        now + timedelta(seconds=backoff_seconds(failures, error.retry_after)) if error.kind == TRANSIENT else None
    )
    CloudLink.update_if_current(
        link.generation,
        last_error_kind=error.kind,
        last_error_message=str(error)[:255],
        consecutive_failures=failures,
        next_attempt_at=next_attempt,
    )


def run_cycle(*, force=False, now=None):
    now = now or timezone.now()
    link = CloudLink.load()
    if not link.is_linked:
        return "not_linked"
    if not force:
        if link.last_error_kind == UNAUTHORIZED:
            return "unauthorized"
        if link.next_attempt_at and now < link.next_attempt_at:
            return "waiting"
    if not _cycle_lock.acquire(blocking=False):
        return "busy"
    try:
        client = client_for_link(link)
        sync_definitions(client, link)
        client.post("heartbeat/")
    except NotLinked:
        return "not_linked"
    except StaleLink:
        # The link changed while syncing; the new link's next cycle starts cleanly.
        return "stale"
    except CloudError as error:
        logger.warning("cloud sync failed: %s", error.kind)
        _record_failure(link, error, now)
        return error.kind
    else:
        _record_success(link, now)
        return "ok"
    finally:
        _cycle_lock.release()
```

`desktop_app/node_runtime.py` 加入：

```python
def run_periodically(stop, interval, func):
    """Call `func` every `interval` seconds until `stop` is set; one failure never ends the loop."""

    while not stop.is_set():
        try:
            func()
        except Exception:  # noqa: BLE001 - background loop must survive
            logger.exception("periodic task failed")
        if stop.wait(interval):
            return
```

（測試中 `stop.set()` 在第三次呼叫時發生，迴圈在 `stop.wait` 立即返回；`interval=0.01` 讓測試快速完成。）

`desktop_app/node_launcher.py` 的 `run_launcher` 在啟動 `supervise` 執行緒那行之後加入：

```python
    def sync_cycle():
        from cloudsync.runner import run_cycle

        close_old_connections()
        run_cycle()

    threading.Thread(
        target=run_periodically, args=(stop, CLOUD_SYNC_INTERVAL_SECONDS, sync_cycle), name="cloud-sync", daemon=True
    ).start()
```

並在 import 區把 `run_periodically` 加入 `desktop_app.node_runtime` 的匯入、模組常數加 `CLOUD_SYNC_INTERVAL_SECONDS = 300`。

`node/status.py` 的 `cloud_status` 改為：

```python
def cloud_status(link=None):
    from cloudsync.models import CloudLink

    link = CloudLink.load() if link is None else link
    if not link.is_linked:
        return StatusItem("cloud", "雲端連線", "off", "未連線")
    if link.last_error_kind == "unauthorized":
        return StatusItem("cloud", "雲端連線", "warn", "雲端連線已撤銷，請重新連結")
    if link.last_error_kind:
        return StatusItem("cloud", "雲端連線", "warn", f"無法同步（{link.last_error_kind}）")
    if link.last_success_at is None:
        return StatusItem("cloud", "雲端連線", "off", "已連結，尚未同步")
    minutes = int((time.time() - link.last_success_at.timestamp()) // 60)
    return StatusItem("cloud", "雲端連線", "ok", f"已連線 · 上次同步 {minutes} 分鐘前")
```

並在 `PENDING_MESSAGES` 加入 `"cloud": "雲端同步需要處理：請到「雲端連線」查看。"`。

- [ ] **Step 4: 執行確認通過**

Run（node）：`... manage.py test cloudsync node feedback.test_node_runtime --settings=config.settings_test`
Expected: PASS

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudsync desktop_app node/status.py node/tests/test_status.py feedback/test_node_runtime.py
git commit -m "feat(cloudsync): commit-safe definition sync with snapshot fallback, backoff and launcher loop"
```

---

### Task 8: 本機主控台——雲端連線頁與經 API 編輯問卷

**Files:**
- Create: `cloudsync/survey_write.py`、`cloudsync/views.py`、`cloudsync/forms.py`、`cloudsync/urls.py`
- Create: `templates/cloudsync/connection.html`
- Modify: `config/urls.py`（node 模式掛載 `node/cloud/`）
- Modify: `feedback/views.py`（`SurveyCreateView.form_valid`、`SurveyBuilderView.post`、`SurveyDeleteView.form_valid`、分類兩個 view 的 node 分支）
- Modify: `feedback/views.py`（`NODE_CONSOLE_NAV_TAIL` 加雲端連線）、`templates/feedback/_nav_icon.html`（`cloud` 圖示）
- Modify: `node/audit.py`（`CLOUD_LINKED`、`CLOUD_UNLINKED`）
- Test: `cloudsync/tests/test_pages.py`

**Interfaces:**
- Consumes: Task 5（`builder_post`、`archive_post`）、Task 6（client、tokens）、Task 7（`upsert_definition`、`run_cycle`）
- Produces:
  - `cloudsync.survey_write.node_commit(survey, definition, expected_version) -> None`（把 `CloudError`／`NotLinked` 轉成 `DefinitionCommitError` 子類別）
  - `cloudsync.survey_write.create_survey(definition) -> Survey`
  - 例外 `OfflineError`、`NotLinkedError`、`UnauthorizedError`（皆 `DefinitionCommitError`，訊息見 Global Constraints）
  - URL：`cloudsync:connection`（`/node/cloud/`，只限擁有者）

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_pages.py
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloudapi.definition import serialize_definition
from cloudsync.client import CONFLICT, TRANSIENT, CloudError
from cloudsync.models import CloudLink
from cloudsync.survey_write import create_survey, node_commit
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import load_token, save_token
from config.node_paths import NodePaths
from feedback.models import Question, Survey
from organizations.models import Organization, OrganizationMembership

User = get_user_model()


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def _call(self, method, path, body):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        return self.response

    def put(self, path, body):
        return self._call("PUT", path, body)

    def post(self, path, body=None):
        return self._call("POST", path, body)


class NodePageCase(TestCase):
    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        paths = NodePaths(Path(home.name))
        paths.ensure(restrict=lambda _path: None)
        override = override_settings(NODE_PATHS=paths)
        override.enable()
        self.addCleanup(override.disable)
        organization = Organization.objects.create(name="Acme")
        self.owner = User.objects.create_user(username="o@x.com", email="o@x.com", password="x", role=User.Role.MANAGER)
        OrganizationMembership.objects.create(user=self.owner, organization=organization, role="owner")
        self.client.force_login(self.owner)
        CloudLink.relink("https://c", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        save_token("https://c", "tok")
        self.survey = Survey.objects.create(title="S", slug="s", definition_version=1)
        self.question = Question.objects.create(survey=self.survey, title="Q", kind="short_text", data_type="text", order=1)


class NodeCommitTests(NodePageCase):
    def test_success_applies_the_returned_definition(self):
        definition = serialize_definition(self.survey)
        definition["title"] = "新名稱"
        returned = {**definition, "version": 2}
        with patch("cloudsync.survey_write.client_for_link", return_value=FakeClient({"definition": returned})):
            node_commit(self.survey, definition, 1)
        self.survey.refresh_from_db()
        self.assertEqual((self.survey.title, self.survey.definition_version), ("新名稱", 2))

    def test_offline_and_conflict_do_not_touch_the_local_copy(self):
        from cloudapi.errors import VersionConflict
        from cloudsync.survey_write import OfflineError

        definition = serialize_definition(self.survey)
        definition["title"] = "不該寫入"
        for error, expected in ((CloudError(TRANSIENT), OfflineError),
                                (CloudError(CONFLICT, payload={"current_version": 5}), VersionConflict)):
            with patch("cloudsync.survey_write.client_for_link", return_value=FakeClient(error=error)), \
                 self.assertRaises(expected):
                node_commit(self.survey, definition, 1)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.title, "S")


class NodeBuilderTests(NodePageCase):
    def test_builder_writes_through_the_api(self):
        returned = {**serialize_definition(self.survey), "version": 2}
        returned["questions"][0]["is_active"] = False
        fake = FakeClient({"definition": returned})
        with patch("cloudsync.survey_write.client_for_link", return_value=fake):
            self.client.post(reverse("feedback:survey-builder", args=["s"]), {
                "definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid),
                "question_id": self.question.pk,
            })
        self.assertEqual(fake.calls[0][0:2], ("PUT", f"surveys/{self.survey.uuid}/"))
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)

    def test_not_linked_builder_is_read_only(self):
        CloudLink.unlink()
        response = self.client.post(reverse("feedback:survey-builder", args=["s"]), {
            "definition_version": 1, "action": "delete-question", "question_uuid": str(self.question.uuid),
        }, follow=True)
        self.assertContains(response, "尚未連結雲端，問卷唯讀")
        self.question.refresh_from_db()
        self.assertTrue(self.question.is_active)

    def test_create_survey_reuses_its_uuid_after_a_lost_response(self):
        form_page = self.client.get(reverse("feedback:survey-create"))
        survey_uuid = form_page.context["pending_survey_uuid"]
        self.assertContains(form_page, f'name="survey_uuid" value="{survey_uuid}"')
        sent = []

        class LostThenOk:
            def post(self, path, body=None):
                sent.append(body["survey_uuid"])
                if len(sent) == 1:  # the cloud created it, but the reply never arrived
                    raise CloudError(TRANSIENT)
                return {"definition": {**body, "version": 1, "slug": "new-survey"}}

        data = {"title": "New Survey", "description": "", "is_active": "on", "analysis_enabled": "on",
                "survey_uuid": survey_uuid}
        with patch("cloudsync.survey_write.client_for_link", return_value=LostThenOk()):
            first = self.client.post(reverse("feedback:survey-create"), data)
            self.assertContains(first, "離線中，問卷唯讀")
            self.assertContains(first, f'name="survey_uuid" value="{survey_uuid}"')  # same uuid on the retry form
            second = self.client.post(reverse("feedback:survey-create"), data)
        self.assertEqual(sent, [survey_uuid, survey_uuid])
        created = Survey.objects.get(slug="new-survey")
        self.assertEqual(str(created.uuid), survey_uuid)
        self.assertRedirects(second, reverse("feedback:survey-builder", args=["new-survey"]), fetch_redirect_response=False)

    def test_create_survey_rejects_a_malformed_uuid(self):
        response = self.client.post(reverse("feedback:survey-create"), {
            "title": "X", "description": "", "is_active": "on", "analysis_enabled": "on", "survey_uuid": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Survey.objects.filter(title="X").exists())

    def test_category_management_is_cloud_only(self):
        response = self.client.post(reverse("feedback:category-create"), {"name": "新分類"}, follow=True)
        self.assertContains(response, "分類由雲端管理")


class ConnectionPageTests(NodePageCase):
    def test_connect_tests_before_saving(self):
        CloudLink.unlink()
        with patch("cloudsync.views.CloudClient") as client_class:
            client_class.return_value.post.return_value = {"node_uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}
            self.client.post(reverse("cloudsync:connection"), {"action": "connect", "api_url": "https://cloud.example",
                                                                 "token": "fih_new"})
        link = CloudLink.load()
        self.assertEqual((link.api_url, str(link.node_uuid), link.cursor), ("https://cloud.example",
                                                                             "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", ""))
        self.assertEqual(load_token("https://cloud.example"), "fih_new")

    def test_plain_http_url_is_refused(self):
        CloudLink.unlink()
        response = self.client.post(reverse("cloudsync:connection"), {"action": "connect",
                                                                        "api_url": "http://cloud.example", "token": "t"})
        self.assertContains(response, "雲端網址必須使用 https://")
        self.assertFalse(CloudLink.load().is_linked)

    def test_failed_test_saves_nothing(self):
        CloudLink.unlink()
        with patch("cloudsync.views.CloudClient") as client_class:
            client_class.return_value.post.side_effect = CloudError("unauthorized")
            response = self.client.post(reverse("cloudsync:connection"), {"action": "connect",
                                                                            "api_url": "https://cloud.example", "token": "bad"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CloudLink.load().is_linked)
        self.assertIsNone(load_token("https://cloud.example"))

    def test_disconnect_removes_token_and_link(self):
        self.client.post(reverse("cloudsync:connection"), {"action": "disconnect"})
        self.assertFalse(CloudLink.load().is_linked)
        self.assertIsNone(load_token("https://c"))

    def test_admin_is_forbidden(self):
        admin = User.objects.create_user(username="a@x.com", password="x", role=User.Role.MANAGER)
        OrganizationMembership.objects.create(user=admin, organization=Organization.current(), role="admin")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("cloudsync:connection")).status_code, 403)
```

- [ ] **Step 2: 執行確認失敗**

Run（node）：`... manage.py test cloudsync.tests.test_pages --settings=config.settings_test`
Expected: FAIL（模組與網址不存在）

- [ ] **Step 3: 實作**

```python
# cloudsync/survey_write.py
"""Node-side survey edits: the cloud is the only writer (spec §1); the local copy changes only from its reply."""

from cloudapi.errors import DefinitionCommitError, DefinitionError, SemanticLockViolation, VersionConflict

from .client import CLIENT, CONFLICT, SEMANTIC, TRANSIENT, UNAUTHORIZED, CloudError, NotLinked, client_for_link
from .definitions import upsert_definition


class OfflineError(DefinitionCommitError):
    user_message = "離線中，問卷唯讀"


class NotLinkedError(DefinitionCommitError):
    user_message = "尚未連結雲端，問卷唯讀"


class UnauthorizedError(DefinitionCommitError):
    user_message = "雲端連線已撤銷，請重新連結"


def _translate(error):
    if error.kind == TRANSIENT:
        return OfflineError(str(error))
    if error.kind == UNAUTHORIZED:
        return UnauthorizedError(str(error))
    if error.kind == CONFLICT:
        return VersionConflict(error.payload.get("current_version"))
    if error.kind == SEMANTIC:
        return SemanticLockViolation(error.payload.get("question_uuid", ""))
    return DefinitionError(str(error))


def _client():
    try:
        return client_for_link()
    except NotLinked as exc:
        raise NotLinkedError() from exc


def node_commit(survey, definition, expected_version):
    client = _client()
    try:
        reply = client.put(f"surveys/{survey.uuid}/", {"expected_version": expected_version, "definition": definition})
    except CloudError as exc:
        raise _translate(exc) from exc
    upsert_definition(reply["definition"])


def create_survey(definition):
    client = _client()
    try:
        reply = client.post("surveys/", definition)
    except CloudError as exc:
        raise _translate(exc) from exc
    return upsert_definition(reply["definition"])[0]
```

```python
# cloudsync/forms.py
from django import forms


from django.conf import settings

from .client import check_api_url


class ConnectForm(forms.Form):
    api_url = forms.URLField(label="雲端網址", assume_scheme="https")
    token = forms.CharField(label="裝置權杖", widget=forms.PasswordInput, strip=True)

    def clean_api_url(self):
        try:
            return check_api_url(self.cleaned_data["api_url"],
                                 allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
```

```python
# cloudsync/views.py
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from node.audit import CLOUD_LINKED, CLOUD_UNLINKED, record
from node.views import NodeConsoleMixin
from organizations.models import OrganizationMembership

from .client import CloudClient, CloudError
from .forms import ConnectForm
from .models import CloudLink
from .runner import run_cycle
from .tokens import delete_token, save_token


class ConnectionView(NodeConsoleMixin, TemplateView):
    template_name = "cloudsync/connection.html"
    active_section = "cloudsync:connection"
    allowed_roles = (OrganizationMembership.Role.OWNER,)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["link"] = CloudLink.load()
        context.setdefault("form", ConnectForm())
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        link = CloudLink.load()
        if action == "disconnect":
            if link.api_url:
                delete_token(link.api_url)
            CloudLink.unlink()  # bumps the generation, so a running sync cannot write the old link back
            record(CLOUD_UNLINKED, request=request, target=link.api_url)
            messages.success(request, "已中斷雲端連線。")
            return redirect("cloudsync:connection")
        if action == "sync-now":
            result = run_cycle(force=True)
            if result == "ok":
                messages.success(request, "同步完成。")
            else:
                messages.error(request, f"同步未完成：{result}")
            return redirect("cloudsync:connection")
        form = ConnectForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        api_url, token = form.cleaned_data["api_url"], form.cleaned_data["token"]
        try:
            client = CloudClient(api_url, token, allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
            reply = client.post("heartbeat/")
        except CloudError as exc:
            form.add_error(None, f"測試連線失敗：{exc.kind}")
            return self.render_to_response(self.get_context_data(form=form))
        if link.api_url and link.api_url != api_url:
            delete_token(link.api_url)
        save_token(api_url, token)
        # Every (re)link bumps the generation and clears the cursor: the next cycle starts
        # from a snapshot, and any sync still running for the old link stops (spec §5).
        CloudLink.relink(api_url, reply["node_uuid"])
        record(CLOUD_LINKED, request=request, target=api_url)
        messages.success(request, "已連結雲端。")
        return redirect("cloudsync:connection")
```

```python
# cloudsync/urls.py
from django.urls import path

from .views import ConnectionView

app_name = "cloudsync"

urlpatterns = [path("", ConnectionView.as_view(), name="connection")]
```

```django
{# templates/cloudsync/connection.html #}
{% extends "feedback/dashboard_base.html" %}
{% block title %}雲端連線 | FeedBack IQ{% endblock %}
{% block dashboard_content %}
<section class="manager-panel">
    <h2>目前狀態</h2>
    {% if link.is_linked %}
        <p>已連結 <code>{{ link.api_url }}</code>（節點 {{ link.node_uuid }}）</p>
        <p>上次成功同步：{{ link.last_success_at|date:"Y/m/d H:i"|default:"尚未同步" }}</p>
        {% if link.last_error_kind %}<p class="flash-message flash-error">最近錯誤：{{ link.last_error_kind }}　{{ link.last_error_message }}</p>{% endif %}
        <form method="post" class="form-inline">{% csrf_token %}
            <button type="submit" name="action" value="sync-now" class="button button-primary">立即同步</button>
            <button type="submit" name="action" value="disconnect" class="button button-danger"
                    onclick="return confirm('確定中斷雲端連線？')">中斷連線</button>
        </form>
    {% else %}
        <p>尚未連結雲端；問卷在本機為唯讀。</p>
    {% endif %}
</section>
<section class="manager-panel">
    <h2>{% if link.is_linked %}重新連結{% else %}連結雲端{% endif %}</h2>
    <form method="post" class="form-stack">{% csrf_token %}
        {{ form.non_field_errors }}
        {% for field in form %}<label for="{{ field.id_for_label }}">{{ field.label }}</label>{{ field }}{{ field.errors }}{% endfor %}
        <button type="submit" name="action" value="connect" class="button button-primary">測試並儲存</button>
    </form>
</section>
{% endblock %}
```

`node/audit.py` 加入常數與標籤：

```python
CLOUD_LINKED = "cloud.linked"
CLOUD_UNLINKED = "cloud.unlinked"
# ACTION_LABELS 加入：
#     CLOUD_LINKED: "連結雲端",
#     CLOUD_UNLINKED: "中斷雲端連線",
```

`config/urls.py` 的 node 區塊加 `path("node/cloud/", include("cloudsync.urls")),`（放在 `path("node/", include("node.urls"))` 之前）。

`feedback/views.py`：
- `NODE_CONSOLE_NAV_TAIL` 改為 `[("cloudsync:connection", "雲端連線", "cloud"), ("node:settings", "設定", "gear")]`。
- `_nav_icon.html` 在 `{% else %}` 前加 `{% elif name == "cloud" %}<path d="M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.1 9.4 4.3 4.3 0 0 0 7 18z"/>`。
- `SurveyBuilderView.post`（Task 5 加的雲端分支之後）加入：

```python
        if settings.IS_NODE:
            from cloudapi.builder import builder_post
            from cloudsync.survey_write import node_commit

            return builder_post(self, request, node_commit)
```

- `SurveyDeleteView.form_valid`（Task 5 雲端分支之後）加入：

```python
        if settings.IS_NODE:
            from cloudapi.builder import archive_post
            from cloudsync.survey_write import node_commit

            archive_post(self.request, survey, node_commit)
            return HttpResponseRedirect(self.get_success_url())
```

- `SurveyCreateView`：建立操作的 UUID 在**顯示表單時**產生並放進隱藏欄位，送出失敗重新顯示表單時沿用同一個值；
  因此「雲端已建立、回應遺失」後使用者重按，送出的是同一個 UUID，由 API 的冪等保護回傳既有問卷，不會建立第二份。
  在 `SurveyCreateView` 加入 `get_context_data` 的 node 分支（放在既有 `context.update(...)` 之後）：

```python
        if settings.IS_NODE:
            import uuid as uuid_module

            posted = self.request.POST.get("survey_uuid", "") if self.request.method == "POST" else ""
            context["pending_survey_uuid"] = posted or str(uuid_module.uuid4())
```

  `templates/feedback/survey_create.html` 的 `{% csrf_token %}` 後加入：

```django
{% if is_node %}<input type="hidden" name="survey_uuid" value="{{ pending_survey_uuid }}">{% endif %}
```

  `form_valid` 開頭加入：

```python
        if settings.IS_NODE:
            import uuid as uuid_module

            from cloudapi.errors import DefinitionCommitError
            from cloudsync.survey_write import create_survey

            try:
                survey_uuid = str(uuid_module.UUID(self.request.POST.get("survey_uuid", "")))
            except ValueError:
                messages.error(self.request, "表單已過期，請重新開啟建立問卷頁。")
                return self.form_invalid(form)
            data = form.cleaned_data
            definition = {
                "survey_uuid": survey_uuid, "version": 0, "title": data["title"], "slug": "",
                "description": data.get("description", ""), "is_active": data.get("is_active", True),
                "analysis_enabled": data.get("analysis_enabled", True),
                "thank_you_email_enabled": data.get("thank_you_email_enabled", True),
                "improvement_tracking_enabled": True,
                "category": data["category"].name if data.get("category") else None,
                "archived_at": None, "questions": [],
            }
            try:
                self.object = create_survey(definition)
            except DefinitionCommitError as exc:
                messages.error(self.request, exc.user_message)
                return self.form_invalid(form)
            return HttpResponseRedirect(self.get_success_url())
```

- `SurveyCategoryCreateView.post` 與 `SurveyCategoryDeleteView.post` 開頭各加入：

```python
        if settings.IS_NODE:
            messages.info(request, "分類由雲端管理；請在雲端網站新增或刪除分類。")
            return redirect("feedback:survey-manager")
```

- [ ] **Step 4: 執行確認通過與 node 全套件**

Run（node）：`... manage.py test feedback accounts config node organizations cloudapi cloudsync --settings=config.settings_test`
Expected: PASS。若既有 node 模式測試因問卷頁改走 API 而失敗（例如直接 POST 編排頁新增題目的測試），依 Plan A 的分類規則：
該測試驗的是雲端網站行為 → 加 `@cloud_only`；其他失敗視為回歸修正。

- [ ] **Step 5: Commit（需使用者授權）**

```bash
git add cloudsync config/urls.py feedback/views.py templates node/audit.py
git commit -m "feat(cloudsync): owner-only cloud connection page and node survey edits through the cloud API"
```

---

### Task 9: 端對端測試（兩個獨立資料庫經 API）與文件

**Files:**
- Create: `cloudsync/testing.py`
- Test: `cloudsync/tests/test_e2e_definitions.py`
- Modify: `docs/architecture.md`、`docs/next-actions.md`、`README.md`

**Interfaces:**
- Consumes: 全部前述任務
- Produces: `cloudsync.testing.CloudServer`（context manager；屬性 `url`、`token`；方法 `shell(code) -> str`）

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_e2e_definitions.py
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from cloudapi.definition import add_question, serialize_definition, update_question
from cloudapi.errors import SemanticLockViolation, VersionConflict
from cloudsync.models import CloudLink
from cloudsync.runner import run_cycle
from cloudsync.survey_write import node_commit
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback.models import Answer, FeedbackSubmission, Question, Survey

QUESTION = {"title": "滿意度", "help_text": "", "kind": "single_choice", "data_type": "ordinal",
            "options_text": "好\n普通\n差", "is_required": True, "enable_keyword_tracking": False, "order": 2}

SEED = """
from cloudapi.models import NodeDevice
from cloudapi.writes import assign_survey_to_node
from feedback.models import Question, Survey
survey = Survey.objects.create(title="門市問卷", slug="{slug}")
Question.objects.create(survey=survey, title="感想", kind="long_text", data_type="text", order=1)
assign_survey_to_node(survey, NodeDevice.objects.get(name="e2e"))
print(survey.uuid)
"""


class DefinitionSyncEndToEndTests(TestCase):
    """Both tests share one cloud subprocess, so each seeds its own survey (version 1) on the cloud."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._workdir = tempfile.TemporaryDirectory()
        cls.cloud = CloudServer(Path(cls._workdir.name))
        cls.cloud.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.__exit__(None, None, None)
        cls._workdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self._keyring = memory_keyring()
        self._keyring.__enter__()
        self.addCleanup(self._keyring.__exit__, None, None, None)
        # The isolated cloud subprocess listens on plain HTTP loopback; only this test opts in.
        loopback = override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True)
        loopback.enable()
        self.addCleanup(loopback.disable)
        CloudLink.relink(self.cloud.url, self.cloud.node_uuid)
        save_token(self.cloud.url, self.cloud.token)
        slug = self._testMethodName.replace("_", "-")[:40]
        self.survey_uuid = self.cloud.shell(SEED.replace("{slug}", slug)).strip().splitlines()[-1]

    def test_round_trip_conflict_semantic_lock_and_deactivation(self):
        self.assertEqual(run_cycle(force=True), "ok")
        survey = Survey.objects.get(uuid=self.survey_uuid)
        self.assertEqual(survey.definition_version, 1)

        # Node adds a question through the cloud.
        definition = serialize_definition(survey)
        add_question(definition, QUESTION)
        node_commit(survey, definition, 1)
        survey.refresh_from_db()
        self.assertEqual(survey.definition_version, 2)
        self.assertIn("滿意度", self.cloud.shell(
            f"from feedback.models import Survey; print([q.title for q in Survey.objects.get(uuid='{self.survey_uuid}').questions.all()])"))

        # A cloud-side edit makes the node's next write stale.
        self.cloud.shell(f"""
from cloudapi.definition import serialize_definition, update_survey
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{self.survey_uuid}')
d = serialize_definition(s); update_survey(d, {{"title": "雲端改名"}})
change_definition(s.uuid, expected_version=2, definition=d)
""")
        stale = serialize_definition(survey)
        stale["description"] = "本機的修改"
        with self.assertRaises(VersionConflict):
            node_commit(survey, stale, 2)
        run_cycle(force=True)
        survey.refresh_from_db()
        self.assertEqual((survey.title, survey.definition_version), ("雲端改名", 3))

        # Semantic lock on an answered question.
        self.cloud.shell(f"from feedback.models import Question; Question.objects.filter(survey__uuid='{self.survey_uuid}', title='滿意度').update(has_received_answer=True)")
        locked = serialize_definition(survey)
        rated = next(q for q in locked["questions"] if q["title"] == "滿意度")
        update_question(locked, rated["uuid"], {"options_text": "好\n差"})
        with self.assertRaises(SemanticLockViolation):
            node_commit(survey, locked, 3)

        # Cloud deactivates the free-text question; the node keeps its answers.
        free_text = Question.objects.get(survey=survey, title="感想")
        Answer.objects.create(submission=FeedbackSubmission.objects.create(survey=survey), question=free_text, value="好")
        self.cloud.shell(f"""
from cloudapi.definition import serialize_definition, set_question_active
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{self.survey_uuid}')
d = serialize_definition(s)
set_question_active(d, next(q['uuid'] for q in d['questions'] if q['title'] == '感想'), False)
change_definition(s.uuid, expected_version=3, definition=d)
""")
        run_cycle(force=True)
        free_text.refresh_from_db()
        self.assertFalse(free_text.is_active)
        self.assertEqual(Answer.objects.filter(question=free_text).count(), 1)

    def test_interrupted_sync_resumes_without_loss(self):
        run_cycle(force=True)
        cursor_before = CloudLink.load().cursor
        with patch("cloudsync.definitions.upsert_definition", side_effect=RuntimeError("crash")):
            self.cloud.shell(f"""
from cloudapi.definition import serialize_definition, update_survey
from cloudapi.writes import change_definition
from feedback.models import Survey
s = Survey.objects.get(uuid='{self.survey_uuid}')
d = serialize_definition(s); update_survey(d, {{"description": "第二次修改"}})
change_definition(s.uuid, expected_version=s.definition_version, definition=d)
""")
            with self.assertRaises(RuntimeError):
                run_cycle(force=True)
        self.assertEqual(CloudLink.load().cursor, cursor_before)
        self.assertEqual(run_cycle(force=True), "ok")
        self.assertEqual(Survey.objects.get(uuid=self.survey_uuid).description, "第二次修改")
```

（兩個測試共用同一個雲端子程序，各自在 `setUp` 建立自己的雲端問卷，版本都從 1 開始；本機資料庫在每個測試後回滾，
雲端資料庫保留，所以 snapshot 也會帶回另一個測試的問卷，斷言只針對本測試的 `survey_uuid`。）

- [ ] **Step 2: 執行確認失敗**

Run（node）：`... manage.py test cloudsync.tests.test_e2e_definitions --settings=config.settings_test`
Expected: FAIL（`No module named 'cloudsync.testing'`）

- [ ] **Step 3: 實作測試用雲端伺服器**

```python
# cloudsync/testing.py
"""Run the cloud site in a subprocess with its own SQLite file (spec §13 端對端驗收)."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from django.conf import settings

STRIPPED = {"DEPLOYMENT_MODE", "FEEDBACK_HUB_NODE_HOME", "NODE_DATABASE_URL", "DJANGO_SETTINGS_MODULE", "DATABASE_URL"}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CloudServer:
    def __init__(self, workdir, *, startup_timeout=60):
        self.workdir = Path(workdir)
        self.startup_timeout = startup_timeout
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {key: value for key, value in os.environ.items() if key not in STRIPPED}
        self.env.update({
            "DEPLOYMENT_MODE": "cloud",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DATABASE_URL": f"sqlite:///{(self.workdir / 'cloud.sqlite3').as_posix()}",
            "DJANGO_SECRET_KEY": "e2e-cloud-secret-not-for-production",
            "DEBUG": "False",
            "ALLOWED_HOSTS": "127.0.0.1,localhost",
            "CLOUD_SYNC_PROTOTYPE_ENABLED": "True",
            "LOG_LEVEL": "WARNING",
            "PYTHONIOENCODING": "utf-8",
        })
        self.process = None
        self.token = None
        self.node_uuid = None

    def _manage(self, *args):
        result = subprocess.run(
            [sys.executable, "manage.py", *args], cwd=settings.BASE_DIR, env=self.env,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"manage.py {args[0]} failed: {result.stderr[-2000:]}")
        return result.stdout

    def shell(self, code):
        return self._manage("shell", "-c", code)

    def __enter__(self):
        self._manage("migrate", "--noinput")
        self.token = self._manage("create_node_device", "--name", "e2e").strip().splitlines()[-1]
        self.node_uuid = self.shell(
            "from cloudapi.models import NodeDevice; print(NodeDevice.objects.get(name='e2e').uuid)"
        ).strip().splitlines()[-1]
        self.process = subprocess.Popen(
            [sys.executable, "manage.py", "runserver", "--noreload", f"127.0.0.1:{self.port}"],
            cwd=settings.BASE_DIR, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            try:
                if requests.get(f"{self.url}/healthz/", timeout=1).status_code == 200:
                    return self
            except requests.RequestException:
                time.sleep(0.5)
        self.__exit__(None, None, None)
        raise RuntimeError("cloud test server did not start")

    def __exit__(self, *exc):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        return False
```

（子程序不讀本機的 `DATABASE_URL` 等變數；開發機 `.env` 由 `load_dotenv(override=False)` 載入，但這裡明確設定的值優先，不會連到 Supabase。）

- [ ] **Step 4: 執行確認通過**

Run（node）：`... manage.py test cloudsync.tests.test_e2e_definitions --settings=config.settings_test`
Expected: PASS

Run（兩種模式全套件）：
- cloud：`.\.venv\Scripts\python.exe manage.py test feedback accounts config cloudapi --settings=config.settings_test`
- node：`... manage.py test feedback accounts config node organizations cloudapi cloudsync --settings=config.settings_test`
Expected: 兩者 PASS

- [ ] **Step 5: 文件（只寫現況）**

- `docs/architecture.md`「部署模式」一節補一段：問卷定義同步（`cloudapi`／`cloudsync`）、原型閘門 `CLOUD_SYNC_PROTOTYPE_ENABLED`、連結規格。
- `docs/next-actions.md`：「雲端同步」項目改為「C1 已完成；接續 C2 收件匣、C3 結果、C4 搬移」。
- `README.md` 本機節點段落加一句：在主控台「雲端連線」輸入雲端網址與裝置權杖（雲端以 `manage.py create_node_device --name <名稱>` 產生，只顯示一次）。

- [ ] **Step 6: Commit（需使用者授權）**

```bash
git add cloudsync docs README.md
git commit -m "test(cloudsync): end-to-end definition sync between isolated cloud and node databases"
```
