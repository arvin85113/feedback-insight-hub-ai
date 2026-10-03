# 雲端同步 C3：結果上傳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本機節點發布的分析結果以不可變身分上傳雲端；雲端保存全部歷史，只在序號、輸入水位、定義版本都不倒退時原子切換 `SurveyAnalysisState`，網站依「定義／輸入／管線」三項標示是否最新。

**Architecture:** 本機在既有 `publish_analysis_snapshot`／`publish_analysis_stages` 的同一交易內建立 `ResultUpload`（身分與內容當下凍結），同步週期上傳；雲端 `cloudapi.results.apply_upload` 在 `SurveyAnalysisState` 列鎖內寫歷史並比較後切換。網站對指派給節點的問卷改讀上傳內容與新鮮度，不再依賴本機才有的 Snapshot／Stage 外鍵。

**Tech Stack:** Django 6.0.8、requests 2.32.5、SQLite／PostgreSQL 17（併發測試）。

**Spec:** [雲端同步規格](../specs/2026-10-01-cloud-sync-design.md)（本計畫涵蓋 §1 已分析／尚未分析／排除／雲端既有、§3 `PublishedResultRecord` 與 `SurveyAnalysisState` 擴充、§4 `results/` 與心跳的 `publish_sequence`、§5 `ResultUpload` 與補排、§7、§13 對應測試）。

**前置：** C1、C2 已合併於 `main`。分支 `feat/cloud-sync-c3`。

**設計決定（需審閱）——輸入擷取：** 規格 §7 要求「一致快照擷取 → 保存 → 運算」三段。現行本機分析的 `AnswerInput` 直接從資料庫串流，不另外物化。本計畫以兩個既有或小幅擴充的機制達到同一保證，不重寫分析輸入層：
1. **水位凍結**：建立 adapter 時讀一次 `SurveySyncState.synced_through_sequence`（W），輸入只納入「非收件匣回覆」與「`response_sequence ≤ W` 的收件匣回覆」。依水位定義，≤ W 的收件匣回覆在擷取時已全部寫入或已放棄，之後不會再增加；擷取後同步進來的回覆序號必大於 W，不會進入本次輸入。W 寫入 Snapshot 的 `data_scope.analyzed_through_sequence`，並納入 adapter 版本字串（快取鍵）。
2. **版本作廢**：運算期間題目、詞典或本機回覆變更，既有排程會遞增 `input_version`／`config_version`，發布時 `_versions_are_current` 不成立 → 該結果不發布、不產生 `ResultUpload`。
因此「結果不混用不同時點的輸入、也不帶上較新的水位」成立；代價是快速連續變更時較多工作被作廢重跑（現行行為即如此）。

**不在本計畫：** C4 搬移與 `migration_baseline` 標示；網站從上傳的 AI 結果匯入改善草稿（本機才有 Stage 列，雲端此階段只讀顯示）；加密；通知系統。

## Global Constraints

- 只有 node 模式在發布時建立 `ResultUpload`；cloud 模式行為不變。
- 發布身分：`publish_uuid`（uuid4）、`publish_sequence`、`content_hash` 在建立 `ResultUpload` 時決定，重送一律沿用，**不因任何拒絕改號**。
- 取號：`publish_sequence = max(SurveySyncState.cloud_publish_sequence, SurveySyncState.local_publish_sequence) + 1`，同交易寫回 `local_publish_sequence`；`cloud_publish_sequence` 只由心跳更新。
- `content_hash = cloudapi.envelope.sha256_hex(content)`。上傳內容（`content`）鍵固定：
  `{"survey_uuid", "definition_version", "analyzed_through_sequence", "input_fingerprint", "published_at", "pipeline": {"input_version", "config_version", "pipeline_version", "implementation_version"}, "stages": {"statistics": bool, "text": bool, "ai": bool}, "display_payload": dict, "ai_payload": dict | None, "ai_source": dict, "coverage": {"analyzed_unique": int, "excluded": {"incomplete": int, "voided": int}}}`
