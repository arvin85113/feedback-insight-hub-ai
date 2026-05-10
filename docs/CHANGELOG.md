# CHANGELOG

依日期反向排列。每個條目說明改了什麼、為什麼改、以及哪些檔案受到影響。

---

## 2026-05-10 — 問卷填答 UX 重構 + 管理頁面卡片統一

### 問卷填答頁（逐步式問卷）

- 填答頁（`survey_detail.html`）改為一題一頁的逐步式問卷，取代原本平鋪所有題目的設計。
- 新增步驟 0：顯示填答者唯讀資訊（姓名、Email 自動從 `request.user` 取得，不可修改），只保留 `consent_follow_up` 核取方塊可操作。
- 移除原本的「改善追蹤」KPI 卡（填答者視角不需要看到）。
- 步驟進度列（`.survey-progress-strip`）顯示目前步驟 / 總步數。
- 步驟 0 的下一步按鈕文字為「開始填答 →」；中間步驟為「下一題 →」；最後一步顯示「送出回饋」按鈕。
- `<form>` 加上 `novalidate`，避免瀏覽器原生 HTML5 驗證因隱藏步驟的必填欄位而阻擋送出。Django 伺服器端驗證仍正常運作。
- 每個步驟卡片有 `data-has-error` 屬性，JS 在後端驗證失敗時會導航到第一個有錯誤的步驟，而非切換回平鋪模式。

**相關修改：** `feedback/forms.py`（移除 `respondent_name`/`respondent_email`）、`feedback/views.py`（改從 `request.user` 讀取姓名 Email）、`templates/feedback/survey_detail.html`、`static/css/app.css`

### 感謝頁重設計

- `survey_success.html` 改用 `customer-workspace-body` 樣式 class。
- 新增 `.survey-success-panel`，包含深色圓形打勾圖示、標題、說明文字與行動按鈕。

### 問卷管理（Survey Manager）統計資訊強化

- 問卷管理列表卡片新增統計 chips（題目 / 回覆 / 最近回覆），取代純文字顯示。
- chips 字體大小 18px（`.survey-stat-chip-value`）。
- 新增 3 日新增回覆趨勢迷你柱狀圖（前天 / 昨天 / 今天），位於卡片可點擊區域內部右側（`.survey-trend-mini`）。
- `SurveyManagerView` 新增 `submission_count=Count("submissions")`、`latest_submission_at=Max("submissions__submitted_at")` 查詢 annotation；趨勢資料使用 `TruncDate` + `Count` 單次查詢後在 Python 端組裝。

**相關修改：** `feedback/views.py`、`templates/feedback/survey_manager.html`、`static/css/app.css`

### 統計分析 / 文字洞察 / 改善追蹤 / 通知中心 卡片格式統一

- 四個頁面（stats_overview、text_analysis、improvement_list、notice_center）的問卷列表卡片，統一使用與問卷管理相同的 `.survey-row-body` > `.survey-row-text` 版型。
- 各頁面顯示對應第一個 chip（題目 / 文字題 / 改善項目 / 通知）+ 回覆 + 最近回覆。
- CSS 選擇器由 `.survey-manager-row .record-row-link` 擴展至同時包含 `.stats-survey-row .record-row-link`，確保四個頁面都有相同的綠色背景與邊框效果。
- 四個 View 均新增 `latest_submission_at=Max("submissions__submitted_at")` annotation。

**相關修改：** `feedback/views.py`、`templates/feedback/stats_overview.html`、`templates/feedback/text_analysis.html`、`templates/feedback/improvement_list.html`、`templates/feedback/notice_center.html`、`static/css/app.css`

### Builder 題目預覽修正

- Scale 預覽移除 `|slice:":7"` 限制，CSS 改為 `flex-wrap: wrap`，可完整顯示 10 個以上選項。
- Builder header meta（回覆數 / 統計分析 / 文字洞察連結）擴大顯示，改用 `.builder-meta-link` 綠色外框按鈕樣式。

### Demo 資料修正

- `seed_demo.py` 的量化題（scale）補上 `options_text: "1\n2\n3\n4\n5\n6\n7\n8\n9\n10"`，避免新建 demo 後出現選項只有 1-5 的 bug。

---

## 2026-05-09 — UI 重設計、Gmail SMTP、統計分布圖

### 摘要

- 公開首頁、登入 / 註冊、密碼重設 / 變更、客戶端頁面改為更簡潔的視覺風格（參考 Claude Design）。
- 管理者工作台維持現有 manager dashboard shell，避免公開 / 客戶端 CSS 污染管理頁面。
- Gmail SMTP 密碼重設在本機以 Google App Password 測試通過。
- 統計概覽頁加入連續 / 離散數值題目的分布長條圖。
- 無 schema 變更。

### 公開頁面與認證頁面

| 頁面 | 說明 |
|---|---|
| 首頁 | 使用獨立 `public_base.html`；主動移除公開問卷列表（B2B login-only 定位）。 |
| 登入 / 註冊 | 改為安靜的公開視覺風格。 |
| 密碼重設 | 四個步驟模板全部重設計；本機 Gmail SMTP 送信測試通過。 |
| 密碼變更 | 對齊客戶帳號頁面風格。 |

