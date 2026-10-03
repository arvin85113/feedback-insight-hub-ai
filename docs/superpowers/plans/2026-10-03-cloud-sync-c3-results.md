# 雲端同步 C3：結果上傳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 雲端同步問卷在本機發布的分析結果以不可變身分上傳雲端；雲端只保存歷史中繼資料，只在序號、輸入水位、定義版本都不倒退時原子切換 `SurveyAnalysisState`，網站依「定義／輸入／管線」三項標示是否最新。

**Architecture:** 本機分析以「水位凍結＋版本作廢」綁定輸入（規格 §7，2026-10-03 修訂）。本機在既有發布函式的同一交易、同一列鎖內建立 `ResultUpload`（身分與內容當下凍結），同步週期以 UTF-8 標準 JSON 上傳；雲端 `apply_upload` 在 `SurveyAnalysisState` 列鎖內驗證、記錄歷史並比較後切換。網站對指派給節點的問卷一律以上傳內容與三項新鮮度呈現。

**Tech Stack:** Django 6.0.8、requests 2.32.5、SQLite／PostgreSQL 17（併發測試）。

**Spec:** [雲端同步規格](../specs/2026-10-01-cloud-sync-design.md)（§1 已分析／尚未分析／排除／雲端既有、§3 `PublishedResultRecord` 與 `SurveyAnalysisState` 擴充、§4 `results/` 與心跳 `publish_sequence`、§5 `ResultUpload` 與補排、§7（含 2026-10-03 修訂）、§8 結果雜湊衝突、§13 對應測試）。

**前置：** C1、C2 已合併於 `main`。分支 `feat/cloud-sync-c3`。審查紀錄：本計畫經兩輪獨立審查（Opus 5.5），本版已納入兩輪的 Critical／Important 意見與部分 Minor。

**不在本計畫：** C4 搬移；網站從上傳的 AI 結果匯入改善草稿（雲端只讀顯示）；外部資料來源問卷的結果上傳（不符合版本作廢前提，規格 §7）；加密；通知系統。已知風險（不處理）：回覆持續流入時，每個同步週期都會遞增 `input_version`，執行較久的分析可能反覆作廢。

## Global Constraints

- **上傳範圍**：只在 node 模式，且 `cloudsync.scope.is_cloud_synced(survey)` 為真時建立、補排、上傳結果與套用水位過濾。判斷：本機有該問卷的 `SurveyDefinitionRevision`（由同步寫入）**且** `resolve_analysis_source(survey).kind == AnalysisJob.SourceKind.ANSWERS`；`resolve_analysis_source` 拋 `AnalysisSourceConfigurationError` 時視為 `False`。
- **水位前進即排程**：`cloudsync.models.advance_and_schedule(survey) -> int` 在呼叫端交易內呼叫 `SurveySyncState.advance`，W 增加時（**不在** `suppress_analysis_scheduling` 範圍內）呼叫 `schedule_survey_analysis(survey.pk, change="input")`。C2 的 `intake` 與 runner 的放棄處理都改用它。
- **發布身分**：`publish_uuid`、`publish_sequence`、`content_hash` 建立時決定，重送沿用，**不因任何拒絕改號**。取號在 `SurveyAnalysisState` 列鎖之後鎖定 `SurveySyncState`：`max(cloud_publish_sequence, local_publish_sequence) + 1`，同交易寫回 `local_publish_sequence`。
- **內容**（`content` 先做 `json.loads(json.dumps(...))` 往返後再算 `content_hash = cloudapi.envelope.sha256_hex(content)`）鍵固定：
  `{"survey_uuid", "definition_version", "analyzed_through_sequence", "input_fingerprint", "published_at", "pipeline": {"input_version", "config_version", "pipeline_version", "implementation_version"}, "stages": {"statistics": STAGE, "text": STAGE, "ai": STAGE}, "display_payload": dict, "ai_payload": dict | None, "ai_source": dict, "coverage": {"analyzed_unique": int, "excluded": {"voided": int, "incomplete": int}}}`
  - `STAGE = {"current": bool, "input_version": int | None, "config_version": int | None, "pipeline_version": str | None}`，取自 `publication_manifest[段]`；`ai` 另含 `"model_name"`。
  - `statistics`／`text` 的 `current`：manifest 該段存在且 `snapshot_id == state.published_snapshot_id`。
  - `ai` 的 `current`：manifest `ai` 存在、`snapshot_id == state.published_snapshot_id`，且三個版本等於 `statistics` 段；否則 `current=False`、`ai_payload=None`、`ai_source={}`。
  - `input_fingerprint`＝已發布 Snapshot 的 `data_fingerprint`；`implementation_version`＝其 `data_scope.pipeline_implementation_version`；水位、定義版本、`excluded` 取自其 `data_scope`；`analyzed_unique`＝其 `response_count`。
  - `excluded`：已作廢優先計數，未作廢中再計未完成。
  - 沒有已發布 Snapshot 時：水位、定義版本、`analyzed_unique` 為 0，`excluded` 兩項為 0，`input_fingerprint` 與 `implementation_version` 為空字串。