- 上傳上限 `CLOUD_RESULT_MAX_BYTES = 2 * 1024 * 1024`（`canonical_bytes(content)`）；超過回 400。
- 雲端回應（HTTP 200，新建歷史時 201）`{"status": "applied" | "stale"}`；同 `publish_uuid` 不同 `content_hash` 回 409 `{"error": "content_conflict"}`；問卷不屬於節點 404；格式錯誤或雜湊與內容不符 400。
- 本機 `ResultUpload.status`：`pending` → `uploaded`（applied）／`stale`（不再重送）／`failed`（409、400、404；`last_error` 記錄分類，不再自動重送）；暫時性錯誤保持 `pending`，`attempts` 加一並讓本次週期以該錯誤結束（沿用退避）。
- 展示指標切換條件（全部成立才切換）：`publish_sequence > state.publish_sequence`、`analyzed_through_sequence >= state.analyzed_through_sequence`、`definition_version >= state.definition_version`。
- 新鮮度（網站）：定義＝`state.definition_version == survey.definition_version`；輸入＝無 `response_sequence > state.analyzed_through_sequence` 且非 `abandoned` 的收據（N＝筆數）；管線＝本機申報，只呈現不驗證。
- 使用者可見訊息（逐字）：「有 N 筆新回覆尚未分析」、「問卷已變更，結果為舊版本」、「雲端既有 N 筆未納入分析」、「管線版本由本機申報」、「本機發布 #N」。
- log 不記錄答案正文；上傳內容只含既有的有限展示 JSON（與現行 `published_display_payload`／`published_ai_payload` 相同層級），不含逐筆回覆。
- 測試指令同 C2（cloud／node 兩模式；雲端行為測試加 `cloud_only`）。commit／push／PR 依 AGENTS.md 取得授權。

## Review Focus

1. **兩份結果同時上傳**：依 `SurveyAnalysisState` 列鎖依序比較，較舊序號只進歷史（Task 3 PostgreSQL 測試）。
2. **上傳回應遺失後重送**：同 `publish_uuid`、同雜湊回原狀態、不建第二筆歷史（Task 3 `test_resend_is_idempotent`、Task 6 端對端）。
3. **本機從備份還原**：新發布接在心跳得知的雲端序號之後；還原前的舊結果重送沿用原身分並成為過期歷史（Task 2 `test_sequence_continues_after_cloud`、Task 3 `test_stale_upload_keeps_history_only`）。
4. **分析期間又同步進新回覆**：新回覆不進本次輸入、結果水位為擷取時的 W（Task 1 `test_replies_beyond_the_watermark_are_excluded`）。
5. **定義已變更或有新回覆時**：網站保留最後成功結果但不標為最新，並顯示原因與筆數（Task 4）。

---

### Task 1: 水位綁定的分析輸入（node）

**Files:**
- Modify: `feedback/analysis_adapters.py`（`AnswerInput.from_survey(..., extra_filter=None)`、`scan` 套用）、`feedback/analysis_worker.py`（`_build_adapter`、`build_result` 的 `data_scope`）
- Create: `cloudsync/capture.py`
- Test: `cloudsync/tests/test_capture.py`

**Interfaces:**
- Consumes: C2 `SurveySyncState`、`SyncedSubmissionSource`
- Produces:
  - `AnswerInput.from_survey(survey, *, version, extra_filter=None)`：`extra_filter` 為 `django.db.models.Q`，套用在 `FeedbackSubmission` 查詢（含 `columns` 為空的計數路徑）
  - `cloudsync.capture.capture_scope(survey) -> CaptureScope`（dataclass：`watermark: int`、`definition_version: int`、`submission_filter: Q`、`excluded: dict[str, int]`）
    - `submission_filter = Q(synced_source__isnull=True) | Q(synced_source__response_sequence__lte=watermark)`
    - `excluded = {"incomplete": n, "voided": n}`：在同一個 filter 範圍內計數 `is_complete=False`、`voided_at` 非空
  - Snapshot `source_snapshot["data_scope"]` 在 node 模式多出：`analyzed_through_sequence`、`definition_version`、`excluded`
  - adapter 版本字串在 node 模式加上 `:watermark:<W>`

`_build_adapter`（answers 來源）在 `settings.IS_NODE` 時呼叫 `capture_scope(job.survey)`，把 filter 傳給 `from_survey`，並把 scope 掛在 adapter 上（`adapter.capture_scope`）供 `build_result` 寫入 `data_scope`。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_capture.py
from django.test import TestCase

