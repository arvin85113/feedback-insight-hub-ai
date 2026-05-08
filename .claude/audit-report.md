# 全面健檢報告

> 初次檢查：2026-04-20　|　最後更新：2026-04-28

---

## 2026-04-28 最新協作狀態

### 已完成

| 項目 | 狀態 |
|---|---|
| Flask `recommend_analysis` text/fallback 文字對齊 | ✅ `text` 獨立一個 elif，fallback 改為「建議先確認資料尺度」，與 Django 完全一致 |
| 停用問卷仍可填答 | ✅ `SurveyDetailView.dispatch` 原地渲染 `survey_notice`，不跳首頁 |
| 空問卷可被提交 | ✅ 同上，`questions.exists()` 失敗也原地渲染 |
| 重複填答無防護 | ✅ Customer 已填過同一問卷，原地渲染 `survey_notice`；Manager 免檢 |
| Single choice 改 RadioSelect | ✅ `SurveyFormBuilder._build_field` SINGLE_CHOICE 分支加 `widget=forms.RadioSelect` |
| Scale widget 與設定脫節 | ✅ 有 `options_text` 時改用 RadioSelect；無選項時 max 收斂為 5 |
| 通知中心「建立新通知」跳第一份問卷 | ✅ 改為下拉選擇問卷後才啟用按鈕；每筆通知旁加「同問卷新增」捷徑 |
| `builder_tabs` dead code 清除 | ✅ `SurveyBuilderView.get_context_data` 移除未使用的三 tab 列表 |

### 首頁問卷清單確認

`is_active=True` 過濾已分別在 `local_service.py:71` 和 Flask `app.py:80` 存在，未開放問卷不會出現在首頁 Active Surveys 區塊。

---

## 2026-04-27 最新協作狀態

### 已完成

| 項目 | 狀態 |
|---|---|
| Login-only UI 清理 | 已完成並推送：前台與 admin 不再顯示 `access_mode` / `source` |
| Pandas/SciPy 統計引擎 | 已完成並推送：commit `df065d9`，目前接在 Django fallback `feedback/local_service.py` |
| 推論統計輸出 | `StatsOverviewView` 會傳 `inferential_analysis`，`stats_overview.html` 已顯示推論統計區塊 |
| Schema 瘦身 | 已完成並推送：commit `94d4de7` |
| Supabase migration | 已套用 `feedback.0008_remove_feedbacksubmission_source_and_more` |

### Supabase schema 現況

| 表 | 已移除欄位 | 驗證 |
|---|---|---|
| `feedback_survey` | `access_mode` | confirmed missing |
| `feedback_feedbacksubmission` | `source` | confirmed missing |

目前產品已完全收斂成 login-only：`Survey.AccessMode`、`Survey.access_mode`、`FeedbackSubmission.source` 都不再存在於 active schema。

### Pandas 統計規格

本專案採用「分析用途導向資料型態」：不是純 Stevens 四尺度，也不是純資料科學的 categorical/numeric 二分法。目標是在題目建立階段收集足夠資訊，讓 Pandas 自動分析可以安全決定哪些資料能做描述統計、哪些能進推論統計。

| data_type / kind | 敘述統計 | 推論統計 |
|---|---|---|
| `continuous` | 有實際數量意義的數值：評分、金額、時間、比例；numeric chart：count / avg / median / min / max / std / 95% CI | 可作為 DV |
| `discrete` | 計數型或編碼型數值：拜訪次數、件數、等級編號；numeric chart | 不作為 DV |
| `nominal` single choice | 無順序分類：部門、地區、角色、問題類型；category chart | 可作為 IV |
| `nominal` multiple choice | 多重回應；split/explode frequency | 不作為 IV |
| `ordinal` | 有順序但間距不保證相等：非常滿意 / 滿意 / 普通 / 不滿意；category chart | 第一版不進 t-test / ANOVA |
| `text` | 交給 text-analysis | 不參與 |

推論規則：
- `nominal IV x continuous DV`：2 組跑 Welch t-test，3-5 組跑 one-way ANOVA，每組至少 2 筆有效數值；效果量分別為 Cohen's d / eta squared。
- `nominal x nominal`：單選名目題之間跑卡方獨立性檢定，效果量為 Cramer's V；多選名目題排除，避免一筆回覆同屬多組。
- `nominal IV x ordinal DV`：2 組跑 Mann-Whitney U，3-5 組跑 Kruskal-Wallis；ordinal 題必須有 `options_text` 才能安全轉換排序分數。
- `continuous x continuous`：Pearson correlation。
- 涉及 ordinal rank 的相關分析：Spearman correlation。
- 不符合條件回傳 `skipped_reason`，前端仍可顯示為跳過原因。