- **傳輸**：本機以 `cloudapi.envelope.canonical_bytes(body)`（UTF-8、不轉義中文）送出；`CLOUD_RESULT_MAX_BYTES = 4 * 1024 * 1024`（兩端共用設定名）。本機超過上限時不送，標 `failed`、`last_error="too_large"`。雲端 results view 先看 `CONTENT_LENGTH`，再以 `request.read(CLOUD_RESULT_MAX_BYTES + 1)` 讀取（不經 `request.body`，避開 `DATA_UPLOAD_MAX_MEMORY_SIZE`），超過回 413。
- **雲端回應**：HTTP 201（新建歷史）／200 `{"status": "applied" | "stale"}`。同 `publish_uuid` 且雜湊、序號相同：不新建，`state.published_upload_uuid == publish_uuid` 回 `applied`，否則 `stale`。雜湊或序號不同 → 409 `{"error": "content_conflict"}`，並在**交易結束後**以獨立更新 `F("conflict_count") + 1`（不被回滾）。問卷不屬於節點 → 404；驗證失敗 → 400。
- **雲端驗證**（不合格 400）：`publish_uuid`、`content.survey_uuid` 為合法 UUID；`publish_sequence ≥ 1` 且所有整數欄位是 `int` 而非 `bool`；`analyzed_through_sequence ≤ survey.response_sequence`；`definition_version ≤ survey.definition_version`；`published_at` 可解析；`display_payload`／`ai_payload` 經 `feedback.analysis_jobs._bounded_json`。
- **歷史**：`PublishedResultRecord` 不存內容（身分、序號、雜湊、水位、定義版本、`applied`、`conflict_count`、`received_at`）。
- **切換條件**（全部成立）：`publish_sequence > state.publish_sequence`、`analyzed_through_sequence >= state.analyzed_through_sequence`、`definition_version >= state.definition_version`。
- **本機上傳狀態**：`pending` → `uploaded`（applied）／`stale`；`CONFLICT`、`CLIENT`、`GONE`、`SEMANTIC` → `failed`（`last_error` 記類別；HTTP 404 記 `not_found`，主控台說明「雲端找不到這份問卷，可能已改連其他雲端或取消指派」）；`TRANSIENT`、`UNAUTHORIZED` → 保持 `pending`、`attempts += 1` 並重新拋出（沿用 runner 的退避與停止）。主控台可把 `failed` 改回 `pending`（「重新上傳」）。上傳順序 `("publish_sequence", "pk")`。
- **同步週期順序**：`sync_definitions` → `sync_inbox` → 心跳（套用 `abandoned_sequences`、`publish_sequence`、`inbox_status`）→ `backfill_publications` → `upload_results`。
- **網站新鮮度**（只對 `survey.owner_node_id` 已設定的問卷）：定義＝`state.definition_version == survey.definition_version`；輸入＝無 `response_sequence > state.analyzed_through_sequence` 且狀態不是 `abandoned` 的收據（含 `quarantined`）；管線＝本機申報值。尚無上傳時 `is_latest=False`。
- **使用者可見訊息（逐字）**：「本機發布 #N」、「尚無本機發布結果」、「問卷已變更，結果為舊版本」、「有 N 筆新回覆尚未分析」、「雲端既有 N 筆未納入分析」、「管線版本由本機申報」、「N 份結果上傳失敗」、「結果內容衝突 N 次」。
- 測試指令同 C2；雲端行為測試加 `cloud_only`、本機行為測試在 node 模式執行。commit／push／PR 依 AGENTS.md 取得授權。

## Review Focus

1. **兩份結果同時上傳**：以強制交錯的 PostgreSQL 測試驗證列鎖序列化（Task 3）。
2. **新統計發布後仍帶舊 AI**：上傳內容 `stages.ai.current=False`、不附 AI 內容（Task 2）。
3. **分析兩次讀取之間發生變更**：不推進水位的新回覆不入本次輸入、結果照常發布；推進水位的新回覆使本次作廢、下一次包含它；詞典或題目修改使結果不發布（Task 1）。
4. **上傳回應遺失後重送、從備份還原後重送舊結果**：沿用原身分；仍在展示中者回 `applied`，已被取代者回 `stale`（Task 3、Task 5、Task 6）。
5. **已指派但尚未有本機結果的問卷**：網站不顯示「符合目前資料版本」（Task 4）。


---

### Task 1: 上傳範圍、水位綁定輸入與重新排程（node）

**Files:**
- Create: `cloudsync/scope.py`、`cloudsync/capture.py`
- Modify: `cloudsync/models.py`（`advance_and_schedule`）、`cloudsync/inbox.py`（`intake` 改用它、移除迴圈尾排程）、`cloudsync/runner.py`（`_apply_abandoned` 改用它）
- Modify: `feedback/analysis_adapters.py`（`AnswerInput.from_survey(..., extra_filter=None)`，`scan` 兩條路徑都套用）、`feedback/analysis_worker.py`（`_build_adapter`、`build_result` 的 `data_scope`、`_persist_snapshot` 的 `source_latest_at`）
- Test: `cloudsync/tests/test_capture.py`

**Interfaces:**
- Consumes: C2 `SurveySyncState`、`SyncedSubmissionSource`、`intake`
- Produces:
  - `cloudsync.scope.is_cloud_synced(survey) -> bool`
  - `cloudsync.capture.capture_scope(survey) -> CaptureScope`（dataclass：`watermark`、`definition_version`、`submission_filter: Q`、`excluded: {"voided", "incomplete"}`）；`submission_filter = Q(synced_source__isnull=True) | Q(synced_source__response_sequence__lte=W)`
  - `cloudsync.models.advance_and_schedule(survey) -> int`
  - node 模式且 `is_cloud_synced` 時：adapter 版本字串加 `:watermark:<W>`、`adapter.capture_scope` 有值、Snapshot `data_scope` 加 `analyzed_through_sequence`、`definition_version`、`excluded`；`source_latest_at` 套用同一 filter。其餘情況行為不變。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_capture.py
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from cloudsync.capture import capture_scope
from cloudsync.definitions import upsert_definition
from cloudsync.inbox import intake
from cloudsync.models import SurveySyncState
from cloudsync.runner import _apply_abandoned
from cloudsync.scope import is_cloud_synced
from cloudsync.tests.test_inbox import Q1, U1, U2, U3, FakeInboxClient, definition, envelope
from feedback.analysis_adapters import AnswerInput
from feedback.models import AnalysisJob, FeedbackSubmission, KeywordCategory, Question, Survey, SurveyAIReportSnapshot


