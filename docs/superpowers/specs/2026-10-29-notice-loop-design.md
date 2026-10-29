# 節點問卷的改善通知閉環與兩頁改版

目標：節點擁有的問卷也能走完「管理者建立改善 → 通知填答者 → 顧客在客戶端看到進度」，
並把通知中心（`/dashboard/notices/`）與客戶端（`/app/`、`/app/notifications/`）改為流程與時間軸版面。

## 決定

- 改善項目與通知繼續在雲端網站管理；節點、同步協定、收件匣都不變。
- 不改 schema、不新增 migration。收據型通知以既有欄位表達：`ImprovementDispatch.recipient_user` 有值、`submission` 為空。
- 本規格取代[雲端同步規格](2026-10-01-cloud-sync-design.md)「不在本階段範圍」中「顧客通知的雲端代寄」一項；
  「改善進度回寫節點」仍不在範圍內，`SubmissionReceipt.last_known_improvement_status` 不使用。

## 現況缺口

節點問卷的回覆在雲端只有 `SubmissionReceipt`（含 `user`、`consent_follow_up`，不含答案），沒有 `FeedbackSubmission`。

1. `notice_service.resolve_survey_respondents` 只查 `FeedbackSubmission`，節點問卷的寄送名單為空，建立批次會被拒絕。
2. `notice_service._current_dispatch_email` 在「問卷填答者」通知且 `submission` 為空時一律不寄。
3. 通知中心的問卷列表回覆數只數 `FeedbackSubmission`，節點問卷顯示 0。
4. 客戶端的填答紀錄以 `submission_id` 對應通知，收據列永遠沒有改善狀態。

## 1. 寄送名單與寄送前複核

**可通知的收據**：同一問卷、`consent_follow_up=True`、`user` 不為空、`status` 為 `received` 或 `synced`。
`quarantined`（衝突）與 `abandoned` 不通知；衝突經處理放回後轉為 `synced`，之後的批次會納入。

**名單**（`resolve_survey_respondents`）：既有 `FeedbackSubmission` 來源不變，再加上可通知的收據。
收據的 `submission_uuid` 已對應某筆 `FeedbackSubmission.idempotency_key` 者略過。
顧客資格與現有規則相同（啟用、信箱已驗證、`notification_opt_in`），收件人仍以 `recipient_key` 去重。
收據來源的 `NoticeRecipient.submission_id` 為 `None`。

**複核**（`_current_dispatch_email`）：「問卷填答者」通知且 `submission` 為空時，
改查 `improvement.survey` 與 `recipient_user` 是否仍有可通知的收據；查不到就不寄。
改善項目沒有綁問卷時查不到收據，結果為不寄。全站通知（`GLOBAL`）規則不變。

**撤回**：收據的 `consent_follow_up` 是填答當下的快照，事後不可改。顧客能改的只有帳號層級的 `notification_opt_in`，
關閉後所有批次都不寄給他。目前沒有針對單一問卷撤回追蹤的功能，本規格不新增。

**改善追蹤開關**：`improvement_tracking_enabled` 是發布後仍可改的設定（`cloudapi/definition.py` 的 `WHITELIST_FIELDS`），
切換會產生新的定義版本並同步到節點，但不推進分析定義版本（`cloudapi/writes.py` 的 `ANALYSIS_FIELDS`），已發布的分析結果不會因此過期。

## 2. 通知中心（管理者）

- 問卷列表保留「先選問卷」流程；回覆數改用 `cloudapi.receipts.received_counts`，與其他頁面的數量定義一致。
- 單一問卷頁依改善項目分組。每個改善項目有一條階段列，計算該項目各批次的狀態：
  草稿（`draft`）→ 寄送中（`sending`）→ 已完成（`sent`）→ 需處理（`partially_sent`、`failed`）。
  預覽不是狀態，維持為批次頁的一個步驟。
- 批次列顯示收件人數、寄送成功數、失敗數、站內已讀；有失敗時提供重試（沿用 `notice-batch-retry`）。
- **站內已讀** = 寄送成功的通知中 `is_read=True` 的比例。`is_read` 只在顧客於客戶端點開時設定，
  頁面須註明「不含 Email 開啟」，不得稱為開信率。
- 舊版通知紀錄（`legacy_notices`）保留為頁尾的相容區塊。
- 此頁繼承 `dashboard_base.html`。主檢出有另一項未提交的 manager shell 改版正在修改此檔與 `static/css/manager.css`，
  本頁排在實作最後，待該改版合併後以新版型完成。

## 3. 客戶端（顧客）

以手機寬度為主設計，桌面沿用同一結構。

**首頁 `/app/`：依問卷的時間軸**

- 每份顧客填過的問卷一張卡，來源是顧客的 `FeedbackSubmission` 與 `receipt_rows`（同一回覆只算一次）。
- 時間軸第一點為「已填答」與提交日期。之後依寄出時間列出寄給這位顧客、屬於這份問卷的通知：
  通知標題、寄出日期、已讀／未讀、改善項目目前狀態（已規劃、進行中、已完成）。
- 只放 `audience_type=survey_respondents` 的通知，且 `improvement.survey` 必須是顧客填過的問卷。
- 尚無通知時：收據為 `received` 或 `synced`、或為一般回覆，顯示「等待店家回應」；
  收據為 `quarantined` 時顯示「店家處理中」，不暗示會收到通知。
- 全站通知（`GLOBAL`）不進時間軸，另列為「店家公告」區塊。
- 只顯示顧客本來就會收到的內容：通知標題與內文、改善標題與摘要、改善狀態。
  不顯示 `internal_note`、優先度、到期日，也不顯示未寄給這位顧客的批次或草稿。

**通知紀錄 `/app/notifications/`**

- 歷史清單，未讀在前，依問卷分組；全站通知自成一組。標記已讀沿用 `MarkNoticeReadView`（已支援 `recipient_user`）。

## 4. 資料讀取

- 客戶端與時間軸的組裝放在 `feedback/local_service.py` 既有的 customer payload 函式，模板只做呈現。
  主檢出的同一檔案有另一項未提交改動，實作在從 `origin/main` 切出的工作樹進行，該改動合併後 rebase。
- 通知以「問卷＋顧客」對應收據。依現行填答限制，顧客每份問卷只能填一次（管理者豁免），對應唯一；
  若同一顧客在同一問卷有多筆回覆，所有該問卷的通知都顯示在同一張卡上。
- 不新增在 request 內的統計或分析；本頁只讀計數與狀態。

## 5. 驗證

- 測試一律用 `--settings=config.settings_test`（記憶體 SQLite）與 Django 測試郵件後端；不寄真信、不寫開發或正式資料庫。
- 寄送名單與複核：收據加入名單、不同意者排除、`notification_opt_in` 關閉者排除、`quarantined` 與 `abandoned` 排除、
  與 `FeedbackSubmission` 不重複、無問卷的改善項目不寄、全站通知行為不變。
- 端對端：以收據建立批次 → 寄送 → 顧客通知清單可見 → 標記已讀 → 通知中心站內已讀更新。
- 客戶端：時間軸只含該問卷的「問卷填答者」通知；全站通知在店家公告；衝突收據顯示「店家處理中」；不洩漏 `internal_note`。
- 通知中心：節點問卷的回覆數正確；階段列計數對應批次狀態。
- 實際寄信給顧客是管理者在雲端網站確認的操作；建立測試帳號或種子資料需另行授權。

## 不在範圍

改善進度回寫節點、單一問卷撤回追蹤、Email 開信追蹤、多組織資料隔離、收件匣加密（接入真實顧客回覆前仍須完成）。