Builder UI 規則：
- `short_text` / `long_text` 固定為 `text`
- `single_choice` 由使用者選 `ordinal` 或 `nominal`
- `multiple_choice` 固定為 `nominal`
- `scale` 由使用者選 `continuous` 或 `ordinal`
- `integer` / `decimal` 由使用者選 `continuous` 或 `discrete`，目前 UI 預設 `continuous`

### 2026-04-28 Builder UI 最新狀態

- 題目卡片已加入表單式填答預覽，不再只是文字摘要。
- `scale` 題會顯示 radio-style 量表點；若有 `options_text` 就使用前 7 個選項，否則預設顯示 1-5。
- `single_choice` / `multiple_choice` 題會顯示前 5 個選項，並分別用 radio / checkbox 視覺。
- `short_text` / `long_text` / `integer` / `decimal` 題會顯示不可互動的輸入框或文字框預覽。
- 新增題目表單已顯示下一題題號，並在選擇題型時顯示輕量用途提示。
- Manager dashboard 的全域 Django messages 黃色橫幅已從 `dashboard_base.html` 移除；非後台頁面的 `base.html` messages block 保留。

### 2026-04-28 Manager / Customer UI 最新狀態

- 統計分析、文字分析、改善追蹤、通知中心已統一為「問卷列表 → 選定問卷 → 進入該功能詳情」的新式入口。
- 這些頁面均保留分類 filter-pill 與排序下拉，預設排序為最新建立；排序選項統一為最新建立、最舊建立、名稱 A-Z。
- 文字分析與通知中心的右上舊式問卷 selector / 執行按鈕已移除。
- 改善追蹤新增每份問卷的追蹤開關；停用後不能新增改善通知，避免 UI 顯示可操作但後端語意關閉。
- Customer Home 已整理為顧客端入口，包含帳戶摘要、填答紀錄、通知摘要；填答紀錄不再顯示答案預覽，避免出現「您的所屬單位」這類問卷答案片段。
- 填答紀錄摘要格式改為：`N 題已作答，提交時間：YYYY/M/D`。
- 顧客通知偏好頁已改為通知專用：全域通知開關 + 已填問卷列表 + 每份問卷追蹤開關；個人資料搬到 `/accounts/profile/`。
- Django fallback 與 Flask customer payload 均補上 `submitted_date` / `submitted_datetime`，避免 template 對 ISO 字串套 `date` filter 導致時間空白。

### 重要協作提醒

- 目前正式策略是 Django-only fallback；Render 的 Django service 建議不要設定 `FEEDBACK_SERVICE_URL`。
- Flask `/api/stats` 尚未接 Pandas `inferential_analysis`，若重新啟用 Flask，stats 頁會缺少新版推論統計結果。
- Google OAuth placeholder 必須保留，這是另一位組員的工作，不是過時殘留。
- `.claude/settings.local.json`、`CLAUDE.md`、`scripts/` 可能有本地未提交協作變更，提交功能改動時不要混入。

---

## Task 1：Model 欄位對齊（feedback/models.py vs services/feedback_service/models.py）

所有欄位已對齊，無待處理問題。

| Model | 狀態 |
|---|---|
| Survey | ✅ 全部對齊（slug 已補 unique=True） |
| Question | ✅ |
| FeedbackSubmission | ✅ |
| ImprovementUpdate | ✅ |
| ImprovementDispatch | ✅ |
| KeywordCategory | ✅ 已新增 SQLAlchemy model |

**blank=True vs NOT nullable 說明：** Django `blank=True` 是 form validation 層，不是 DB nullable。這些欄位 Django 端沒有 `null=True`，DB 實際 NOT NULL、寫入空字串。SQLAlchemy `Mapped[str]` 正確反映，無需修改。

> `SurveyCategory` 已補入 SQLAlchemy models；目前 Django source-of-truth 與 Flask ORM 映射已對齊。

---

## Task 2：業務邏輯一致性（Flask vs Django local_service）

| 項目 | 狀態 |
|---|---|
| 斷詞 regex | ✅ 一致 |
| 停用字 | ✅ 一致 |
| Top-N 數量 | ✅ 一致 |
| keyword.category 欄位 | ✅ 已修（Flask 補上） |
| data_type 顯示語言 | ✅ 已修（Flask 改回中文 label） |
| text 類型建議文字 | ⚠️ 輕微不一致（低優先，不影響功能） |

---

## Task 3：Template vs 後端資料結構

全部 ✅，無待處理問題。

---

## 2026-04-25 更新紀錄

### 問卷建立流程重構（commits 82913f7、ef76217）

**目標：** 簡化建立介面，移除使用者不需手動填寫的欄位。