class ScopeTests(TestCase):
    def test_only_synced_answer_surveys_are_in_scope(self):
        synced, _ = upsert_definition(definition(1))
        local = Survey.objects.create(title="L", slug="local")
        self.assertEqual((is_cloud_synced(synced), is_cloud_synced(local)), (True, False))


class CaptureTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([]))  # gap at 2 keeps W at 1
        FeedbackSubmission.objects.create(survey=self.survey, is_complete=False)
        voided = FeedbackSubmission.objects.create(survey=self.survey, is_complete=False)
        FeedbackSubmission.objects.filter(pk=voided.pk).update(voided_at="2026-10-01T00:00:00+00:00")

    def test_scope_freezes_the_watermark_and_counts_exclusions_once(self):
        scope = capture_scope(self.survey)
        self.assertEqual((scope.watermark, scope.definition_version), (1, 1))
        self.assertEqual(scope.excluded, {"voided": 1, "incomplete": 1})

    def test_adapter_reads_only_up_to_the_frozen_watermark(self):
        scope = capture_scope(self.survey)
        intake(envelope(2, U2, {Q1: "b"}), FakeInboxClient([]))
        adapter = AnswerInput.from_survey(Survey.objects.get(pk=self.survey.pk), version="v",
                                          extra_filter=scope.submission_filter)
        name = next(iter(adapter.question_fields.values())).name
        self.assertEqual([row[name] for row in adapter.scan([name])], ["a"])

    def test_watermark_advance_schedules_analysis(self):
        AnalysisJob.objects.all().delete()
        state = SurveySyncState.objects.get(survey=self.survey)
        _apply_abandoned({"surveys": [{"survey_uuid": str(self.survey.uuid), "abandoned_sequences": [2]}]})
        state.refresh_from_db()
        self.assertEqual(state.synced_through_sequence, 3)
        self.assertTrue(AnalysisJob.objects.filter(survey=self.survey, status="pending").exists())


class InterleavingTests(TestCase):
    """Mutations between the statistics and text reads of one worker run (spec §13 新鮮度)."""

    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        self.output = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.output.cleanup)

    def run_worker_with(self, mutate):
        from feedback import analysis_worker

        original = analysis_worker.calculate_text

        def text_with_mutation(*args, **kwargs):
            mutate()
            return original(*args, **kwargs)

        with patch.object(analysis_worker, "calculate_text", text_with_mutation):
            call_command("run_analysis_worker_once", worker_id="t", output=self.output.name)

    def published_scope(self):
        snapshot = SurveyAIReportSnapshot.objects.filter(survey=self.survey).order_by("-pk").first()
        state = self.survey.analysis_state
        state.refresh_from_db()
        return state.published_snapshot_id == getattr(snapshot, "pk", None), snapshot

    def test_reply_that_does_not_advance_the_watermark_is_excluded(self):
        self.run_worker_with(lambda: intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([])))  # gap at 2: W stays 1
        published, snapshot = self.published_scope()
        self.assertTrue(published)
        self.assertEqual((snapshot.response_count, snapshot.source_snapshot["data_scope"]["analyzed_through_sequence"]), (1, 1))

    def test_reply_that_advances_the_watermark_voids_this_run_and_the_next_includes_it(self):
        self.run_worker_with(lambda: intake(envelope(2, U2, {Q1: "b"}), FakeInboxClient([])))  # W 1 -> 2
        self.assertFalse(self.published_scope()[0])
        self.assertTrue(AnalysisJob.objects.filter(survey=self.survey, status="pending").exists())
        call_command("run_analysis_worker_once", worker_id="t", output=self.output.name)
        published, snapshot = self.published_scope()
        self.assertTrue(published)
        self.assertEqual((snapshot.response_count, snapshot.source_snapshot["data_scope"]["analyzed_through_sequence"]), (2, 2))

    def test_dictionary_change_blocks_publication(self):
        self.run_worker_with(lambda: KeywordCategory.objects.create(survey=self.survey, keyword="甜", category="口味"))
        self.assertFalse(self.published_scope()[0])

    def test_question_change_blocks_publication(self):
        def rename():
            question = Question.objects.get(uuid=Q1)
            question.title = "新題名"
            question.save()  # post_save signal bumps the version in the same transaction

        self.run_worker_with(rename)
        self.assertFalse(self.published_scope()[0])
```

（`run_analysis_worker_once` 已確認可在 `TestCase` 內執行；如需外部輸入參數，沿用 `feedback/test_analysis_worker.py` 的呼叫方式。）

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_capture`；Expected: FAIL（`No module named 'cloudsync.capture'`）
- [ ] **Step 3: 實作**（見 Interfaces 與 Global Constraints「水位前進即排程」；`intake` 改為 `with transaction.atomic():` 內先 `with suppress_analysis_scheduling():` 寫入，離開 suppress 後在同一交易呼叫 `advance_and_schedule`）
- [ ] **Step 4: 執行確認通過與回歸** — Run（node）：`manage.py test cloudsync feedback.test_analysis_worker`；cloud：`manage.py test feedback.test_analysis_worker`；Expected: PASS
- [ ] **Step 5: Commit** — `feat(cloudsync): bind node analysis input to the reply watermark and reschedule on advance`