### 客戶端頁面

| 頁面 | 說明 |
|---|---|
| `/app/` | 歡迎標題、KPI 卡、通知摘要、最新狀態卡。 |
| `/app/notifications/` | 通知歷史頁，對齊標題層級、KPI 卡、可捲動通知列表。 |
| `/accounts/preferences/` | 全域通知開關 + 各問卷追蹤開關分離。 |
| `/accounts/profile/` | 帳號資訊 + 個人資料欄位 + 修改密碼 CTA。 |
| 導覽列 | 簡化為首頁 / 客戶端 / 通知 / 個人資料 / 登出；通知與個人資料有可見的 active 狀態。 |

### 統計分析

- `feedback/local_service.py` 為連續 / 離散數值題目準備 `counts` 資料。
- `stats_overview.html` 在 `chart.counts` 存在時顯示數值分布長條圖，對齊類別題目的長條顯示方式。
- Pandas/SciPy 推論統計仍限 Django fallback；Flask `/api/stats` 尚未同步此格式。

### Email 設定

```env
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=<gmail-address>
EMAIL_HOST_PASSWORD=<google-app-password>
DEFAULT_FROM_EMAIL=<sender>
```

注意：`EMAIL_HOST_PASSWORD` 必須是 Google App Password（16 碼），不是 Google 帳號密碼。

**主要修改檔案：** `feedback/forms.py`、`feedback/views.py`、`feedback/management/commands/seed_demo.py`、`static/css/app.css`、`templates/feedback/dashboard.html`、`templates/feedback/dashboard_base.html`、`templates/feedback/improvement_list.html`、`templates/feedback/notice_center.html`、`templates/feedback/stats_overview.html`、`templates/feedback/survey_builder.html`、`templates/feedback/survey_detail.html`、`templates/feedback/survey_manager.html`、`templates/feedback/survey_success.html`、`templates/feedback/text_analysis.html`

---

## 2026-05-08 — 程式碼庫巡檢任務清單

以下是 2026-05-08 巡檢產生的任務，記錄狀態供追蹤：

| # | 項目 | 狀態 |
|---|---|---|
| 1 | `feedback/data/USAGE_GUIDE.md` 中 `seed_beverage_demo` 指令名稱錯誤（應為 `seed_demo_beverage`） | ⚠️ 待確認是否仍存在 |
| 2 | README Local Setup 指令跨平台問題（Windows cmd vs bash）| ✅ 已修正（README 現已分平台說明） |
| 3 | README 描述功能完整但測試檔為空樣板，文件與現實不符 | ⚠️ 待補充「測試覆蓋範圍」說明段落 |
| 4 | 建立最小可行測試集（註冊 / 登入 / 填答 / fallback 邏輯）| ⚠️ 尚未執行，列為 roadmap |

---

## 2026-05-07 — Merge Incident：危險 migration 攔截

### 事件說明

`origin/main` 的 teammate 更新包含一條危險 migration 鏈：

- `feedback/migrations/0010_remove_answer_analysis_text_and_more.py` — 會對 Supabase 執行 `DROP COLUMN` 移除 `Answer.analysis_text`、`analysis_version`、`sentiment_score`
- `feedback/migrations/0011_improvementdispatch_is_read.py` — 在前一個錯誤 migration 之後建立欄位
- `build.sh` 有 `python manage.py migrate feedback 0011 --fake` 的暫時繞過

上述欄位早已存在於 Supabase 生產資料庫，且正被文字分析 pipeline 使用。若套用 `0010` 會直接破壞生產環境。

### 處理方式

1. 合併 `origin/main` 但不立即 commit。
2. 保留本機安全 schema（`SurveyCategory`、`Survey.category`、`Answer` 三個分析欄位、`ImprovementDispatch.is_read`）。
3. 從合併結果中刪除危險 migration 檔案（`0010`、`0011`）。
4. 移除 `build.sh` 中的 fake migration 繞過。
5. 保留 teammate 安全的新增：`CSRF_TRUSTED_ORIGINS` 新增 Render URL、`text_analysis_summary()` / `category_sentiment_summary()` helper。

### 驗證指令（每次 PR 前必跑）

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python -m py_compile feedback/models.py feedback/views.py feedback/local_service.py accounts/views.py
```

---

## Schema 安全守則（常駐）

### 不得移除的欄位

| Model | Field | 說明 |
|---|---|---|
| `Survey` | `category` | 問卷分類與篩選 |
| `Answer` | `analysis_text` | 文字分析快取來源 |
| `Answer` | `sentiment_score` | 情緒分析支援 |
| `Answer` | `analysis_version` | 文字分析快取版本 |
| `ImprovementDispatch` | `is_read` | 客戶通知已讀狀態 |

### 不得加回的欄位

| Model | Field | 移除原因 |
|---|---|---|
| `Survey` | `access_mode` | 已全面改為 login-only，`0008` migration 移除 |
| `FeedbackSubmission` | `source` | 同上 |

---

*更新日期：2026-05-10。此檔案整合自原 `TASK_REVIEW_2026-05-08.md`、`docs/audit-2026-05-09.md`、`.claude/audit-report.md`。原始檔案可刪除。*
