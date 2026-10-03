# purge_survey 指令 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `purge_survey` 管理指令，在**目前的 schema** 上清除測試或模擬問卷及其所有關聯資料，並讓 `seed_demo_beverage --reset` 改用它。

**Architecture:** 刪除邏輯放在 `feedback/survey_purge.py` 的一個函式（指令與 seed 共用），在單一交易內先刪除會擋住問卷刪除的 `PROTECT` 關聯與 `SET_NULL` 會留下孤兒的改善紀錄，再刪問卷，其餘由 `CASCADE` 處理。本計畫不含任何 schema 變更，必須先於[問卷建立工具改版](2026-10-03-survey-builder-redesign.md)合併與部署。

**Tech Stack:** Django 6.0.8、SQLite（測試）。

**Spec:** [問卷建立工具改版](../specs/2026-10-03-survey-builder-redesign-design.md) §5 第 1–2 步、§5.1。

## Global Constraints

- 只用現有模型欄位；**不得新增 migration**（`makemigrations --check` 必須通過）。
- 預設 dry-run，只列出筆數、不寫入；`--confirm` 才刪除；全部在一個 `transaction.atomic()` 內。
- `owner_node` 有值的問卷拒絕執行，訊息「指派給節點的問卷不能清除」，`CommandError`。
- 找不到 slug：`CommandError("找不到問卷：<slug>")`。
- 指令輸出開頭印出目前資料庫（沿用 `seed_demo_beverage._print_db_hint` 的格式），不輸出連線秘密。
- 測試：`.venv/Scripts/python.exe manage.py test <label> --settings=config.settings_test`；雲端專用行為加 `feedback.test_utils.cloud_only`。
- 執行此指令會寫入所選 DB；計畫只寫程式與測試，實際對任何非測試 DB 執行需使用者另行授權（AGENTS.md）。

## Review Focus

1. 問卷有已寄出的改善通知（`ImprovementNotice` 對 `ImprovementUpdate` 為 `PROTECT`）：清除成功，不留通知、發送紀錄或改善紀錄。
2. 問卷曾由外部資料匯入（`ImportedSubmissionSource.batch` 為 `PROTECT`）：清除成功。
3. 問卷已有 `SurveyDefinitionRevision`／`SurveyChange`（對問卷 `PROTECT`）但 `owner_node` 為空（曾指派後解除，或之後改版所有問卷都存 revision）：清除成功。
4. 刪除中途失敗：整個交易回滾，問卷與回覆都還在。
5. 改善紀錄的 `survey` 已是 `NULL`（來源問卷先前被移除）：不受影響、不被刪除。

---

### Task 1: `purge_survey` 函式與指令

**Files:**
- Create: `feedback/survey_purge.py`
- Create: `feedback/management/commands/purge_survey.py`
- Modify: `feedback/management/commands/seed_demo_beverage.py:302-318`（`_reset`）
- Test: `feedback/test_survey_purge.py`

**Interfaces:**
- Produces: `purge_counts(survey) -> dict[str, int]`（各模型將刪除的筆數，鍵為 `app_label.ModelName`）；
  `purge_survey(survey) -> dict[str, int]`（在交易內刪除並回傳實際筆數；`owner_node_id` 有值時 `raise PurgeRefused("指派給節點的問卷不能清除")`）；
  例外類別 `PurgeRefused(Exception)`。

- [ ] **Step 1: 寫失敗測試** `feedback/test_survey_purge.py`，建立一份「全套」問卷 fixture `make_full_survey()`：題目、回覆＋答案、`KeywordCategory`、
  `ImprovementUpdate`（`survey=` 該問卷）＋ `ImprovementNotice`（status `sent`）＋ `ImprovementDispatch`（`submission=` 該回覆）、
  `DatasetImportBatch` ＋ `ImportedSubmissionSource`、`SurveyAIReportSnapshot` ＋ `SurveyAIAnalysisStage`、`SurveyAnalysisState`、`AnalysisJob`、
  `SurveyDefinitionRevision`（version 1）＋ `SurveyChange`（seq 1）。測試：

