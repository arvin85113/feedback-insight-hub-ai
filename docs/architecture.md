# System Architecture — Feedback Insight Hub

描述現行架構；狀態與待辦見 [next-actions](next-actions.md)，除錯線索見 [debugging-notes](debugging-notes.md)。

---

## System Overview

問卷回饋洞察平台，提供問卷建立、回饋收集、外部資料匯入、統計分析、文字分析、改善追蹤與通知管理等功能。分為**顧客（Customer）**與**管理者（Manager）**兩種角色。Django 是唯一後端與 ORM；Supabase PostgreSQL 是權威資料庫，本機 Worker／EXE 執行重運算。

---

## User Roles

### Customer（顧客）
- 透過 QR Code 或直連連結進入問卷頁面（須先登入）
- 登入後以逐步式問卷（一題一頁）填寫
- 在客戶端 (`/app/`) 檢視填答紀錄、追蹤狀態
- 接收改善通知（站內 + Email）
- 管理通知偏好與個人資料

### Manager（管理者）
- 透過 manager workspace (`/dashboard/`) 進行所有問卷與分析操作
- 管理問卷（建立、設定、Builder）
- 統計分析、文字洞察、改善追蹤、通知中心
- 管理者帳號由系統管理員建立；註冊頁只建立顧客帳號

### 共用登入入口
- 管理者與顧客使用同一個 `/accounts/login/`；登入後依角色導向（管理者 `/dashboard/`、顧客 `/app/`）
- 首頁「進入管理端／顧客端」帶 `?next=`；若入口與帳號角色不符，改導向該帳號自己的工作區，不會 403

---

## Service Architecture

```
Browser
  |
  v
Django (port 8000)
  |-- 權限、問卷、填答、排程
  └-- Django ORM ─────────────────────────────→ Shared DB

Windows EXE
  |-- 本機固定版本 Parquet（大型外部資料）
  |-- 第一段：統計與文字分析
  |-- 第二段：Gemini 綜合解析
  └-- Django ORM ─────────────────────────────→ Shared DB

Shared DB:
  - local: db.sqlite3
  - production: Supabase PostgreSQL
```

同一套程式另有本機節點模式（`DEPLOYMENT_MODE=node`）：EXE 以 cheroot 在 `127.0.0.1` 提供主控台、
本機 SQLite 為資料庫、allauth 本機帳號登入，並監督 `run_analysis_worker` 子程序（以 `run\worker.heartbeat` 回報狀態）。
`cloud` 模式不載入 `node`、`organizations` 與 allauth，也沒有 `/setup/`、`/node/` 網址。
設計與範圍見 [架構總覽](superpowers/specs/2026-09-30-local-node-architecture-design.md)、
[主控台與登入](superpowers/specs/2026-09-30-local-node-console-and-auth-design.md)。

網站與本機工作台共用 Django models、工作協調與分析輸入契約，不維護第二套 HTTP domain service 或 ORM 鏡像。

---

## Core Feature Modules

### 1. 首頁 (`/`)
- 公開首頁，介紹平台功能（B2B 定位，無公開問卷列表）
- 導向登入 / 註冊

### 2. 問卷管理 (`/dashboard/forms/`)
- 建立問卷：支援題型 — 簡答 / 詳答、單選、多選、量化（scale）、整數、小數
- 題目資料型別：`continuous` / `discrete` / `nominal` / `ordinal` / `text`（對應統計分析方法）
- 問卷設定：標題、分類（SurveyCategory）、說明、是否開放、感謝信
- Survey Builder：題目設定 tab + 問卷設定 tab，含題目預覽；頂部使用與分析二級頁一致的 KPI 膠囊列
- 問卷管理列表：分類篩選、排序、統計 chips（題目 / 回覆 / 最近回覆）、3 日趨勢圖；外部資料集來源的問卷顯示資料集筆數

