# System Architecture — Feedback Insight Hub

> **Current as of:** 2026-05-10
> **Initial design doc (2026-04-22):** `docs/architecture-initial.md`

---

## System Overview

問卷回饋洞察平台，提供問卷建立、回饋收集、統計分析、文字分析、改善追蹤與通知管理等功能。分為**顧客（Customer）**與**管理者（Manager）**兩種角色。後端以 Django 為主，並整合 Flask 微服務（目前實際部署使用 Django-only fallback）。

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

---

## Service Architecture

```
Browser
  |
  v
Django (port 8000)
  |-- Django ORM ─────────────────────────────→ Shared DB
  |-- feedback/service_client.py
        |-- Flask service (if FEEDBACK_SERVICE_URL set and healthy)
        └-- feedback/local_service.py (fallback, current recommended)
              |
              └─→ Django ORM → Shared DB

Shared DB:
  - local: db.sqlite3
  - production: Supabase PostgreSQL
```

**Circuit-breaker pattern:** `service_client.py` 先嘗試 Flask，失敗自動切換 `local_service.py`；`FEEDBACK_SERVICE_URL` 未設定則直接使用 local。

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
- 問卷管理列表：分類篩選、排序、統計 chips（題目 / 回覆 / 最近回覆）、3 日趨勢圖

### 3. 統計分析 (`/dashboard/stats/`)
- 問卷索引 → 選擇問卷 → 進入分析工作台
- 單一問卷頁：頂部 KPI 膠囊列 + 資料地圖 / 描述統計 / 推論分析 tab
- 描述統計：計數、平均、中位數、標準差、信賴區間、分布長條圖
- 推論統計（自動匹配）：Welch t-test、One-way ANOVA、Chi-square、Mann-Whitney U、Kruskal-Wallis、Pearson / Spearman 相關
- ⚠️ 推論統計目前僅 Django fallback 路徑實作；Flask `/api/stats` 尚未同步

### 4. 文字洞察 (`/dashboard/text-analysis/`)
- 問卷索引 → 選擇問卷 → 進入分析工作台
- 單一問卷頁：頂部 KPI 膠囊列 + 關鍵字摘要 / 情緒分布 / 分類規則 tab
- 字典驅動關鍵字提取 + 情緒分數（快取於 `Answer.analysis_text` / `sentiment_score`）
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
| 主框架 | Django 6.0.3 |
| 微服務 | Flask 3.1.2 |
| 資料庫 | SQLite（本機）/ Supabase PostgreSQL（生產） |
| ORM | Django ORM（主）+ SQLAlchemy（Flask 微服務鏡像） |
| 統計 | pandas + scipy（Django fallback） |
| 文字分析 | 字典驅動 pipeline（`feedback/text_pipeline.py`） |
| 靜態檔案 | Whitenoise |
| 部署 | Render |
| 前端 | Django templates + 純手寫 CSS（`static/css/app.css`） |

---

## Data Flow

```
顧客填答
  → FeedbackSubmission + Answer 寫入 DB
  → Manager 在 dashboard 檢視統計 / 文字分析
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
| `feedback/` | Main Django app: surveys, views, service client, stats, text pipeline |
| `feedback/data/` | Text-analysis dictionaries, keyword maps |
| `feedback/management/commands/` | Custom management commands |
| `feedback/local_service.py` | Django fallback: stats + text analysis |
| `feedback/service_client.py` | Circuit-breaker client to Flask |
| `feedback/text_pipeline.py` | Tokenization, sentiment, ANALYSIS_VERSION |
| `services/feedback_service/` | Flask microservice + SQLAlchemy models |
| `static/css/app.css` | Main stylesheet |
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
| 字典驅動文字分析 | 使用自訂詞典、同義詞正規化、情緒字典評分（`feedback/text_pipeline.py`），結果快取於 `Answer` 欄位，避免每次請求重算。 |
| Survey-index-first 流程 | 統計分析、文字洞察、改善追蹤、通知中心均採「先選問卷 → 再進入工作台」的流程，減少頁面跳轉並讓各功能聚焦於單一問卷。 |
| Django fallback 優先 | `service_client.py` 採 circuit-breaker 設計，Flask 不可用時自動使用 Django ORM 路徑；推論統計（Pandas/SciPy）目前只在 Django 路徑完整實作。 |

---

*初始設計文件（2026-04-22 版）請見 `docs/architecture-initial.md`。*