from cloudsync.capture import capture_scope
from cloudsync.definitions import upsert_definition
from cloudsync.inbox import intake
from cloudsync.tests.test_inbox import Q1, U1, U2, U3, FakeInboxClient, definition, envelope
from feedback.analysis_adapters import AnswerInput
from feedback.models import FeedbackSubmission, Survey


class CaptureTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        intake(envelope(1, U1, {Q1: "a"}), FakeInboxClient([]))
        intake(envelope(3, U3, {Q1: "c"}), FakeInboxClient([]))  # gap at 2 keeps W at 1
        FeedbackSubmission.objects.create(survey=self.survey, is_complete=False)  # local, not from the inbox

    def test_scope_freezes_the_watermark(self):
        scope = capture_scope(self.survey)
        self.assertEqual((scope.watermark, scope.definition_version), (1, 1))
        self.assertEqual(scope.excluded, {"incomplete": 1, "voided": 0})

    def test_replies_beyond_the_watermark_are_excluded(self):
        scope = capture_scope(self.survey)
        intake(envelope(2, U2, {Q1: "b"}), FakeInboxClient([]))  # arrives after capture; W would now be 3
        adapter = AnswerInput.from_survey(Survey.objects.get(pk=self.survey.pk), version="v",
                                          extra_filter=scope.submission_filter)
        name = next(iter(adapter.question_fields.values())).name
        self.assertEqual([row[name] for row in adapter.scan([name])], ["a"])
        self.assertEqual(sum(1 for _ in adapter.scan([])), 1)
```

在 `feedback/test_analysis_worker.py`（node 模式才跑的類別，加 `skipUnless(settings.IS_NODE)`）加入 `test_node_snapshot_records_watermark_and_definition_version`：建一份收件匣回覆（W=1），執行 `execute_deterministic_job`（沿用該檔既有的 claim／output_root 輔助），斷言 Snapshot `source_snapshot["data_scope"]` 的 `analyzed_through_sequence == 1`、`definition_version == 1`、`excluded == {"incomplete": 0, "voided": 0}`。

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_capture`；Expected: FAIL（`No module named 'cloudsync.capture'`）
- [ ] **Step 3: 實作**（見 Interfaces；cloud 模式 `_build_adapter` 行為與版本字串完全不變）
- [ ] **Step 4: 執行確認通過與回歸** — Run（node）：`manage.py test cloudsync feedback.test_analysis_worker`；cloud：`manage.py test feedback.test_analysis_worker`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudsync feedback/analysis_adapters.py feedback/analysis_worker.py feedback/test_analysis_worker.py`；`feat(cloudsync): bind node analysis input to the reply watermark`

---

### Task 2: 本機發布紀錄與取號（node）

**Files:**
- Modify: `cloudsync/models.py`（`ResultUpload`；`SurveySyncState.cloud_publish_sequence`、`local_publish_sequence`）、migration `cloudsync/0004`
- Create: `cloudsync/results.py`
- Modify: `feedback/analysis_jobs.py`（`publish_analysis_snapshot`、`publish_analysis_stages` 在 `state.save(...)` 之後、同一交易內呼叫 `record_publication(state)`，只在 `settings.IS_NODE`）
- Test: `cloudsync/tests/test_results_local.py`

**Interfaces:**
- Consumes: Task 1 `data_scope`；C2 `SurveySyncState`
- Produces:
  - `ResultUpload`：`publish_uuid`（`UUIDField(unique=True, default=uuid4)`）、`survey`（FK）、`publish_sequence`、`content_hash`、`content`（JSON）、`published_at`（取自 `state.published_at`）、`status`（`pending`／`uploaded`／`stale`／`failed`）、`attempts`、`last_error`（`CharField(64, blank=True)`）、`created_at`
  - `SurveySyncState.cloud_publish_sequence`、`local_publish_sequence`（`PositiveBigIntegerField(default=0)`）
  - `cloudsync.results.build_content(state) -> dict`（鍵見 Global Constraints；`display_payload`＝`state.published_display_payload`、`ai_payload`＝`state.published_ai_payload or None`、`ai_source` 取自已發布 AI Stage 的 Snapshot：`response_count`、`analysis_coverage`、`source_latest_at`、`model_name`；水位、定義版本、`excluded` 取自已發布 Snapshot 的 `data_scope`，缺少時 0；`analyzed_unique`＝Snapshot `response_count`）
  - `cloudsync.results.record_publication(state) -> ResultUpload`（取號、凍結內容與雜湊；呼叫端須在交易內）
  - `cloudsync.results.backfill_publications() -> int`：對每份有 `published_at` 的問卷，若沒有 `published_at` 相同的 `ResultUpload` 就補建一筆

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_results_local.py
from django.test import TestCase
from django.utils import timezone

from cloudapi.envelope import sha256_hex
from cloudsync.definitions import upsert_definition
from cloudsync.models import ResultUpload, SurveySyncState
from cloudsync.results import backfill_publications, record_publication
from cloudsync.tests.test_inbox import definition
from feedback.models import SurveyAnalysisState


class RecordPublicationTests(TestCase):
    def setUp(self):
        self.survey, _ = upsert_definition(definition(1))
        self.state = SurveyAnalysisState.objects.create(
            survey=self.survey, published_at=timezone.now(),
            published_display_payload={"statistics": {"charts": []}, "text_analysis": {}},
        )

    def test_identity_and_content_are_frozen_at_creation(self):
        upload = record_publication(self.state)
        self.assertEqual((upload.status, upload.publish_sequence), ("pending", 1))
        self.assertEqual(upload.content_hash, sha256_hex(upload.content))
        self.assertEqual(upload.content["display_payload"], {"statistics": {"charts": []}, "text_analysis": {}})
        self.assertEqual(upload.content["survey_uuid"], str(self.survey.uuid))
        SurveyAnalysisState.objects.filter(pk=self.state.pk).update(published_display_payload={"changed": True})
        upload.refresh_from_db()
        self.assertEqual(upload.content_hash, sha256_hex(upload.content))  # unchanged copy

    def test_sequence_continues_after_cloud(self):
        record_publication(self.state)  # local 1
        SurveySyncState.objects.filter(survey=self.survey).update(cloud_publish_sequence=7)
        self.assertEqual(record_publication(self.state).publish_sequence, 8)

    def test_backfill_creates_missing_upload_once(self):
        self.assertEqual(backfill_publications(), 1)
        self.assertEqual(backfill_publications(), 0)
        self.assertEqual(ResultUpload.objects.count(), 1)
```

