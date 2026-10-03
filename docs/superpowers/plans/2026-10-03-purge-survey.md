# purge_survey 指令 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `purge_survey` 管理指令，在**目前的 schema** 上清除測試或模擬問卷及其所有關聯資料，並讓 `seed_demo_beverage --reset` 改用它。

**Architecture:** 刪除邏輯放在 `feedback/survey_purge.py`（指令、seed 與之後的草稿刪除共用）。單一交易內先處理規格 §7.4 列出的 `PROTECT`、孤兒與無外鍵關聯，再刪問卷，其餘由 `CASCADE` 處理。
dry-run 以「實際刪除後回滾」計數。本計畫不含任何 schema 變更，必須先於[問卷建立工具改版](2026-10-03-survey-builder-redesign.md)合併與部署。

**Tech Stack:** Django 6.0.8、SQLite（測試）。

**Spec:** [問卷建立工具改版 v4.2](../specs/2026-10-03-survey-builder-redesign-design.md) §5.1、**§7.4**、§7.5 第 1 步。

## Global Constraints

- 只用現有模型欄位；**不得新增 migration**（`makemigrations --check` 必須通過）。
- 不得把任何 `PROTECT` 改成 `CASCADE`。
- 整個刪除程序（含 dry-run）在 `feedback.analysis_jobs.suppress_analysis_scheduling()` 內執行，不得排程分析工作。
- `cloudsync` 只在本機模式安裝（`config/settings.py:227`）；用到 `PendingAck`、`SurveySyncState` 時以 `django.apps.apps.is_installed("cloudsync")` 判斷。
- 拒絕條件（`PurgeRefused`，指令轉為 `CommandError`）：雲端 `owner_node` 有值 →「指派給節點的問卷不能清除」；本機模式且問卷有 `SurveySyncState` →「由雲端同步的問卷不能在本機清除」。
- 找不到 slug：`CommandError("找不到問卷：<slug>")`。
- 指令輸出先印目前資料庫（沿用 `seed_demo_beverage._print_db_hint` 格式），不輸出連線秘密；無 `--confirm` 時最後印「dry-run：未刪除任何資料，加 --confirm 才會執行」。
- 測試：雲端 `.venv/Scripts/python.exe manage.py test feedback.test_survey_purge --settings=config.settings_test`；本機另以 `DEPLOYMENT_MODE=node` 執行同一指令。
- 實際對任何非測試 DB 執行需使用者另行授權（AGENTS.md）。

## Review Focus

1. 問卷有已寄出的改善通知、匯入來源、外部資料來源指標、revision、SurveyChange、收件匣項目、收據、結果上傳紀錄：清除成功、無殘留。
2. 刪除收件匣項目（pending 與 quarantined 都占容量）：`InboxCounter` 依每筆 `size_bytes` 退回一次，其他問卷的占用不受影響。
3. 刪除中途失敗：整個交易回滾，問卷、回覆、答案、通知都還在。
4. dry-run 之後資料庫完全不變（筆數、`InboxCounter`），且回報筆數與之後 `--confirm` 的實際筆數相同。
5. 其他問卷的改善發送紀錄指向本問卷回覆：保留且指標清空；`survey=NULL` 的舊改善紀錄不被刪除。

---

### Task 1: `purge_survey` 函式與指令

**Files:**
- Create: `feedback/survey_purge.py`
- Create: `feedback/management/commands/purge_survey.py`
- Modify: `feedback/management/commands/seed_demo_beverage.py`（`_reset`）
- Test: `feedback/test_survey_purge.py`

**Interfaces:**
- Produces: `PurgeRefused(Exception)`；`purge_survey(survey, *, dry_run: bool = False) -> dict[str, int]`
  （鍵為 `app_label.ModelName`，值為刪除筆數；`dry_run=True` 時在同一交易內執行後 `transaction.set_rollback(True)`，回傳相同結構）。

- [ ] **Step 1: 寫失敗測試** `feedback/test_survey_purge.py`。fixture `make_full_survey()` 建立：題目、兩筆回覆（一筆無答案）＋答案、`KeywordCategory`、
  `ImprovementUpdate`（`survey=` 本問卷）＋`ImprovementNotice`（status `sent`）＋`ImprovementDispatch`＋`ImprovementStatusHistory`、
  `DatasetImportBatch`＋`ImportedSubmissionSource`、`SurveyAnalysisSource`（`kind=external`）＋`ExternalDatasetVersion` 並設為 `active_external_version`、
  `SurveyAIReportSnapshot`＋`SurveyAIAnalysisStage`、`SurveyAnalysisState`、`AnalysisJob`、`SurveyDefinitionRevision`（version 1）＋`SurveyChange`（seq 1）。
  另有 `make_inbox_history(survey)`：建立 `NodeDevice`、pending `InboxSubmission`（`size_bytes=100`）、`SubmissionReceipt`、`PublishedResultRecord`，
  與 quarantined `InboxSubmission`（`size_bytes=50`）、另一問卷的 pending 項目（`size_bytes=30`），並把 `InboxCounter` 設為 `occupied_count=3, occupied_bytes=180`（模擬曾指派後解除：最後把本問卷的 `owner_node` 設回 `None`）。