```python
def test_dry_run_counts_without_writing(self):
    survey = make_full_survey()
    counts = purge_counts(survey)
    self.assertEqual(counts["feedback.Survey"], 1)
    self.assertEqual(counts["feedback.ImprovementNotice"], 1)
    self.assertEqual(counts["cloudapi.SurveyChange"], 1)
    self.assertTrue(Survey.objects.filter(pk=survey.pk).exists())

def test_purge_removes_everything_and_leaves_no_orphan_improvement(self):
    survey = make_full_survey()
    purge_survey(survey)
    self.assertFalse(Survey.objects.filter(pk=survey.pk).exists())
    for model in (ImprovementUpdate, ImprovementNotice, ImprovementDispatch, FeedbackSubmission, Answer,
                  DatasetImportBatch, ImportedSubmissionSource, SurveyDefinitionRevision, SurveyChange):
        self.assertEqual(model.objects.count(), 0, model.__name__)

def test_unrelated_improvement_with_null_survey_survives(self):
    orphan = ImprovementUpdate.objects.create(title="舊", survey=None)
    purge_survey(make_full_survey())
    self.assertTrue(ImprovementUpdate.objects.filter(pk=orphan.pk).exists())

def test_node_owned_survey_is_refused(self):
    survey = make_full_survey()
    survey.owner_node, _ = NodeDevice.issue("office")
    survey.save(update_fields=["owner_node"])
    with self.assertRaisesMessage(PurgeRefused, "指派給節點的問卷不能清除"):
        purge_survey(survey)

def test_failure_rolls_back(self):
    survey = make_full_survey()
    with mock.patch("feedback.survey_purge._delete_survey_row", side_effect=RuntimeError):
        with self.assertRaises(RuntimeError):
            purge_survey(survey)
    self.assertTrue(Survey.objects.filter(pk=survey.pk).exists())
    self.assertEqual(ImprovementNotice.objects.count(), 1)
```

  指令測試（`call_command`）：`test_command_without_confirm_is_dry_run`（輸出含「dry-run」與 `feedback.Survey: 1`，問卷仍在）、
  `test_command_with_confirm_deletes`、`test_unknown_slug_raises`（`CommandError`「找不到問卷：nope」）。
  seed 測試：`test_seed_reset_uses_purge_when_revisions_exist`——先跑 `seed_demo_beverage`，為該問卷補一筆 `SurveyDefinitionRevision` 與改善通知，
  再跑 `seed_demo_beverage --reset --yes`，不丟 `ProtectedError`。

- [ ] **Step 2: 執行確認失敗**
  Run: `.venv/Scripts/python.exe manage.py test feedback.test_survey_purge --settings=config.settings_test`
  Expected: FAIL / ImportError（`feedback.survey_purge` 不存在）

- [ ] **Step 3: 實作 `feedback/survey_purge.py`**
  刪除順序（spec §5.1）：該問卷的 `ImprovementNotice`（經 `improvement__survey`）→ `ImprovementUpdate`（`survey=`，dispatch 與 status history 隨 CASCADE）
  → `ImportedSubmissionSource`（`batch__survey=`）→ `cloudapi.SurveyChange` → `cloudapi.SurveyDefinitionRevision` → `_delete_survey_row(survey)`（`survey.delete()`，其餘 CASCADE）。
  `purge_counts` 用 `django.db.models.deletion.Collector` 對同一組物件計數（先收集上述明確刪除的 queryset，再收集問卷），不寫入。
  `purge_survey` 以 `transaction.atomic()` 包住，並先 `select_for_update` 鎖問卷列。

- [ ] **Step 4: 實作指令 `purge_survey --survey <slug> [--confirm]`**：印資料庫提示與各模型筆數；無 `--confirm` 時最後印「dry-run：未刪除任何資料，加 --confirm 才會執行」；
  `PurgeRefused` 轉成 `CommandError`。

- [ ] **Step 5: `seed_demo_beverage._reset` 改呼叫 `purge_survey(existing)`**，保留原本的確認提示與 `--yes`。

- [ ] **Step 6: 執行測試**
  Run: `.venv/Scripts/python.exe manage.py test feedback.test_survey_purge --settings=config.settings_test`
  Expected: PASS
  Run: `.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --settings=config.settings_test`
  Expected: `No changes detected`

- [ ] **Step 7: Commit**
```bash
git add feedback/survey_purge.py feedback/management/commands/purge_survey.py feedback/management/commands/seed_demo_beverage.py feedback/test_survey_purge.py
git commit -m "feat: purge_survey command for test and simulated surveys"
```