在 `feedback/test_analysis_jobs.py` 加 node 模式限定測試 `test_node_publish_creates_result_upload_in_the_same_transaction`：以既有輔助走一次 `publish_analysis_snapshot` 成功路徑，斷言 `ResultUpload` 一筆、`status="pending"`；再走一次被判定 stale 的發布，斷言沒有新增。

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_results_local`；Expected: FAIL（`ResultUpload` 不存在）
- [ ] **Step 3: 實作模型、migration、`cloudsync/results.py` 與兩個發布函式的呼叫點**（`from cloudsync.results import record_publication` 放在函式內，避免 cloud 模式載入）
- [ ] **Step 4: 執行確認通過** — Run（node）：`manage.py test cloudsync feedback.test_analysis_jobs`；cloud：`manage.py test feedback.test_analysis_jobs`；Expected: PASS；node `makemigrations --check` 無變更
- [ ] **Step 5: Commit** — `git add cloudsync feedback/analysis_jobs.py feedback/test_analysis_jobs.py`；`feat(cloudsync): freeze each node publication as a result upload`

---

### Task 3: 雲端結果歷史與原子切換

**Files:**
- Modify: `cloudapi/models.py`（`PublishedResultRecord`）、`feedback/models.py`（`SurveyAnalysisState` 四個欄位）、migrations（`feedback/0022`、`cloudapi/0003`）
- Create: `cloudapi/results.py`
- Modify: `cloudapi/views.py`、`cloudapi/urls.py`（`results/`）、`cloudapi/inbox.py`（`survey_sequences` 加 `publish_sequence`）、`config/settings.py`（`CLOUD_RESULT_MAX_BYTES`）
- Test: `cloudapi/tests/test_results.py`、`cloudapi/test_postgres.py`（加一個類別）

**Interfaces:**
- Consumes: C1 `node_api`；Task 2 內容格式
- Produces:
  - `SurveyAnalysisState.publish_sequence`、`analyzed_through_sequence`、`definition_version`（`PositiveBigIntegerField(default=0)`）、`published_upload_uuid`（`UUIDField(null=True, blank=True)`）
  - `PublishedResultRecord`：`publish_uuid`（唯一）、`node`、`survey`（`PROTECT`）、`publish_sequence`、`content_hash`、`content`、`definition_version`、`analyzed_through_sequence`、`applied`（bool）、`received_at`
  - `cloudapi.results.apply_upload(node, *, publish_uuid, publish_sequence, content_hash, content) -> tuple[str, bool]`（`(status, created)`；`status` 為 `"applied"`／`"stale"`；拋 `ResultConflict`、`ResultInvalid`、`PermissionError`）
  - `POST results/` body `{"publish_uuid", "publish_sequence", "content_hash", "content"}`；回應見 Global Constraints
  - 心跳 `surveys[]` 每項加 `"publish_sequence": state.publish_sequence`（無 state 為 0）

`apply_upload` 順序（一個交易）：驗證 `sha256_hex(content) == content_hash` 與大小 → 問卷 `uuid=content["survey_uuid"]` 且 `owner_node=node` → `SurveyAnalysisState.objects.get_or_create(survey=...)` 後 `select_for_update()` 重新取得 → 已有同 `publish_uuid`：雜湊同回 `("applied" if record.applied else "stale", False)`，不同拋 `ResultConflict` → 建歷史 → **在鎖內**比較三條件 → 成立則寫：
`published_display_payload`、`published_ai_payload`（`None` 轉 `{}`）、`publication_manifest = {"source": "node", "publish_uuid", "stages", "pipeline", "coverage", "ai_source", "input_fingerprint"}`、`published_at = content["published_at"]`、`published_snapshot = None`、`published_ai_stage = None`、四個新欄位；`record.applied = True`。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_results.py
import json
import uuid

from django.test import TestCase, override_settings

from cloudapi.envelope import sha256_hex
from cloudapi.models import NodeDevice, PublishedResultRecord
from cloudapi.results import ResultConflict, apply_upload
from cloudapi.writes import assign_survey_to_node
from feedback.models import Survey, SurveyAnalysisState
from feedback.test_utils import cloud_only


def content(survey, *, watermark=1, version=1, title="v"):
    return {"survey_uuid": str(survey.uuid), "definition_version": version, "analyzed_through_sequence": watermark,
            "input_fingerprint": "f", "published_at": "2026-10-03T00:00:00+00:00",
            "pipeline": {"input_version": 1, "config_version": 1, "pipeline_version": "p", "implementation_version": "i"},
            "stages": {"statistics": True, "text": True, "ai": False},
            "display_payload": {"statistics": {"title": title}, "text_analysis": {}}, "ai_payload": None,
            "ai_source": {}, "coverage": {"analyzed_unique": 3, "excluded": {"incomplete": 0, "voided": 0}}}


@cloud_only
class ApplyUploadTests(TestCase):
    def setUp(self):
        self.node, self.token = NodeDevice.issue("office")
        self.survey = assign_survey_to_node(Survey.objects.create(title="S", slug="s"), self.node).survey

    def upload(self, sequence, body, publish_uuid=None):
        return apply_upload(self.node, publish_uuid=publish_uuid or uuid.uuid4(), publish_sequence=sequence,
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
        state = SurveyAnalysisState.objects.get(survey=self.survey)
        self.assertEqual(state.published_display_payload["statistics"]["title"], "five")
        self.assertEqual(PublishedResultRecord.objects.count(), 4)

    def test_resend_is_idempotent(self):
        publish_uuid = uuid.uuid4()
        body = content(self.survey)
        self.upload(1, body, publish_uuid)
        self.assertEqual(self.upload(1, body, publish_uuid), ("applied", False))
        self.assertEqual(PublishedResultRecord.objects.count(), 1)
        with self.assertRaises(ResultConflict):
            self.upload(1, content(self.survey, title="other"), publish_uuid)

    @override_settings(CLOUD_SYNC_PROTOTYPE_ENABLED=True)
    def test_api_status_codes_and_heartbeat_sequence(self):
        def post(body):
            return self.client.post("/api/node/v1/results/", data=json.dumps(body), content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        body = content(self.survey)
        good = {"publish_uuid": str(uuid.uuid4()), "publish_sequence": 1, "content_hash": sha256_hex(body), "content": body}
        self.assertEqual((post(good).status_code, post(good).status_code), (201, 200))
        self.assertEqual(post({**good, "content_hash": "0" * 64}).status_code, 400)
        other, _ = NodeDevice.issue("other")
        Survey.objects.filter(pk=self.survey.pk).update(owner_node=other)
        self.assertEqual(post({**good, "publish_uuid": str(uuid.uuid4())}).status_code, 404)
        Survey.objects.filter(pk=self.survey.pk).update(owner_node=self.node)
        beat = self.client.post("/api/node/v1/heartbeat/", data="{}", content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertEqual(beat["surveys"][0]["publish_sequence"], 1)
```

