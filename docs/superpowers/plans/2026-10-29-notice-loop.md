# 節點問卷通知閉環與兩頁改版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 節點擁有的問卷能把改善通知寄給收據上的填答者，通知中心與客戶端改為流程與時間軸版面。

**Architecture:** 收據規則集中在 `cloudapi/receipts.py`，`feedback/notice_service.py` 用它擴充寄送名單與寄送前複核；
頁面資料由 `feedback/local_service.py`（客戶端）與 `feedback/notice_service.py`（通知中心）組好，模板只做呈現。無 schema 變更。

**Tech Stack:** Django 6.0、Django templates、`static/css/app.css`／`ui.css`／`manager.css`、Django `TestCase`。

**Spec:** `docs/superpowers/specs/2026-10-29-notice-loop-design.md`

## Global Constraints

- 不新增 migration、不改 models；`makemigrations --check --dry-run --settings=config.settings_test` 必須無變更。
- 測試一律 `--settings=config.settings_test`；寄信以 `@patch("feedback.notice_service.send_mail", return_value=1)` 或 locmem 後端，不寄真信、不連任何外部 DB。
- 可通知的收據：`consent_follow_up=True`、`user` 非空、`status` ∈ {`received`, `synced`}。
- 「站內已讀」= 寄送成功（`delivery_status=sent`）的 dispatch 中 `is_read=True` 的比例；頁面文案須含「不含 Email 開啟」。
- 階段列四段與對應狀態：草稿=`draft`、寄送中=`sending`、已完成=`sent`、需處理=`partially_sent`+`failed`。
- 顧客頁不得輸出 `internal_note`、`priority`、`due_date`。
- 收據相關測試類別加 `@cloud_only`（`feedback.test_utils`）。
- 實作在本工作樹；主檢出另有未提交的 `feedback/local_service.py`、`dashboard_base.html`、`manager.css` 改動，Task 5 須等其合併後 rebase 再做。

## Review Focus

1. 同一顧客同時有 `FeedbackSubmission` 與對應收據（同 uuid）→ 只收一次通知、客戶端只一張卡（Task 1、Task 2 測試）。
2. 顧客填答後關閉 `notification_opt_in`，批次已建立但尚未寄出 → 寄送前複核不寄（Task 1 測試）。
3. 寄送前收據轉為 `abandoned` → 不寄（Task 1 測試）。
4. 全站通知的改善項目屬於顧客沒填過的問卷 → 只出現在「店家公告」，不產生問卷卡（Task 2 測試）。
5. 改善項目 `survey` 為空的「問卷填答者」通知 → 名單為空、建立批次被拒（既有 `NoticeRecipientError`），不崩潰（Task 1 測試）。

---

### Task 1: 收據納入寄送名單與複核

**Files:**
- Modify: `cloudapi/receipts.py`
- Modify: `feedback/notice_service.py:85-120`（`resolve_survey_respondents`）、`:184-204`（`_current_dispatch_email`）
- Test: `feedback/test_notice_receipts.py`（新檔）

**Interfaces:**
- Produces: `cloudapi.receipts.NOTIFIABLE_STATUSES = ("received", "synced")`
- Produces: `cloudapi.receipts.notifiable_receipts(survey) -> QuerySet[SubmissionReceipt]`（條件見 Global Constraints，並排除 `submission_uuid` 已等於某筆 `FeedbackSubmission.idempotency_key` 者；`select_related("user")`）
- Produces: `cloudapi.receipts.has_notifiable_receipt(survey, user) -> bool`
- `notice_service` 以函式內 import 使用（`cloudapi.receipts` 頂層 import `feedback.local_service`，避免循環）。

- [ ] **Step 1: 寫失敗測試** `ReceiptRecipientTests`（`@cloud_only`，setUp 仿 `feedback/test_improvement_workflow.py:459` 的 manager／survey／improvement，問卷以 `NodeDevice.issue` + `assign_survey_to_node` 指派節點；收據用仿 `cloudapi/tests/test_result_display.py:18` 的 helper，多帶 `user`、`consent_follow_up`）：
  - `test_consenting_receipt_is_a_recipient`：`resolve_survey_respondents(survey)` 長度 1，`user_id == customer.id`，`submission_id is None`。
  - `test_excluded_receipts`：`consent_follow_up=False`、`user=None`、`status="quarantined"`、`status="abandoned"` 各一筆 → 名單為空。
  - `test_receipt_matching_a_submission_counts_once`：同一顧客一筆 `FeedbackSubmission(idempotency_key=u)` 與一筆 `submission_uuid=u` 的收據 → 名單長度 1 且 `submission_id` 為該 submission。
  - `test_receipt_dispatch_is_sent`：`prepare_notice_dispatches` + `send_notice_batch` 後 `notice.status == SENT`、dispatch `recipient_user == customer`、`submission is None`。
  - `test_recheck_skips_opt_out_and_abandoned`：prepare 後分別把顧客 `notification_opt_in=False`、把收據 `status="abandoned"` → `send_notice_batch` 後該 dispatch 非 `sent`、`send_mail` 未以其信箱呼叫。
  - `test_improvement_without_survey_has_no_recipients`：`improvement.survey=None` → `prepare_notice_dispatches` 拋 `NoticeRecipientError`。