### 3. 統計分析 (`/dashboard/stats/`)
- 問卷索引 → 選擇問卷 → 進入分析工作台
- 單一問卷頁：頂部 KPI 膠囊列 + 資料地圖 / 描述統計 / 推論分析 tab
- 描述統計：計數、平均、中位數、標準差、信賴區間、分布長條圖
- 推論統計（自動匹配）：Welch t-test、One-way ANOVA、Chi-square、Mann-Whitney U、Kruskal-Wallis、Pearson / Spearman 相關
- 推論統計由共用 Django 分析服務與本機 Worker 使用同一 `AnalysisInput` 合約。

### 4. 文字洞察 (`/dashboard/text-analysis/`)
- 問卷索引 → 選擇問卷 → 進入分析工作台
- 單一問卷頁：頂部 KPI 膠囊列 + 關鍵字摘要 / 情緒分布 / 分類規則 tab
- 字典驅動關鍵字提取 + 情緒分數（快取於 `Answer.analysis_text` / `sentiment_score`）；頁面只讀已發布結果
- 文字雲（關鍵字模式 / 分類模式可切換）+ 三欄精簡關鍵字卡
- 分類情緒分布：每個分類一張卡，正向 / 中性 / 負向 stacked bar 內直接顯示數字
- 關鍵字分類規則（`KeywordCategory`）支援新增、inline 編輯、刪除

### 5. 改善追蹤 (`/dashboard/improvements/`)
- 問卷索引 → 選擇問卷 → 查看改善項目 + 建立新通知
- 每份問卷有改善追蹤開關（`Survey.improvement_tracking_enabled`）
- 建立改善通知 → 觸發 Email 發送給符合條件的填答者

### 6. 通知中心 (`/dashboard/notices/`)
- 問卷索引 → 選擇問卷 → 查看已發布改善通知
- 管理者視角，確認哪些通知已寄出

### 7. 客戶端 (`/app/`)
- 填答紀錄（狀態篩選：全部 / 待追蹤 / 追蹤中 / 已改善）
- 通知歷史（AJAX 標記已讀）
- 個人資料 (`/accounts/profile/`)
- 通知偏好 (`/accounts/preferences/`)

---

## Survey Fill Flow (Current)

```
掃描 QR Code
  → 未登入 → redirect to /accounts/login/?next=<path>
  → 已登入 → /survey/<slug>/
      → Step 0：唯讀填答者資訊 + consent_follow_up 核取
      → Step 1–N：逐步填答（一題一頁）
      → 最後一步：送出
      → /survey/<slug>/success/：感謝頁
```

**限制：**
- 問卷未開放 → 顯示提示，隱藏表單
- 問卷無題目 → 顯示提示，隱藏表單
- 顧客已填答 → 顯示提示，隱藏表單（管理者豁免）

---

## Backend Tech Stack

| 項目 | 技術 |
|---|---|
| 主框架 | Django 6.0.8 |
| 資料庫 | SQLite（本機）/ Supabase PostgreSQL（生產） |
| ORM | Django ORM |
| 統計 | pandas + scipy |
| 文字分析 | 字典驅動 pipeline（`feedback/text_pipeline.py`） |
| 靜態檔案 | Whitenoise |
| 部署 | Render |
| 前端 | Django templates；設計變數與既有樣式在 `static/css/app.css`，元件層在 `static/css/ui.css`；頁面腳本在 `static/js/` |
| AI | Gemini（`GEMINI_MODEL`，Vertex express mode），只由本機工作台／Worker 呼叫；AI 文字中的數字須對應 evidence |

---

## Data Flow

```
網站填答
  → Supabase 的 Survey + Question + FeedbackSubmission + Answer
大型固定外部資料
  → Supabase 的 Survey + Question + 不可變分析來源版本；完整列留在版本化本機 Parquet
兩種來源
  → 交易後更新版本並合併 AnalysisJob
  → 本機 Worker／EXE 透過 AnswerInput 或 ParquetInput 計算統計 / 文字分析
  → 可選擇由 Gemini 產生第二段解析
  → 新 Snapshot／Stage 發布回 Supabase，舊版本保留
  → Render 只讀最新成功結果
  → 建立 ImprovementUpdate
  → ImprovementDispatch 寄出通知 Email 給相關顧客
  → 顧客在 /app/notifications/ 查看並標記已讀
```