在 `cloudapi/test_postgres.py` 加 `ConcurrentResultPostgreSQLTests.test_concurrent_uploads_never_regress`：兩執行緒以 `threading.Barrier(2)` 同時 `apply_upload` 序號 11 與 12；斷言最終 `state.publish_sequence == 12`、展示內容為 12 的內容、兩筆歷史都在、11 的 `applied` 為 False 或其 `applied` 為 True 但已被 12 覆蓋（以 `state.published_upload_uuid == uuid12` 為準）。

- [ ] **Step 2: 執行確認失敗** — Run：`manage.py test cloudapi.tests.test_results`；Expected: FAIL（`No module named 'cloudapi.results'`）
- [ ] **Step 3: 實作模型、migration、`apply_upload`、API 與心跳欄位**
- [ ] **Step 4: 執行確認通過** — Run：`manage.py test cloudapi feedback.test_analysis_jobs`；Expected: PASS；cloud／node `makemigrations --check` 無變更
- [ ] **Step 5: Commit** — `git add cloudapi feedback/models.py feedback/migrations config/settings.py`；`feat(cloudapi): result history and atomic display switch for node uploads`

---

### Task 4: 網站顯示上傳結果與新鮮度

**Files:**
- Modify: `feedback/published_analysis.py`（`get_published_analysis_payload`、`get_published_ai_pipeline_status`）、`templates/feedback/_analysis_publication_status.html`
- Create: `cloudapi/freshness.py`
- Test: `cloudapi/tests/test_result_display.py`