---

### Task 2: 本機發布紀錄、內容與取號（node）

**Files:**
- Modify: `cloudsync/models.py`（`ResultUpload`；`SurveySyncState.cloud_publish_sequence`、`local_publish_sequence`）、migration `cloudsync/0004`
- Create: `cloudsync/results.py`
- Modify: `feedback/analysis_jobs.py`（`publish_analysis_snapshot`、`publish_analysis_stages` 在 `state.save(...)` 後、同一交易與列鎖內：`if settings.IS_NODE: from cloudsync.results import record_publication; record_publication(state)`）
- Test: `cloudsync/tests/test_results_local.py`

**Interfaces:**
- Consumes: Task 1 `is_cloud_synced`、Snapshot `data_scope`
- Produces:
  - `ResultUpload`：`publish_uuid`（`UUIDField(unique=True, default=uuid4)`）、`survey`、`publish_sequence`、`content_hash`、`content`、`published_at`、`status`（`pending`／`uploaded`／`stale`／`failed`）、`attempts`、`last_error`（`CharField(32, blank=True)`）、`created_at`
  - `SurveySyncState.cloud_publish_sequence`、`local_publish_sequence`
  - `cloudsync.results.build_content(state) -> dict`（規則見 Global Constraints）
  - `cloudsync.results.record_publication(state) -> ResultUpload | None`（不在範圍內回 `None`；呼叫端須已鎖 `state`）
  - `cloudsync.results.backfill_publications() -> int`：逐問卷在交易內 `select_for_update` 取得 state，鎖內確認 `published_at` 且沒有相同 `published_at` 的 `ResultUpload` 才建立

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_results_local.py
from django.test import TestCase
from django.utils import timezone

from cloudapi.envelope import sha256_hex
from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload, SurveySyncState
from cloudsync.results import backfill_publications, build_content, record_publication
from cloudsync.tests.test_inbox import definition
from feedback.models import Survey, SurveyAnalysisState


def published_state(survey, *, ai_snapshot_id=None, snapshot_id=None):
    manifest = {"statistics": {"snapshot_id": snapshot_id, "input_version": 1, "config_version": 1, "pipeline_version": "p"},
                "text": {"snapshot_id": snapshot_id, "input_version": 1, "config_version": 1, "pipeline_version": "p"}}
    if ai_snapshot_id is not None:
        manifest["ai"] = {"snapshot_id": ai_snapshot_id, "input_version": 1, "config_version": 1,
                          "pipeline_version": "p", "model_name": "m"}
    # upsert_definition already created the state through the analysis-scheduling signals.
    state, _ = SurveyAnalysisState.objects.update_or_create(survey=survey, defaults={
        "published_at": timezone.now(), "publication_manifest": manifest,
        "published_display_payload": {"statistics": {"charts": []}, "text_analysis": {}},
        "published_ai_payload": {"summary": "舊的 AI"} if ai_snapshot_id is not None else {},
    })
    return state


class RecordPublicationTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))

    def test_identity_and_content_are_frozen_at_creation(self):
        state = published_state(self.survey)
        upload = record_publication(state)
        self.assertEqual((upload.status, upload.publish_sequence), ("pending", 1))
        self.assertEqual(upload.content_hash, sha256_hex(upload.content))
        SurveyAnalysisState.objects.filter(pk=state.pk).update(published_display_payload={"changed": True})
        upload.refresh_from_db()
        self.assertEqual(upload.content_hash, sha256_hex(upload.content))

    def test_old_ai_is_not_sent_with_new_statistics(self):
        state = published_state(self.survey, snapshot_id=None, ai_snapshot_id=999)  # AI from another snapshot
        content = build_content(state)
        self.assertEqual((content["stages"]["ai"]["current"], content["ai_payload"], content["ai_source"]), (False, None, {}))

    def test_sequence_continues_after_cloud(self):
        record_publication(published_state(self.survey))
        SurveySyncState.objects.filter(survey=self.survey).update(cloud_publish_sequence=7)
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(record_publication(state).publish_sequence, 8)

    def test_out_of_scope_surveys_create_nothing(self):
        local = Survey.objects.create(title="L", slug="local")
        self.assertIsNone(record_publication(published_state(local)))

    def test_backfill_creates_missing_upload_once(self):
        published_state(self.survey)
        self.assertEqual((backfill_publications(), backfill_publications()), (1, 0))
        self.assertEqual(ResultUpload.objects.count(), 1)
```

在 `feedback/test_analysis_jobs.py` 加 node 限定（`skipUnless(settings.IS_NODE, ...)`）測試 `test_node_publish_creates_result_upload_in_the_same_transaction`：同步問卷走一次 `publish_analysis_snapshot` 成功路徑 → `ResultUpload` 一筆 `pending`；被判定 stale 的發布不新增。

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_results_local`；Expected: FAIL（`ResultUpload` 不存在）
- [ ] **Step 3: 實作**
- [ ] **Step 4: 執行確認通過** — Run（node）：`manage.py test cloudsync feedback.test_analysis_jobs`；cloud：`manage.py test feedback.test_analysis_jobs`；node `makemigrations --check` 無變更
- [ ] **Step 5: Commit** — `feat(cloudsync): freeze each node publication as a result upload`

---