- [ ] **Step 2: 執行確認失敗** — `python manage.py test feedback.test_notice_receipts --settings=config.settings_test`，預期名單相關測試 FAIL。
- [ ] **Step 3: 實作** `cloudapi/receipts.py` 三個介面；`resolve_survey_respondents` 在既有 submission 迴圈後附加收據來源（資格判斷沿用同一段 `is_active`／`is_email_verified`／`notification_opt_in`）；`_current_dispatch_email` 在 `SURVEY_RESPONDENTS` 且 `submission is None` 時改為 `has_notifiable_receipt(dispatch.notice.improvement.survey, dispatch.recipient_user)`，survey 或 user 為空即回 `None`，其餘既有規則不變。
- [ ] **Step 4: 執行確認通過**，並跑既有 `feedback.test_improvement_workflow` 確認無回歸。
- [ ] **Step 5: Commit** — `feat: notify node-survey respondents from submission receipts`

### Task 2: 客戶端時間軸資料

**Files:**
- Modify: `feedback/local_service.py:124-190`（`get_customer_home_payload`、`get_customer_notifications_payload`）
- Modify: `feedback/views.py:277-326`（`CustomerHomeView` 的狀態篩選移除，改用新 payload）
- Test: `feedback/test_customer_timeline.py`（新檔）

**Interfaces:**
- Consumes: Task 1 的 dispatch 形態（`recipient_user` 有值、`submission` 為空）。
- Produces: `get_customer_home_payload(user) -> dict`，鍵：
  - `survey_cards`: list，每項 `{"survey": {id,title,slug,category}, "submitted_date": str, "state": "waiting"|"processing"|"notified", "events": [notice_event...]}`，依最近活動新到舊；
  - `notice_event`: `{"id", "title", "summary", "sent_at", "is_read", "improvement_status_label"}`；
  - `announcements`: 全站通知的 `notice_event` list；
  - `unread_count`: int；`notification_opt_in`: bool。
- Produces: `get_customer_notifications_payload(user) -> dict`，鍵 `groups`: `[{"survey": {...}|None, "notices": [notice_event...]}]`（未讀在前；`survey=None` 為店家公告）、`notice_count`、`unread_count`。
- `state`：有事件 → `notified`；收據為 `quarantined` → `processing`；其他 → `waiting`。

- [ ] **Step 1: 寫失敗測試** `CustomerTimelineTests`（`@cloud_only`）：
  - `test_receipt_card_shows_sent_notice`：收據 + 已寄出 dispatch → 一張卡，`state == "notified"`，事件標題為通知主旨，`improvement_status_label == "進行中"`（improvement `status="in_progress"`）。
  - `test_global_notice_goes_to_announcements`：顧客只填過問卷 A，全站通知屬問卷 B → `survey_cards` 只有 A 且無事件，`announcements` 長度 1。
  - `test_quarantined_receipt_is_processing`：`state == "processing"`。
  - `test_submission_and_receipt_with_same_uuid_make_one_card`。
  - `test_unsent_and_other_customers_notices_hidden`：`pending` dispatch 與別人的 dispatch 不出現。
  - `test_internal_fields_not_rendered`：GET `feedback:customer-home`，回應不含 improvement 的 `internal_note` 字串。
  - `test_notifications_grouped_unread_first`：兩則同問卷（一讀一未讀）+ 一則全站 → `groups` 兩組，第一組首則 `is_read is False`，公告組 `survey is None`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作** 兩個 payload；問卷集合 = 顧客的 `FeedbackSubmission` 加 `receipt_rows(user)` 的問卷（同 uuid 只算一次，`receipt_rows` 已處理）；事件只取 `delivery_status=sent` 且 `notice.audience_type=survey_respondents` 且 `improvement.survey_id` 在該集合的 dispatch；`processing` 判斷需讀收據 `status`，在 `cloudapi/receipts.py` 加 `receipt_states(user) -> dict[survey_id, status]`。沿用 `serialize_notice` 的標題／摘要取法。`CustomerHomeView` 只把 payload 放入 context。