**Interfaces:**
- Consumes: Task 3 欄位；C2 `SubmissionReceipt`
- Produces:
  - `cloudapi.freshness.node_freshness(state) -> dict`：`{"definition_current": bool, "pending_new": int, "legacy_unmigrated": int, "pipeline_declared": dict, "coverage": dict, "publish_sequence": int, "is_latest": bool}`；`legacy_unmigrated`＝該問卷雲端 `FeedbackSubmission` 筆數；`is_latest = definition_current and pending_new == 0`
  - `get_published_analysis_payload` 在 `state.published_upload_uuid` 有值時：`available = bool(display)`、`snapshot_id = None`、`node_result = node_freshness(state)`、`freshness` 各段＝`is_latest and manifest["stages"][段]`、`ai_source = manifest["ai_source"]`；其他問卷維持現行
  - `get_published_ai_pipeline_status` 對上傳結果的改善草稿狀態一律空（`_published_draft_states` 回 `{}`）

範本：`node_result` 存在時，標題列改顯示「本機發布 #N」，並依序列出：最新／「問卷已變更，結果為舊版本」／「有 N 筆新回覆尚未分析」／「雲端既有 N 筆未納入分析」（N>0 才顯示）、已分析與排除筆數（`coverage`）、「管線版本由本機申報」與 `pipeline_version`。

- [ ] **Step 1: 寫失敗測試**