### Task 3: 雲端結果歷史、驗證與原子切換

**Files:**
- Modify: `cloudapi/models.py`（`PublishedResultRecord`）、`feedback/models.py`（`SurveyAnalysisState` 四欄位）、migrations `feedback/0022`、`cloudapi/0003`
- Create: `cloudapi/results.py`
- Modify: `cloudapi/views.py`、`cloudapi/urls.py`（`results/`）、`cloudapi/inbox.py`（`survey_sequences` 加 `publish_sequence`）、`config/settings.py`（`CLOUD_RESULT_MAX_BYTES`）
- Test: `cloudapi/tests/test_results.py`、`cloudapi/test_postgres.py`

**Interfaces:**
- Produces:
  - `SurveyAnalysisState.publish_sequence`、`analyzed_through_sequence`、`definition_version`（預設 0）、`published_upload_uuid`（可空）
  - `PublishedResultRecord`：`publish_uuid`（唯一）、`node`、`survey`（`PROTECT`）、`publish_sequence`、`content_hash`、`definition_version`、`analyzed_through_sequence`、`applied`、`conflict_count`、`received_at`
  - `cloudapi.results.apply_upload(node, *, publish_uuid, publish_sequence, content_hash, content) -> tuple[str, bool]`；例外 `ResultConflict`、`ResultInvalid(message)`、`PermissionError`
  - `POST results/`、心跳 `surveys[].publish_sequence`

`apply_upload` 順序（一個交易）：驗證（Global Constraints；`publish_uuid` 以 `uuid.UUID(...)` 解析一次，之後的查詢、寫入與比較都用解析後的值；問卷以合法 UUID 查 `owner_node=node`）→ `get_or_create` 後 `select_for_update` 鎖 `SurveyAnalysisState` → 同 `publish_uuid` 已存在：雜湊與序號都同回 `("applied" if state.published_upload_uuid == publish_uuid else "stale", False)`；否則在交易內記下衝突、結束交易後以 `PublishedResultRecord.objects.filter(pk=...).update(conflict_count=F("conflict_count") + 1)` 遞增，再拋 `ResultConflict` → 建歷史 → 鎖內比較三條件 → 成立則寫 `published_display_payload`、`published_ai_payload`（`None` → `{}`）、`publication_manifest = {"source": "node", "publish_uuid", "stages", "pipeline", "coverage", "ai_source", "input_fingerprint"}`、`published_at`、`published_snapshot=None`、`published_ai_stage=None`、四個新欄位，`record.applied=True`。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_results.py
import json
import uuid

from django.test import TestCase, override_settings

from cloudapi.envelope import canonical_bytes, sha256_hex
from cloudapi.models import NodeDevice, PublishedResultRecord
from cloudapi.results import ResultConflict, ResultInvalid, apply_upload
from cloudapi.writes import assign_survey_to_node
from feedback.models import Survey, SurveyAnalysisState
from feedback.test_utils import cloud_only

STAGE = {"current": True, "input_version": 1, "config_version": 1, "pipeline_version": "p"}


def content(survey, *, watermark=1, version=1, title="v"):
    return {"survey_uuid": str(survey.uuid), "definition_version": version, "analyzed_through_sequence": watermark,
            "input_fingerprint": "f", "published_at": "2026-10-03T00:00:00+00:00",
            "pipeline": {"input_version": 1, "config_version": 1, "pipeline_version": "p", "implementation_version": "i"},
            "stages": {"statistics": STAGE, "text": STAGE, "ai": {**STAGE, "current": False, "model_name": None}},
            "display_payload": {"statistics": {"title": title}, "text_analysis": {}}, "ai_payload": None,
            "ai_source": {}, "coverage": {"analyzed_unique": 3, "excluded": {"voided": 0, "incomplete": 0}}}