```python
def test_dry_run_reports_real_counts_and_writes_nothing(self):
    survey = make_full_survey(); make_inbox_history(survey)
    before = snapshot_counts()                       # 各相關模型 count() 與 InboxCounter 值
    counts = purge_survey(survey, dry_run=True)
    self.assertEqual(snapshot_counts(), before)
    self.assertEqual(counts["feedback.Survey"], 1)
    self.assertEqual(counts["feedback.ImprovementNotice"], 1)
    self.assertEqual(counts["cloudapi.InboxSubmission"], 2)     # 本問卷的 pending 與 quarantined
    self.assertEqual(purge_survey(Survey.objects.get(pk=survey.pk)), counts)

def test_purge_leaves_nothing_of_target_and_keeps_other_survey(self):
    survey = make_full_survey(); other = make_inbox_history(survey)   # 回傳另一問卷
    purge_survey(survey)
    self.assertFalse(Survey.objects.filter(pk=survey.pk).exists())
    for model, lookup in TARGET_LOOKUPS:            # 例：(Question, "survey_id")、(InboxSubmission, "survey_id")、(ImprovementNotice, "improvement__survey_id")
        self.assertFalse(model.objects.filter(**{lookup: survey.pk}).exists(), model.__name__)
    for model in (ImprovementUpdate, ImprovementNotice, DatasetImportBatch, ImportedSubmissionSource,
                  SurveyAnalysisSource, ExternalDatasetVersion):
        self.assertEqual(model.objects.count(), 0, model.__name__)   # fixture 只為目標問卷建立這些
    self.assertTrue(Survey.objects.filter(pk=other.pk).exists())
    self.assertEqual(InboxSubmission.objects.filter(survey=other).count(), 1)
    counter = InboxCounter.objects.get()
    self.assertEqual((counter.occupied_count, counter.occupied_bytes), (1, 30))   # 只剩另一問卷的項目

def test_purge_schedules_no_analysis(self):
    survey = make_full_survey()            # 含外部資料來源與作用中版本、回覆與答案
    with mock.patch("feedback.analysis_jobs.schedule_survey_analysis") as schedule:
        purge_survey(survey, dry_run=True)
        purge_survey(Survey.objects.get(pk=survey.pk))
    schedule.assert_not_called()
    self.assertEqual(AnalysisJob.objects.count(), 0)

def test_other_surveys_dispatch_and_null_improvement_survive(self):
    survey = make_full_survey()
    other = other_improvement_dispatching_to(survey.submissions.first())
    orphan = ImprovementUpdate.objects.create(title="舊", survey=None)
    purge_survey(survey)
    other.refresh_from_db()
    self.assertIsNone(other.submission_id)
    self.assertTrue(ImprovementUpdate.objects.filter(pk=orphan.pk).exists())

@cloud_only
def test_node_owned_survey_is_refused(self): ...       # PurgeRefused「指派給節點的問卷不能清除」

def test_failure_rolls_back_everything(self):
    survey = make_full_survey()
    with mock.patch("feedback.survey_purge._delete_survey_row", side_effect=RuntimeError):
        with self.assertRaises(RuntimeError):
            purge_survey(survey)
    self.assertTrue(Survey.objects.filter(pk=survey.pk).exists())
    self.assertEqual((FeedbackSubmission.objects.count(), Answer.objects.count(), ImprovementNotice.objects.count()),
                     FULL_SURVEY_COUNTS)
```

  本機模式專用（`skipUnless(apps.is_installed("cloudsync"))`）：`test_node_removes_pending_acks_of_survey_replies`（`PendingAck` 依回覆 uuid 刪除、其他問卷的不動）、
  `test_node_refuses_synced_survey`（有 `SurveySyncState` → `PurgeRefused`）。
  指令：`test_command_without_confirm_is_dry_run`、`test_command_with_confirm_deletes`、`test_unknown_slug_raises`。
  seed：`test_seed_reset_works_with_revision_change_and_notice`（先 seed，再補 revision、`SurveyChange`、已寄出通知，`seed_demo_beverage --reset --yes` 成功重建）。

- [ ] **Step 2: 執行確認失敗** → ImportError（`feedback.survey_purge` 不存在）。
- [ ] **Step 3: 實作 `feedback/survey_purge.py`**：`suppress_analysis_scheduling()` 與 `transaction.atomic()` 內 `select_for_update` 鎖問卷 → 拒絕條件 → 依規格 §7.4 表格順序：
  通知 → 改善紀錄 → 匯入來源 → 清空 `active_external_version` → 該問卷的 `InboxSubmission`（沿用 ACK／abandon 的鎖序：先 `select_for_update().order_by("pk")` 鎖定正文列，刪除這些已鎖定的列，再依節點以**實際刪除列**的筆數與 `size_bytes` 一次 `F()` 退回 `InboxCounter`） → `SubmissionReceipt` → `PublishedResultRecord`
  → `SurveyChange` → `SurveyDefinitionRevision` → （本機）`PendingAck` → `_delete_survey_row(survey)`。每一步把 `QuerySet.delete()` 回傳的明細累加進結果。
  `dry_run=True` 時最後 `transaction.set_rollback(True)`。
- [ ] **Step 4: 實作指令** `purge_survey --survey <slug> [--confirm]`（無 `--confirm` 即 `dry_run=True`），印出各模型筆數。
- [ ] **Step 5: `seed_demo_beverage._reset` 改呼叫 `purge_survey(existing)`**，保留確認提示與 `--yes`。
- [ ] **Step 6: PostgreSQL 併發測試**：在 `cloudapi/test_postgres.py` 新增 `test_purge_and_ack_do_not_double_release_capacity`——同一正文由 ACK 與 purge 兩個連線同時處理，結束後 `InboxCounter` 與剩餘正文的實際總和相符；另測 purge 與 abandon 同時處理。指令：`.venv/Scripts/python.exe manage.py test cloudapi.test_postgres --settings=config.settings_postgres_test`（需獨立 `TEST_DATABASE_URL`，無法確認隔離就停止並回報）。
- [ ] **Step 7: 執行測試**（雲端與 `DEPLOYMENT_MODE=node`）→ PASS；`makemigrations --check --dry-run --settings=config.settings_test` → `No changes detected`。
- [ ] **Step 8: Commit** `feat: purge_survey command for test and simulated surveys`