```python
# cloudapi/tests/test_result_display.py — setUp 同 Task 3 ApplyUploadTests，並 apply 一份 watermark=1、version=1 的內容
@cloud_only
class ResultDisplayTests(TestCase):
    def test_uploaded_result_is_available_and_latest(self):
        payload = get_published_analysis_payload(self.survey)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["node_result"]["is_latest"])
        self.assertTrue(payload["freshness"]["statistics"])
        self.assertFalse(payload["freshness"]["ai"])  # stages.ai is False

    def test_new_reply_and_definition_change_mark_it_old(self):
        make_receipt(self.survey, response_sequence=2)        # helper: SubmissionReceipt status=received
        make_receipt(self.survey, response_sequence=3, status="abandoned")
        payload = get_published_analysis_payload(self.survey)
        self.assertEqual((payload["node_result"]["pending_new"], payload["node_result"]["is_latest"]), (1, False))
        Survey.objects.filter(pk=self.survey.pk).update(definition_version=9)
        self.assertFalse(get_published_analysis_payload(Survey.objects.get(pk=self.survey.pk))["node_result"]["definition_current"])

    def test_page_shows_reasons(self):
        make_receipt(self.survey, response_sequence=2)
        FeedbackSubmission.objects.create(survey=self.survey)   # legacy cloud reply
        page = self.client.get(reverse("feedback:stats-overview") + f"?survey={self.survey.slug}")  # manager logged in
        self.assertContains(page, "本機發布 #1")
        self.assertContains(page, "有 1 筆新回覆尚未分析")
        self.assertContains(page, "雲端既有 1 筆未納入分析")
        self.assertContains(page, "管線版本由本機申報")

    def test_unassigned_surveys_keep_the_old_payload(self):
        plain = Survey.objects.create(title="P", slug="p")
        self.assertNotIn("node_result", get_published_analysis_payload(plain))
```

（`make_receipt` 寫在本檔：建立 `SubmissionReceipt`，`node`、`survey`、`submitted_at=timezone.now()`、`definition_version=1`、`payload_hash="0"*64`、隨機 `submission_uuid`。統計頁的 survey 參數名稱以 `feedback/views.py` 中 `StatsOverviewView` 實際讀取的為準。）

- [ ] **Step 2: 執行確認失敗** — Run：`manage.py test cloudapi.tests.test_result_display`；Expected: FAIL（`node_result` 不存在）
- [ ] **Step 3: 實作**
- [ ] **Step 4: 執行確認通過與回歸** — Run：`manage.py test cloudapi feedback`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudapi feedback/published_analysis.py templates/feedback`；`feat(cloudapi): show node results with definition, input and pipeline freshness`

---

### Task 5: 同步週期上傳結果（node）

**Files:**
- Modify: `cloudsync/results.py`（`upload_results`）、`cloudsync/runner.py`、`node/status.py`、`templates/cloudsync/connection.html`、`cloudsync/views.py`
- Test: `cloudsync/tests/test_results_upload.py`、`cloudsync/tests/test_runner.py`

**Interfaces:**
- Consumes: Task 2、Task 3 HTTP 契約；C2 心跳
- Produces:
  - `cloudsync.results.upload_results(client) -> dict`（`{"uploaded", "stale", "failed"}` 計數）：依 `created_at` 上傳 `pending`；狀態轉換見 Global Constraints；`CloudError(TRANSIENT)` 時 `attempts += 1` 後重新拋出
  - `run_cycle` 順序：`sync_definitions` → `sync_inbox` → `backfill_publications` → `upload_results` → 心跳；心跳的 `publish_sequence` 寫入對應問卷的 `SurveySyncState.cloud_publish_sequence`（只增不減）
  - `node.status.inbox_status` 之外新增 `results_status(link=None) -> StatusItem`（key `"results"`）：有 `failed` → `warn`「N 份結果上傳失敗」；否則 `ok`「待上傳 N 份」；未連結 `off`；`PENDING_MESSAGES["results"] = "結果上傳需要處理：請到「雲端連線」查看。"`；總覽 items 加入

- [ ] **Step 1: 寫失敗測試**

```python
# cloudsync/tests/test_results_upload.py
class FakeResultClient:
    def __init__(self, responses):
        self.responses, self.bodies = list(responses), []

    def post(self, path, body=None):
        assert path == "results/"
        self.bodies.append(body)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class UploadResultsTests(TestCase):
    # setUp: survey via upsert_definition(definition(1)); SurveyAnalysisState with published_at; two record_publication() calls

    def test_statuses_follow_the_cloud_reply_and_identity_is_reused(self):
        client = FakeResultClient([CloudError(TRANSIENT), {"status": "applied"}, {"status": "stale"}])
        with self.assertRaises(CloudError):
            upload_results(client)
        first = ResultUpload.objects.order_by("created_at").first()
        self.assertEqual((first.status, first.attempts), ("pending", 1))
        upload_results(client)
        self.assertEqual(list(ResultUpload.objects.order_by("created_at").values_list("status", flat=True)),
                         ["uploaded", "stale"])
        self.assertEqual(client.bodies[0]["publish_uuid"], client.bodies[1]["publish_uuid"])  # resend keeps identity
        self.assertEqual(client.bodies[0]["content_hash"], client.bodies[1]["content_hash"])

    def test_conflict_fails_without_retry(self):
        upload_results(FakeResultClient([CloudError(CONFLICT), CloudError(CLIENT)]))
        self.assertEqual(set(ResultUpload.objects.values_list("status", flat=True)), {"failed"})
        upload_results(FakeResultClient([]))  # nothing pending: no request