---

## Key File Locations

| Path | Purpose |
|---|---|
| `config/` | Django settings, root URLs, WSGI/ASGI |
| `accounts/` | User model, auth views, signup, profile, preferences |
| `feedback/` | Main Django app: surveys, views, jobs, stats, text pipeline |
| `feedback/data/` | Text-analysis dictionaries, keyword maps |
| `feedback/management/commands/` | Custom management commands |
| `feedback/local_service.py` | Django domain service: stats + text analysis + submission |
| `feedback/analysis_jobs.py` | 版本失效、工作租約與發布協調 |
| `feedback/analysis_adapters.py` | Answer／Parquet 共用分析輸入契約 |
| `feedback/text_pipeline.py` | Tokenization, sentiment, ANALYSIS_VERSION |
| `feedback/published_analysis.py` | 頁面讀取已發布結果 |
| `feedback/ai_stage_service.py` / `feedback/ai_grounding.py` | Gemini 三階段分析與數字驗證 |
| `config/health.py` | 健康檢查與資料庫無法連線時的 503 頁 |
| `desktop_app/` | Windows 本機工作台（EXE） |
| `static/css/` / `static/js/` | 樣式與頁面腳本 |
| `templates/` | All Django templates |

---

## Architecture Decision Notes

以下說明現況架構的關鍵設計決策，以及背後的原因：

| 決策 | 說明 |
|---|---|
| Login-only 填答 | 所有問卷填答須先登入，無訪客或匿名入口。確保填答紀錄與顧客帳號綁定，支援改善通知回推。 |
| 逐步式問卷（一題一頁） | 降低填答認知負擔，使用 `data-has-error` 屬性支援後端驗證錯誤的步驟導航。 |
| Analysis-purpose data type model | 採 `continuous / discrete / nominal / ordinal / text` 分類，對應各統計方法的適用條件，而非純統計學的 Stevens 四等級。 |
| 自動推論方法匹配 | 系統根據題目資料型別組合自動選擇檢定方法（Welch t-test、ANOVA、Chi-square、Mann-Whitney、Kruskal-Wallis、Pearson、Spearman），不符條件時回傳 `skipped_reason` 而非靜默略過。 |
| 字典驅動文字分析 | 使用自訂詞典、同義詞正規化、情緒字典評分（`feedback/text_pipeline.py`）；網站提交只保存原始答案並排程，正式統計／文字計算由本機 Worker 執行。 |
| Survey-index-first 流程 | 統計分析、文字洞察、改善追蹤、通知中心均採「先選問卷 → 再進入工作台」的流程，減少頁面跳轉並讓各功能聚焦於單一問卷。 |
| Django 單一後端 | Render 與問卷寫入使用 Django，避免雙 ORM 與逾時 fallback 造成重複寫入。 |
| 頁面只讀已發布結果 | 統計、文字與 Gemini 由本機工作台／Worker 計算後版本化發布；網站不在 request 內運算，Free 方案也不會逾時。 |
| 大型資料留在本機 | 大型固定外部資料以本機 Parquet 分析，Supabase 只存定義、來源版本與發布結果，避免超過 500 MB 上限。 |
| 問卷／題目生命週期分離 | `Survey.is_active` 控制填答、`analysis_enabled` 控制分析、`archived_at` 保留歷史；題目以 `code` 作穩定識別並可停用。 |
| 回覆與匯入可追溯 | 回覆具有冪等鍵、寫入時間、完整／作廢狀態；外部來源以 namespace＋record key 去重，內容改變列為衝突而不覆寫。 |