| 變更 | 說明 |
|---|---|
| `SurveyCreateForm` 移除 `slug` | 改由 `form_valid` 用 `slugify(title)` 自動產生，衝突時加流水號 `-2`, `-3`... |
| `SurveyCreateForm` 移除 `access_mode` | 固定寫入 `Survey.AccessMode.LOGIN` |
| `SurveyCreateForm` 移除 `improvement_tracking_enabled` | 固定寫入 `True`；Admin 端設為 `readonly_fields` |
| `survey_create.html` 重設計 | 單欄佈局；`is_active` 改為 toggle UI；`thank_you_email_enabled` 改為 rich checkbox；加 info-callout |
| `app.css` 新增 | `.setting-block`、`.toggle-*`、`.checkbox-label-rich`、`.info-callout` |

---

### 新增 python-dotenv（commit d7d92c2）

- `requirements.txt` 加入 `python-dotenv==1.0.1`
- `config/settings.py` 啟動時自動 `load_dotenv(BASE_DIR / ".env")`
- 本地開發不再需要手動 export 環境變數

---

### 問卷分類功能（commit d7d92c2）

**新增 `SurveyCategory` model：**
- `name`（unique）、`created_at`
- migration `0007_add_survey_category`

**`Survey` 新增 `category` FK：**
- `null=True, blank=True, SET_NULL`
- `SurveyAdmin` 加入 `category` 欄位

**`SurveyCreateForm` 加入 `category`：**
- `ModelChoiceField`，`empty_label="── 選擇分類（選填）──"`

**`SurveyManagerView` 加入排序與篩選：**
- `?sort=newest`（預設）/ `?sort=oldest` / `?sort=title`
- `?category=<id>` 篩選分類
- context 新增：`categories`、`current_sort`、`current_category`

**`survey_manager.html` 更新：**
- 頂部加 toolbar：左側分類 filter-pill，右側排序 `<select>`
- 問卷列加分類 `.pill-category`

**`app.css` 新增：**
- `.manager-toolbar`、`.toolbar-filters`、`.filter-pill`、`.filter-pill-active`、`.sort-select`、`.pill-category`

---

### Survey Builder 三 Tab 重設計（commit 517603f）

**目標：** 將靜態裝飾 tab 改為可切換的真實功能介面。

**Tab 1：題目設定（questions）**
- 左欄（60%）：題目列表 + 每題 inline edit（`action=edit-question`）
  - 點「編輯」展開 edit form，同時只能開一個
  - 刪除按鈕保留
- 右欄（40%）：新增題目 form（原有 `QuestionCreateForm`，不變）

**Tab 2：回覆概況（responses）**
- 顯示回覆總數、最近回覆時間
- 連結到統計分析頁、文字洞察頁

**Tab 3：問卷設定（settings）**
- `SurveyEditForm`（新增）：`title`、`category`、`description`、`is_active`（toggle）、`thank_you_email_enabled`（checkbox）
- slug 唯讀 + 「複製連結」按鈕（`navigator.clipboard`）
- `action=update-survey`

**views.py 變更：**
- `SurveyBuilderView.post` 新增 `edit-question` 和 `update-survey` action
- `get_context_data` 新增 `survey_edit_form`、`latest_response`、`active_tab`
- redirect 帶 `?tab=<key>` 保留 tab 狀態

**forms.py 新增：**
- `SurveyEditForm`（ModelForm，fields 同上）

**app.css 新增：**
- `.tab-bar`、`.tab-btn`、`.tab-btn-active`
- `.builder-layout`（3fr 2fr grid，響應式）
- `.inline-edit-panel`、`.inline-edit-actions`
- `.responses-summary`、`.response-actions`
- `.slug-row`、`.slug-input`

---

## 全部修復與改動紀錄

| commit | 內容 |
|---|---|
| c562d5a | Flask text-analysis 補 category；stats data_type 改中文 label；新增 KeywordCategory SQLAlchemy model |
| 88b37d5 | Survey.slug SQLAlchemy 補 unique=True |
| 31d2f24 | local_service.get_dashboard_payload() NameError 修復（surveys 未定義，/dashboard/ 500） |
| a1458fa | 改善追蹤頁重設計：accordion UI、inline 新增表單、ImprovementListView 改 survey_groups context |
| 6b90257 | 註冊頁重設計：Google 登入預留位、兩層欄位結構 |
| ff99d94 | 修復 checkbox 寬度與文字換行（input[type=checkbox] 被全域樣式影響） |
| e4f38b6 | 註冊表單移除 last_name 和 organization |
| 3913cdb | 移除 QUICK/HYBRID access mode，統一改為 LOGIN；移除 QuickSurveyView；migration 0005+0006 |
| 4cf1efe | seed_demo.py 修復（HYBRID → LOGIN） |
| 446d0a5 | Manager sidebar 固定不隨頁面捲動（sticky + 100vh） |
| 9efdcef | Fix QUICK/HYBRID remnants, add CSRF_TRUSTED_ORIGINS, remove orphan CSS brace |
| 82913f7 | Remove slug/access_mode from survey create form, auto-generate slug from title |
| ef76217 | Enforce improvement_tracking, redesign survey create form with toggle and callout |
| d7d92c2 | Add survey category with filter/sort, redesign create form layout |
| 517603f | Implement tabbed survey builder with inline edit, responses, and settings tab |