```

在 `cloudsync/tests/test_runner.py` 的 `InboxCycleTests` 加 `test_cycle_uploads_results_and_records_cloud_sequence`：patch `sync_definitions`、`sync_inbox`、`upload_results`，心跳回 `{"surveys": [{"survey_uuid": ..., "publish_sequence": 7, ...}]}`；斷言 `upload_results` 被呼叫一次、`SurveySyncState.cloud_publish_sequence == 7`；第二次心跳回 5 時仍為 7。

`node/tests/test_status.py` 加 `test_results_status_warns_on_failed_upload`。

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_results_upload cloudsync.tests.test_runner node.tests.test_status`；Expected: FAIL
- [ ] **Step 3: 實作**（既有 runner 測試的輔助一併 patch `upload_results` 與 `backfill_publications`）
- [ ] **Step 4: 執行確認通過** — Run（node）：`manage.py test cloudsync node`；Expected: PASS
- [ ] **Step 5: Commit** — `git add cloudsync node templates/cloudsync`；`feat(cloudsync): upload node results in the sync cycle with stable identity`

---

### Task 6: 端對端與文件

**Files:**
- Create: `cloudsync/tests/test_e2e_results.py`
- Modify: `docs/architecture.md`、`docs/next-actions.md`、`README.md`

**Interfaces:**
- Consumes: 全部前述任務、C2 端對端輔助（`SEED`／`SUBMIT` 寫法）
- Produces: 無

- [ ] **Step 1: 寫失敗測試**

`cloudsync/tests/test_e2e_results.py`（結構同 C2 `test_e2e_inbox`，`CloudServer(inbox=True)`），三個測試：
1. `test_reply_analysis_upload_and_display`：雲端送出兩筆 → `run_cycle(force=True)` → 本機 `call_command("run_analysis_worker_once", worker_id="e2e", output=<temp>)` → `run_cycle(force=True)`；雲端 `shell` 讀 `get_published_analysis_payload(survey)`：`available` 為真、`node_result.is_latest` 為真、`coverage.analyzed_unique == 2`、`publish_sequence == 1`。
2. `test_new_reply_marks_the_result_old`：接續上一流程再送出一筆並 `run_cycle`（不重跑分析）；雲端 `node_result.pending_new == 1`、`is_latest` 為假、展示內容仍是序號 1。
3. `test_lost_upload_reply_is_resent_with_the_same_identity`：patch `CloudClient.post`，第一次 `results/` 在雲端已處理後拋 `CloudError(TRANSIENT)`（先呼叫原函式再拋出）；第二次 `run_cycle` 後本機 `ResultUpload.status == "uploaded"`，雲端 `PublishedResultRecord` 只有一筆。

- [ ] **Step 2: 執行確認失敗** — Run（node）：`manage.py test cloudsync.tests.test_e2e_results`；Expected: FAIL（尚未建立或斷言失敗）
- [ ] **Step 3: 補齊實作缺口**（若端對端暴露問題，回到對應任務修正並補單元測試）
- [ ] **Step 4: 兩模式全套件** — cloud：`manage.py test feedback accounts config cloudapi` PASS；node：`manage.py test feedback accounts config node organizations cloudapi cloudsync` PASS
- [ ] **Step 5: 文件（只寫現況）**
  - `docs/architecture.md` 同步段落補：結果上傳（`ResultUpload` 凍結身分、`PublishedResultRecord` 歷史、`SurveyAnalysisState` 原子切換三條件、網站三項新鮮度與「本機發布 #N」）、水位綁定的分析輸入。
  - `docs/next-actions.md`：雲端同步改為「C1–C3 已完成；接續 C4 搬移，之後處理通知系統」。
  - `README.md`：無新指令，不需修改。
- [ ] **Step 6: Commit** — `git add cloudsync docs`；`test(cloudsync): end-to-end result upload, freshness and lost reply resend`