- [ ] **Step 4: 執行確認通過**，並跑既有引用這兩個 payload 的測試（`rg "customer-home|customer_home_payload" feedback cloudapi`）。
- [ ] **Step 5: Commit** — `feat: customer timeline payload grouped by survey`

### Task 3: 客戶端兩頁版面

**Files:**
- Modify: `templates/feedback/customer_home.html`、`templates/feedback/customer_notifications.html`
- Modify: `static/css/app.css`（`customer-*` 區塊；新 class 以 `customer-timeline-` 為前綴）
- Modify: `static/js/customer-notifications.js`（只在 DOM 結構變動處調整選擇器，標記已讀行為不變）
- Test: `feedback/test_customer_timeline.py`

**Interfaces:**
- Consumes: Task 2 的 `survey_cards`、`announcements`、`groups`。

- [ ] **Step 1: 寫失敗測試**：`test_home_renders_timeline_and_waiting_copy`（含「已填答」「等待店家回應」）、`test_home_renders_processing_copy`（「店家處理中」）、`test_announcements_section_rendered`（「店家公告」）、`test_notification_row_keeps_mark_read_hook`（含 `data-pk` 與 `notice-mark-read` URL）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作版面**：首頁為問卷卡＋縱向時間軸（已填答 → 各通知；未讀以 pill 標示），店家公告在卡片之後；通知頁依 `groups` 分組。以 375px 寬為主，無水平捲動；公開／顧客樣式不得影響管理頁（只用 `customer-` 前綴）。
- [ ] **Step 4: 執行確認通過**；`python manage.py runserver` 不在計畫內，畫面檢查由使用者授權後另行進行。
- [ ] **Step 5: Commit** — `feat: customer client timeline layout`

### Task 4: 通知中心資料

**Files:**
- Modify: `feedback/views.py:869-930`（`NoticeCenterView`）
- Modify: `feedback/notice_service.py`
- Test: `feedback/test_notice_center.py`（新檔）

**Interfaces:**
- Consumes: `cloudapi.receipts.received_counts(survey_ids) -> dict[int, int]`（既有）。
- Produces: `notice_service.NOTICE_STAGES = (("draft", "草稿", ("draft",)), ("sending", "寄送中", ("sending",)), ("done", "已完成", ("sent",)), ("attention", "需處理", ("partially_sent", "failed")))`
- Produces: `notice_service.notice_center_groups(survey) -> list[dict]`，每項 `{"improvement", "stages": [{"key","label","count"}], "batches": [{"notice", "recipient_count", "sent_count", "failed_count", "site_read_rate": int|None}]}`；`site_read_rate` 為百分比整數，寄送成功數為 0 時為 `None`。

- [ ] **Step 1: 寫失敗測試**：`test_node_survey_response_count_uses_receipts`（2 筆收據、0 筆 submission → 列表 context 該問卷 `response_count == 2`）；`test_stage_counts`（各狀態批次各一，`partially_sent`+`failed` 計入 attention=2）；`test_site_read_rate`（3 筆 sent、1 筆已讀 → 33；全未寄 → `None`）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**：列表的 `response_count` 改為 annotate 後以 `received_counts` 覆寫；單一問卷頁 context 加 `notice_groups = notice_center_groups(survey)`，保留 `legacy_notices`。
- [ ] **Step 4: 執行確認通過**。
- [ ] **Step 5: Commit** — `feat: notice center stages and site read rate`

### Task 5: 通知中心版面（manager shell 改版合併後）

**Files:**
- Modify: `templates/feedback/notice_center.html`
- Modify: `static/css/manager.css`（該檔合併後才存在於 main）
- Modify: `docs/architecture.md`（第 6、7 節改寫為現況）
- Test: `feedback/test_notice_center.py`

- [ ] **Step 1: 前置**：確認 manager shell 改版已在 `origin/main`，本分支 rebase；未合併則停在此並回報。
- [ ] **Step 2: 寫失敗測試**：`test_page_shows_stage_rail_labels`（四個階段名）、`test_site_read_copy`（含「站內已讀」與「不含 Email 開啟」）、`test_retry_link_only_when_failed`（`failed_count>0` 才有 `notice-batch-retry` URL）。
- [ ] **Step 3: 執行確認失敗**。
- [ ] **Step 4: 實作版面**：每個改善項目一條階段列，下方批次列（收件、成功、失敗、站內已讀與進度條、重試）；頁尾保留舊版通知；沿用 manager shell 元件與 token。同步更新 `docs/architecture.md` 第 6、7 節。
- [ ] **Step 5: 執行通過**，並跑 `makemigrations --check --dry-run --settings=config.settings_test` 與 `git diff --check`。
- [ ] **Step 6: Commit** — `feat: notice center pipeline layout`