---

## 目前待處理事項

| 優先 | 問題 | 說明 |
|---|---|---|
| ⚠️ 低 | Flask `/api/stats` 未接 Pandas 統計契約 | 若重新啟用 Flask stats，推論統計區塊會缺資料；目前 Django-only fallback 不受影響 |
| ⚠️ 低 | 題目排序仍是簡易上下移動 | 已可移動題目，但尚未做 drag-and-drop |

---

## 目前架構狀態

**Survey 存取模式：** 只剩 `LOGIN`，所有問卷必須登入才能填答。流程：掃 QR code → 未登入跳登入頁（帶 `?next=` 參數）→ 登入後繼續填。

**Survey 建立流程：** 使用者只需填 title / category / description / 功能開關，slug 自動產生，access_mode 和 improvement_tracking_enabled 由 view 強制寫入。

**Survey Builder：** 三 Tab 架構（題目設定 / 回覆概況 / 問卷設定），每個 tab 都有對應內容和 POST handler，tab 狀態透過 `?tab=` query param 保留。

**Manager Workspace：** 左側導覽面板固定不動，右側內容區獨立捲動。

**改善追蹤頁：** 新式問卷入口頁，選定問卷後顯示改善項目與 inline 新增表單；可切換單份問卷的改善追蹤開關。

**文字分析 / 通知中心：** 已改成與統計分析一致的問卷列表入口，不再使用右上舊式 selector。

**顧客端：** `/app/` 顯示帳戶摘要、填答紀錄與通知摘要；`/accounts/preferences/` 只管理通知偏好；`/accounts/profile/` 管理個人資料。

**註冊頁：** 欄位精簡為 username / first_name / email / password / notification_opt_in，頂部有 Google 登入預留位（disabled）。

**資料庫：** Supabase PostgreSQL，`DATABASE_URL` 需在 Render dashboard 兩個服務各自設定。Migration 0007 已加入 SurveyCategory。

**本地開發：** `python-dotenv` 自動載入 `.env`，不需手動 export。

---

## 2026-05-07 Merge 事故處理紀錄

### 問題來源

遠端 `origin/main` 合入組員文字分析更新時，同時帶入一條危險 migration：

- `feedback/migrations/0010_remove_answer_analysis_text_and_more.py`

該 migration 會移除 Supabase production 已存在且正在使用的三個欄位：

- `feedback_answer.analysis_text`
- `feedback_answer.analysis_version`
- `feedback_answer.sentiment_score`

這三欄是新版文字分析快取與情感分數資料流的一部分，不能在未規劃資料遷移的情況下刪除。

### 風險

如果 Render 部署時執行該 migration，PostgreSQL 會實際 `DROP COLUMN`，造成：

- 文字分析快取欄位消失
- 依賴 `analysis_text` / `sentiment_score` 的本地服務與管理命令失效
- Supabase production schema 與目前程式碼不一致

遠端另有 `build.sh` 的 `migrate feedback 0011 --fake` workaround。此做法同樣不保留，因為它會把 migration 標記為已執行但不實際修改 DB，容易讓 production schema 進入不可預期狀態。

### 採用解法

本次 merge 以「保留 production-safe schema」為原則：

- 保留本地既有安全 migration 鏈：`0007_add_is_read_to_improvementdispatch.py` + `0010_merge_20260505_2155.py`
- 移除遠端危險 migration：`0010_remove_answer_analysis_text_and_more.py`
- 移除遠端重複 migration：`0011_improvementdispatch_is_read.py`
- 移除 `build.sh` 的 `--fake` migration workaround
- 保留遠端安全功能：
  - pa75 Render 網址加入 `CSRF_TRUSTED_ORIGINS`
  - `text_analysis_summary()` / `category_sentiment_summary()` helper

### 驗證項目

合併後需確認：

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python -m py_compile feedback/models.py feedback/views.py feedback/local_service.py accounts/views.py
```

目前判定標準：

- `makemigrations --check --dry-run` 應顯示 `No changes detected`
- `migrate --plan` 不應出現會刪除 `Answer.analysis_*` 或 `sentiment_score` 的操作