@cloud_only
class ApplyUploadTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(response_sequence=5)

    def upload(self, sequence, body, publish_uuid=None):
        return apply_upload(self.node, publish_uuid=str(publish_uuid or uuid.uuid4()), publish_sequence=sequence,
                            content_hash=sha256_hex(body), content=body)

    def test_newer_upload_switches_the_display(self):
        self.assertEqual(self.upload(1, content(self.survey, title="first")), ("applied", True))
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual((state.publish_sequence, state.published_display_payload["statistics"]["title"]), (1, "first"))
        self.assertIsNone(state.published_snapshot_id)

    def test_stale_upload_keeps_history_only(self):
        self.upload(5, content(self.survey, title="five"))
        for sequence, body in ((4, content(self.survey, title="older")),
                               (6, content(self.survey, title="lower watermark", watermark=0)),
                               (7, content(self.survey, title="older definition", version=0))):
            with self.subTest(sequence):
                self.assertEqual(self.upload(sequence, body)[0], "stale")
        self.assertEqual(SurveyAnalysisState.objects.get(survey=self.survey).published_display_payload["statistics"]["title"], "five")
        self.assertEqual(PublishedResultRecord.objects.count(), 4)

    def test_resend_is_idempotent_and_mismatch_conflicts(self):
        publish_uuid = uuid.uuid4()
        body = content(self.survey)
        self.upload(1, body, publish_uuid)
        self.assertEqual(self.upload(1, body, publish_uuid), ("applied", False))
        self.upload(2, content(self.survey, title="newer"))
        self.assertEqual(self.upload(1, body, publish_uuid), ("stale", False))  # replaced since: answer with today's relation
        for sequence, other in ((1, content(self.survey, title="other")), (2, body)):
            with self.subTest(sequence), self.assertRaises(ResultConflict):
                self.upload(sequence, other, publish_uuid)
        self.assertEqual(PublishedResultRecord.objects.get(publish_uuid=publish_uuid).conflict_count, 2)

    def test_impossible_values_are_rejected(self):
        for body in (content(self.survey, watermark=6), content(self.survey, version=2),
                     {**content(self.survey), "analyzed_through_sequence": True},
                     {**content(self.survey), "survey_uuid": "not-a-uuid"}):
            with self.subTest(body.get("analyzed_through_sequence")), self.assertRaises(ResultInvalid):
                self.upload(1, body)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
    def test_api_status_codes_utf8_body_and_heartbeat_sequence(self):
        def post(body):
            return self.client.post("/api/node/v1/results/", data=canonical_bytes(body),
                                    content_type="application/json; charset=utf-8",
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        body = content(self.survey, title="中文標題")
        good = {"publish_uuid": str(uuid.uuid4()), "publish_sequence": 1, "content_hash": sha256_hex(body), "content": body}
        self.assertEqual((post(good).status_code, post(good).status_code), (201, 200))
        self.assertEqual(post({**good, "publish_uuid": str(uuid.uuid4()), "content_hash": "0" * 64}).status_code, 400)
        with override_settings(CLOUD_RESULT_MAX_BYTES=100):
            self.assertEqual(post({**good, "publish_uuid": str(uuid.uuid4())}).status_code, 413)
        beat = self.client.post("/api/node/v1/heartbeat/", data="{}", content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertEqual(beat["surveys"][0]["publish_sequence"], 1)
```

`cloudapi/test_postgres.py` 加 `ConcurrentResultPostgreSQLTests.test_concurrent_uploads_are_serialized`（強制交錯）：patch `PublishedResultRecord.objects.create` 的包裝，讓執行緒 A（序號 12）建立歷史後在 `threading.Event` 上等待（此時持有 state 列鎖）；啟動執行緒 B（序號 11），`time.sleep(0.5)` 後斷言 B 仍在執行（被鎖住）；放行 A；兩者結束後斷言 `state.publish_sequence == 12`、`published_upload_uuid` 為 A 的 uuid、B 的歷史 `applied` 為 False。

- [ ] **Step 2: 執行確認失敗** — Run：`manage.py test cloudapi.tests.test_results`；Expected: FAIL（`No module named 'cloudapi.results'`）
- [ ] **Step 3: 實作**
- [ ] **Step 4: 執行確認通過** — Run：`manage.py test cloudapi feedback.test_analysis_jobs`；cloud／node `makemigrations --check` 無變更
- [ ] **Step 5: Commit** — `feat(cloudapi): validated result uploads with metadata history and atomic display switch`

---

### Task 4: 網站顯示、新鮮度與計數

**Files:**
- Create: `cloudapi/freshness.py`
- Modify: `feedback/published_analysis.py`（`get_published_analysis_payload`、`get_published_ai_pipeline_status`）、`feedback/views.py`（`analysis_report_surveys`、`_survey_catalog_rows`）、`templates/feedback/_analysis_publication_status.html`、`cloudapi/manage_views.py`／`templates/cloudapi/inbox_manage.html`（結果衝突次數）
- Test: `cloudapi/tests/test_result_display.py`

**Interfaces:**
- Consumes: Task 3 欄位；C2 `SubmissionReceipt`
- Produces:
  - `cloudapi.freshness.node_freshness(survey, state) -> dict`：`{"has_result", "publish_sequence", "definition_current", "pending_new", "legacy_unmigrated", "is_latest", "coverage", "pipeline_declared"}`；`state` 可為 `None`
  - `get_published_analysis_payload`：`survey.owner_node_id` 已設定時一律附 `node_result`，包含 state 為 `None` 而提早 return 的分支；`freshness` 各段＝`is_latest and manifest["stages"][段]["current"]`（無上傳時全為 False）；`available = bool(display)`；`ai_source = manifest["ai_source"]`
  - `get_published_ai_pipeline_status`：node 問卷的草稿狀態一律 `{}`
  - `analysis_report_surveys` 的 `valid_response_count` 與 `_survey_catalog_rows` 的 `response_count`：上傳結果改讀 `publication_manifest.coverage.analyzed_unique`（`KT("publication_manifest__coverage__analyzed_unique")` 轉整數），順序為 Snapshot 計數 → 上傳 coverage → 原本的回退

範本：`node_result` 存在時，有結果顯示「本機發布 #N」，否則「尚無本機發布結果」；依序顯示「問卷已變更，結果為舊版本」、「有 N 筆新回覆尚未分析」、「雲端既有 N 筆未納入分析」（N>0）、已分析與排除筆數、「管線版本由本機申報」與版本。收件匣管理頁每節點顯示「結果內容衝突 N 次」（N>0）。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_result_display.py
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cloudapi.envelope import sha256_hex
from cloudapi.models import NodeDevice, SubmissionReceipt
from cloudapi.results import apply_upload
from cloudapi.tests.test_results import content
from cloudapi.writes import assign_survey_to_node
from feedback.models import FeedbackSubmission, Survey
from feedback.published_analysis import get_published_analysis_payload
from feedback.test_utils import cloud_only


def make_receipt(survey, sequence, status="received"):
    return SubmissionReceipt.objects.create(
        submission_uuid=uuid.uuid4(), node=survey.owner_node, survey=survey, submitted_at=timezone.now(),
        definition_version=1, response_sequence=sequence, payload_hash="0" * 64, status=status)


@cloud_only
class ResultDisplayTests(TestCase):
    def setUp(self):
        self.node, _ = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey
        Survey.objects.filter(pk=self.survey.pk).update(response_sequence=1)
        make_receipt(self.survey, 1, status="synced")
        body = content(self.survey)
        apply_upload(self.node, publish_uuid=str(uuid.uuid4()), publish_sequence=1,
                     content_hash=sha256_hex(body), content=body)
        manager = get_user_model().objects.create_user(username="m", password="x", role="manager")
        self.client.force_login(manager)

    def payload(self):
        return get_published_analysis_payload(Survey.objects.get(pk=self.survey.pk))

    def test_uploaded_result_is_available_and_latest(self):
        payload = self.payload()
        self.assertTrue(payload["available"])
        self.assertTrue(payload["node_result"]["is_latest"])
        self.assertEqual((payload["freshness"]["statistics"], payload["freshness"]["ai"]), (True, False))

    def test_new_quarantined_and_abandoned_replies(self):
        make_receipt(self.survey, 2, status="quarantined")
        make_receipt(self.survey, 3, status="abandoned")
        result = self.payload()["node_result"]
        self.assertEqual((result["pending_new"], result["is_latest"]), (1, False))
        Survey.objects.filter(pk=self.survey.pk).update(definition_version=9)
        self.assertFalse(self.payload()["node_result"]["definition_current"])

    def test_assigned_survey_without_upload_is_not_latest(self):
        from feedback.models import SurveyAnalysisState

        other = assign_survey_to_node(Survey.objects.create(title="O", slug="o"), self.node).survey
        result = get_published_analysis_payload(other)["node_result"]
        self.assertEqual((result["has_result"], result["is_latest"]), (False, False))
        SurveyAnalysisState.objects.filter(survey=other).delete()  # the early-return branch
        self.assertIn("node_result", get_published_analysis_payload(other))

    def test_page_shows_reasons_and_counts(self):
        make_receipt(self.survey, 2)
        FeedbackSubmission.objects.create(survey=self.survey)
        page = self.client.get(reverse("feedback:stats-overview") + "?survey=s")
        for text in ("本機發布 #1", "有 1 筆新回覆尚未分析", "雲端既有 1 筆未納入分析", "管線版本由本機申報"):
            self.assertContains(page, text)

    def test_catalog_counts_use_uploaded_coverage(self):
        from feedback.views import _survey_catalog_rows

        row = _survey_catalog_rows(Survey.objects.filter(pk=self.survey.pk))[0]
        self.assertEqual(row.response_count, 3)  # coverage.analyzed_unique from the upload

    def test_unassigned_surveys_keep_the_old_payload(self):
        self.assertNotIn("node_result", get_published_analysis_payload(Survey.objects.create(title="P", slug="p")))
```

- [ ] **Step 2: 執行確認失敗** — Run：`manage.py test cloudapi.tests.test_result_display`；Expected: FAIL
- [ ] **Step 3: 實作**
- [ ] **Step 4: 執行確認通過與回歸** — Run：`manage.py test cloudapi feedback`；Expected: PASS
- [ ] **Step 5: Commit** — `feat(cloudapi): show node results with definition, input and pipeline freshness`

---

### Task 5: 同步週期上傳與主控台（node）

**Files:**
- Modify: `cloudsync/client.py`（`CloudClient.post_raw(path, data: bytes) -> dict`）、`cloudsync/results.py`（`upload_results`、`retry_failed`）、`cloudsync/runner.py`、`node/status.py`、`node/views.py`、`cloudsync/views.py`、`templates/cloudsync/connection.html`
- Test: `cloudsync/tests/test_results_upload.py`、`cloudsync/tests/test_runner.py`、`node/tests/test_status.py`、`cloudsync/tests/test_client.py`

**Interfaces:**
- Consumes: Task 2、Task 3 HTTP 契約
- Produces:
  - `CloudClient.post_raw`：與 `request` 相同的錯誤分類，送 `data` 與 `Content-Type: application/json; charset=utf-8`
  - `cloudsync.results.upload_results(client) -> dict`（`uploaded`、`stale`、`failed` 計數）、`retry_failed() -> int`
  - `run_cycle` 順序見 Global Constraints；心跳的 `publish_sequence` 只增不減寫入 `cloud_publish_sequence`
  - `node.status.results_status(link=None) -> StatusItem`（key `"results"`）：未連結 `off`；有 `failed` → `warn`「N 份結果上傳失敗」；否則 `ok`，摘要「已分析 A 筆 · 尚未分析 B 筆 · 待上傳 C 份」（A＝已發布 Snapshot `response_count`；B＝本機同步回覆中 `response_sequence` 大於已發布 Snapshot 水位者）；`PENDING_MESSAGES["results"] = "結果上傳需要處理：請到「雲端連線」查看。"`
  - 雲端連線頁顯示待上傳／失敗份數與失敗原因，「重新上傳」按鈕（`action=retry-results`）

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_results_upload.py
from django.test import TestCase

from cloudsync.client import CLIENT, CONFLICT, TRANSIENT, UNAUTHORIZED, CloudError
from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload
from cloudsync.results import record_publication, retry_failed, upload_results
from cloudsync.tests.test_inbox import definition
from cloudsync.tests.test_results_local import published_state


class FakeResultClient:
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def post_raw(self, path, data):
        import json

        assert path == "results/"
        self.sent.append(json.loads(data))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class UploadResultsTests(TestCase):
    def setUp(self):
        survey, _ = upsert_definition(definition(1))
        state = published_state(survey)
        record_publication(state)
        record_publication(state)

    def test_identity_is_reused_and_statuses_follow_replies(self):
        client = FakeResultClient([CloudError(TRANSIENT), {"status": "applied"}, {"status": "stale"}])
        with self.assertRaises(CloudError):
            upload_results(client)
        first = ResultUpload.objects.order_by("publish_sequence").first()
        self.assertEqual((first.status, first.attempts), ("pending", 1))
        upload_results(client)
        self.assertEqual(list(ResultUpload.objects.order_by("publish_sequence").values_list("status", flat=True)),
                         ["uploaded", "stale"])
        self.assertEqual(client.sent[0]["publish_uuid"], client.sent[1]["publish_uuid"])
        self.assertEqual(client.sent[0]["content_hash"], client.sent[1]["content_hash"])

    def test_unauthorized_stays_pending_and_conflicts_fail_until_retried(self):
        with self.assertRaises(CloudError):
            upload_results(FakeResultClient([CloudError(UNAUTHORIZED)]))
        self.assertEqual(set(ResultUpload.objects.values_list("status", flat=True)), {"pending"})
        upload_results(FakeResultClient([CloudError(CONFLICT), CloudError(CLIENT, status=404)]))
        self.assertEqual(set(ResultUpload.objects.values_list("last_error", flat=True)), {"conflict", "not_found"})
        upload_results(FakeResultClient([]))  # failed items are not retried automatically
        self.assertEqual(retry_failed(), 2)
        self.assertEqual(set(ResultUpload.objects.values_list("status", flat=True)), {"pending"})

    def test_oversized_content_fails_without_sending(self):
        from django.test import override_settings

        with override_settings(CLOUD_RESULT_MAX_BYTES=10):
            client = FakeResultClient([])
            upload_results(client)
        self.assertEqual((client.sent, set(ResultUpload.objects.values_list("last_error", flat=True))), ([], {"too_large"}))
```

`cloudsync/tests/test_runner.py` 加 `test_cycle_order_and_cloud_sequence_only_increases`：以 `MagicMock` 記錄呼叫順序 patch `sync_definitions`、`sync_inbox`、`backfill_publications`、`upload_results`，心跳回 `publish_sequence` 7 再回 5；斷言順序為 definitions、inbox、heartbeat、backfill、upload，`cloud_publish_sequence` 兩次後皆為 7。既有 runner 測試輔助一併 patch 新增的兩個函式。
`cloudsync/tests/test_client.py` 加 `test_post_raw_sends_utf8_bytes`。`node/tests/test_status.py` 加 `test_results_status_counts_and_failures`。

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync node`；Expected: FAIL
- [ ] **Step 3: 實作**
- [ ] **Step 4: 執行確認通過** — Run（node）：`manage.py test cloudsync node`；Expected: PASS
- [ ] **Step 5: Commit** — `feat(cloudsync): upload node results in the sync cycle with stable identity`

---

### Task 6: 端對端與文件

**Files:**
- Create: `cloudsync/tests/test_e2e_results.py`
- Modify: `docs/architecture.md`、`docs/next-actions.md`

**Interfaces:** Consumes 全部前述任務與 C2 端對端寫法（`CloudServer(inbox=True)`、`SEED`、`SUBMIT`、`cloud.shell`）。

- [ ] **Step 1: 寫失敗測試**（四個測試；雲端狀態以 `cloud.shell` 執行 `get_published_analysis_payload` 並印出 JSON 讀取）
  1. `test_reply_analysis_upload_and_display`：雲端送出兩筆 → `run_cycle(force=True)` → 本機 `run_analysis_worker_once` → `run_cycle(force=True)` → 雲端 `available` 真、`node_result.is_latest` 真、`coverage.analyzed_unique == 2`、`publish_sequence == 1`。
  2. `test_new_reply_and_definition_change_mark_the_result_old`：接著再送一筆並 `run_cycle`（不重跑分析）→ `pending_new == 1`、`is_latest` 假；雲端改題目標題後 → `definition_current` 假；展示內容仍為序號 1。
  3. `test_lost_upload_reply_is_resent_with_the_same_identity`：patch `CloudClient.post_raw`，第一次 `results/` 先呼叫原函式再拋 `CloudError(TRANSIENT)`；下一輪後本機 `ResultUpload.status == "uploaded"`、雲端 `PublishedResultRecord` 只有一筆。
  4. `test_restored_node_resends_old_result_as_stale`：完成一次上傳（序號 1）後再發布並上傳（序號 2）；把序號 1 的本機紀錄改回 `pending` 模擬還原前資料重送 → 雲端依目前關係回 `stale`、不新增歷史；本機該筆 `stale`，雲端展示仍為序號 2。
- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_e2e_results`
- [ ] **Step 3: 補齊實作缺口**（暴露的問題回到對應任務修正並補單元測試）
- [ ] **Step 4: 兩模式全套件** — cloud：`manage.py test feedback accounts config cloudapi`；node：`manage.py test feedback accounts config node organizations cloudapi cloudsync`；Expected: 兩者 PASS
- [ ] **Step 5: 文件（只寫現況）** — `docs/architecture.md` 同步段落補結果上傳（範圍限定、凍結身分、歷史只存中繼資料、三條件切換、三項新鮮度、水位綁定輸入與前提）；`docs/next-actions.md` 改為「C1–C3 已完成；接續 C4 搬移，之後處理通知系統」。
- [ ] **Step 6: Commit** — `test(cloudsync): end-to-end result upload, freshness and resend`
