# Feedback Insight Hub

Feedback Insight Hub 是一套以登入制問卷為核心的顧客回饋管理平台。系統把「問卷建立、填答紀錄、統計分析、文字洞察、改善追蹤、通知回推」串成同一個工作流，目標不是只收集表單，而是讓管理者能把回饋轉換成可追蹤的改善行動。

目前專案以 Django 為主要執行入口，並保留 Flask feedback domain service 的微服務設計。正式 demo / Render 部署建議先使用 Django fallback 路徑，除非 Flask service 已部署且確認 schema 與統計 payload 完全相容。

## Current Status

- 問卷已改為 100% login-only，舊的 quick / hybrid access mode 已從 UI、schema 與 payload 中移除。
- Django 是目前主要穩定服務：頁面、登入、ORM、問卷填答、統計、文字分析與改善追蹤都可透過 Django fallback 運作。
- Flask service 仍存在於 `services/feedback_service/`，但 `/api/stats` 尚未完整同步 Django fallback 的 Pandas/SciPy 推論統計格式。
- 生產資料庫使用 Supabase PostgreSQL；本地預設使用 SQLite。
- Manager workspace 目前包含問卷管理、統計分析、文字洞察、改善追蹤、通知中心。
- Customer portal 目前包含填答紀錄、通知摘要、個人資料設定與通知偏好設定。
- Google OAuth / Google login placeholder 必須保留，這是另一位組員負責的整合項目。

## Tech Stack

| Layer | Tech |
|---|---|
| Web app | Django 6.0.3 |
| Domain service | Flask 3.1.2 |
| Database | SQLite local / Supabase PostgreSQL production |
| ORM | Django ORM + SQLAlchemy mirror models |
| Statistics | pandas + scipy |
| Text analysis | jieba + dictionary-based keyword / sentiment pipeline |
| Static files | Whitenoise |
| Deployment | Render |
| Frontend | Django templates + custom CSS only |

No frontend framework is used. Most UI styling lives in `static/css/app.css`.

## Repository Structure

```text
accounts/                    User model, auth views, signup, profile, preferences
config/                      Django settings, root URLs, WSGI / ASGI
feedback/                    Main Django app: surveys, views, local service, stats, text pipeline
feedback/data/               Text-analysis dictionaries and keyword maps
feedback/management/commands Custom seed / rebuild / sync commands
services/feedback_service/   Flask microservice and SQLAlchemy models
static/css/app.css           Main handcrafted stylesheet
templates/                   Django templates
build.sh                     Render build script
render.yaml                  Render blueprint
```

## Core Architecture

```text
Browser
  |
  v
Django app
  |-- Django ORM -> shared database
  |-- feedback/service_client.py
        |-- Flask service if FEEDBACK_SERVICE_URL is set and healthy
        |-- feedback/local_service.py fallback otherwise

Shared database:
  - local: db.sqlite3
  - production: Supabase PostgreSQL
```

`feedback/service_client.py` implements a circuit-breaker style fallback. If `FEEDBACK_SERVICE_URL` is not set, Django uses `feedback/local_service.py` directly. This is the recommended current deployment mode because the Django fallback contains the newest Pandas/SciPy statistics contract.

## Main Product Flow

1. Manager creates a survey and questions.
2. Customer logs in and fills the survey.
3. Manager reviews response volume and descriptive statistics.
4. System recommends and runs eligible inferential tests.
5. Text answers are normalized, tokenized, categorized, and summarized.
6. Manager creates improvement updates from insights.
7. Customers receive improvement notifications and can track follow-up status.

## Key Features

### Manager Workspace

- Survey management with category filter and sort controls.
- Survey builder with compact KPI header, question preview, data type hints, QR code, and settings tab.
- Stats analysis index: select a survey, then inspect data map, descriptive statistics, and inferential analysis tabs.
- Text analysis index: keyword frequency, category sentiment cards, and editable keyword rules.
- Improvement tracking: per-survey tracking toggle and improvement update creation.
- Notice center: survey-first notification management.

### Customer Portal

- Customer home dashboard with account summary.
- Submission records with status filters.
- Notification summary and read status.
- `/accounts/profile/` for profile data.
- `/accounts/preferences/` for global and per-survey notification preferences.

### Statistics Engine

The project uses an analysis-oriented data type model:

| Data type | Meaning | Analysis behavior |
|---|---|---|
| `continuous` | meaningful numeric magnitude, e.g. rating, amount, duration | numeric summary, t-test / ANOVA DV, Pearson correlation |
| `discrete` | count-like or code-like number | numeric summary only |
| `nominal` | unordered category, e.g. department, region | frequency chart, IV for selected tests |
| `ordinal` | ordered category without guaranteed equal spacing | frequency chart, rank tests where safe |
| `text` | open-ended answer | text-analysis pipeline |

Implemented inferential methods in Django fallback include:

- Welch independent-samples t-test
- One-way ANOVA
- Chi-square test of independence
- Mann-Whitney U
- Kruskal-Wallis
- Pearson correlation
- Spearman correlation

Invalid or unsafe combinations return `skipped_reason` instead of silently producing misleading results.

### Text Analysis

Text analysis is dictionary-driven and cached on `Answer` rows.

#### jieba（結巴）中文斷詞

- 依賴套件：`requirements.txt` 內含 `jieba`。
- 主要實作：`feedback/text_pipeline.py`；環境有安裝時會用 `jieba.cut()` 做中文斷詞，未安裝則退回較簡單的切詞邏輯，不會讓整站無法啟動。
- 寫入與回填：`feedback/local_service.py` 在文字題填答寫入時，以及 `python manage.py rebuild_text_analysis` 回填歷史資料時，會透過 `build_analysis_text()` 產生 `Answer.analysis_text` 與 `sentiment_score`。
- 字典與規則：停用詞、同義詞、情緒詞與分類規則在 `feedback/data/`，並搭配 `KeywordCategory`（DB）做關鍵字→大分類映射。
- 與文字雲的差異：文字洞察頁的關鍵字頻率／文字雲目前主要走 `feedback/models.py` 的 `tokenize_feedback()`（正則切詞 + DB 規則），與 `text_pipeline.py` 的 jieba 路徑並非同一條；若兩邊結果不一致，請先確認是否已執行 `rebuild_text_analysis`，以及 DB 規則是否已 `sync_keyword_categories`。

Important files:

```text
feedback/text_pipeline.py
feedback/data/
feedback/local_service.py
services/feedback_service/app.py
```

Important cached fields on `Answer`:

- `analysis_text`
- `sentiment_score`
- `analysis_version`

Useful commands:

```bash
python manage.py rebuild_text_analysis --dry-run
python manage.py rebuild_text_analysis
python manage.py rebuild_text_analysis --survey <survey-slug>

python manage.py sync_keyword_categories --dry-run
python manage.py sync_keyword_categories
python manage.py top_uncategorized_keywords --survey <survey-slug>
```

Text-analysis payload contract from `service_client.get_text_analysis(slug)`:

- `keywords`: keyword frequency rows
- `summary`: coverage + average sentiment score
- `category_sentiments`: per-category positive / neutral / negative counts

Text-analysis rule source-of-truth policy:

- Runtime source: `KeywordCategory` in database (Django / Supabase).
- Versioned seed source: `feedback/data/keyword_category_map.json`.
- Recommended workflow: edit JSON in git, then sync to DB via management command.
- Manager UI also supports creating, inline-editing, and deleting per-survey keyword rules from the text-analysis rules tab.

### Keyword Rule Sync SOP

Use this when updating keyword-to-category mappings:

1. Edit `feedback/data/keyword_category_map.json` and commit the change.
2. Preview sync result:

```bash
python manage.py sync_keyword_categories --dry-run
```

3. Apply to database:

```bash
python manage.py sync_keyword_categories
```

4. Verify expected rule counts on target survey and then check text-analysis page.

Do not rely on ad-hoc manual DB edits as the primary process; JSON + sync keeps rules auditable and consistent across environments.

### Text Analysis Troubleshooting

If the text-analysis page shows keywords but sentiment distribution appears empty, check:

1. `TextAnalysisView` passes `analysis_summary` and `category_sentiments` to template context (not just `keywords`).
2. Historical answers may not have cached `analysis_text` / `sentiment_score` yet. Rebuild once:

```bash
python manage.py rebuild_text_analysis
```

3. In `feedback/models.py`, avoid duplicate helper definitions for `text_analysis_summary()` and `category_sentiment_summary()`. Duplicate definitions can silently override newer sentiment logic.

### 文字分類規則操作 SOP（給操作人員）

適用對象：維護文字洞察 / 文字雲關鍵字分類的人員。

核心原則：

- 版控來源：`feedback/data/keyword_category_map.json`
- 執行來源：資料庫 `KeywordCategory`（本機 DB / Supabase）
- 標準流程：先改 JSON，再同步 DB；不要只改 DB

#### 新增或修改規則

1. 編輯 `feedback/data/keyword_category_map.json`。
2. 先預覽（不寫入）：

```bash
python manage.py sync_keyword_categories --dry-run
```

3. 正式套用：

```bash
python manage.py sync_keyword_categories
```

4. 到文字洞察頁面確認分類與文字雲是否符合預期。

#### 刪除規則（重要）

目前 `sync_keyword_categories` 以新增/更新（upsert）為主，不會自動刪除資料庫中已存在但 JSON 已移除的舊規則。

建議刪除流程：

1. 先從 JSON 移除該規則。
2. 執行 dry-run 與正式同步。
3. 若資料庫仍有舊規則，再於資料庫刪除該筆。
4. 重新整理文字洞察頁面驗證結果。

#### 常用參數

- 指定問卷：

```bash
python manage.py sync_keyword_categories --survey <survey-slug>
```

- 覆寫門檻：

```bash
python manage.py sync_keyword_categories --threshold <number>
```

- 指定 JSON 檔：

```bash
python manage.py sync_keyword_categories --file feedback/data/keyword_category_map.json
```

## Local Setup

### 1. Create environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux / WSL:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env`:

Windows PowerShell:

```powershell
copy .env.example .env
```

macOS / Linux / WSL:

```bash
cp .env.example .env
```

For local SQLite development, `DATABASE_URL` can be omitted. For Supabase / PostgreSQL, set `DATABASE_URL` in `.env`.

Minimum local variables:

```text
DJANGO_SECRET_KEY=replace-me
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-me
```

If you are not running Flask locally, leave `FEEDBACK_SERVICE_URL` unset.

### 3. Initialize database

```bash
python manage.py migrate
python manage.py ensure_superuser
python manage.py seed_demo
```

### 4. Run Django

```bash
python manage.py runserver
```

Open:

```text
http://127.0.0.1:8000/
```

### 5. Optional: run Flask service

Only run this when you specifically want to test the microservice path:

```bash
python -m flask --app services.feedback_service.app run --host 127.0.0.1 --port 5001
```

Then set:

```text
FEEDBACK_SERVICE_URL=http://127.0.0.1:5001
```

## Important Environment Variables

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django secret key |
| `DEBUG` | `True` local, `False` production |
| `ALLOWED_HOSTS` | Comma-separated host list |
| `DATABASE_URL` | PostgreSQL URL; omitted falls back to SQLite |
| `ADMIN_USERNAME` | Used by `ensure_superuser` |
| `ADMIN_EMAIL` | Used by `ensure_superuser` |
| `ADMIN_PASSWORD` | Used by `ensure_superuser` |
| `FEEDBACK_SERVICE_URL` | Optional Flask service URL |
| `FEEDBACK_SERVICE_CONNECT_TIMEOUT` | Flask connect timeout |
| `FEEDBACK_SERVICE_READ_TIMEOUT` | Flask read timeout |
| `FEEDBACK_SERVICE_FAILURE_COOLDOWN` | Circuit-breaker cooldown |
| `EMAIL_HOST` | If set, enables SMTP backend |
| `EMAIL_HOST_USER` | SMTP username |
| `EMAIL_HOST_PASSWORD` | SMTP password / Gmail app password |
| `DEFAULT_FROM_EMAIL` | Outgoing email sender |

Email backend auto-detects:

- `EMAIL_HOST` set -> SMTP backend
- `EMAIL_HOST` empty -> console backend

## Deployment Notes

The repository includes `render.yaml`:

- `feedback-insight-hub`: Django web service
- `feedback-domain-service`: Flask private service

Important practical note: Render private services may require a paid plan depending on current Render product rules. If Flask is not deployed, keep `FEEDBACK_SERVICE_URL` unset and run Django-only fallback.

`build.sh` runs:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py ensure_superuser
python manage.py seed_demo
python manage.py collectstatic --noinput
```

Production requirements:

- Set `DATABASE_URL` in Render environment variables.
- Set `DEBUG=False`.
- Set `ALLOWED_HOSTS`.
- Set admin env vars for `ensure_superuser`.
- Use Python 3.13.2 or another Django 6-compatible Python version.

## Database and Migration Safety

The Django ORM is the source of truth. SQLAlchemy models in `services/feedback_service/models.py` must mirror the same database schema when Flask writes or reads shared tables.

Fields that must not be removed without a planned production migration:

| Model | Field |
|---|---|
| `Survey` | `category` |
| `Answer` | `analysis_text` |
| `Answer` | `sentiment_score` |
| `Answer` | `analysis_version` |
| `ImprovementDispatch` | `is_read` |

Fields intentionally removed and must not be added back:

| Model | Field |
|---|---|
| `Survey` | `access_mode` |
| `FeedbackSubmission` | `source` |

Before opening a PR that touches models or migrations:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
```

## Collaboration Rules

Before pushing or opening a PR:

```bash
git fetch origin
git merge origin/main
python manage.py check
python manage.py makemigrations --check --dry-run
python -m py_compile feedback/models.py feedback/views.py feedback/local_service.py
```

Rules:

- Do not commit `.env`.
- Do not commit `supabase密碼.txt` or any credential file.
- Do not remove production text-analysis fields.
- Do not reintroduce quick / hybrid access mode.
- Do not fake migrations in `build.sh` to bypass schema conflicts.
- Keep `CLAUDE.md` / `.claude/` updates separate from feature changes unless explicitly requested.

## Useful Commands

```bash
# System checks
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan

# Admin and demo data
python manage.py ensure_superuser
python manage.py seed_demo
python manage.py seed_notification_test

# Text analysis
python manage.py rebuild_text_analysis --dry-run
python manage.py rebuild_text_analysis
python manage.py sync_keyword_categories --dry-run
python manage.py sync_keyword_categories

# Static files
python manage.py collectstatic --noinput
```

## Current Caveats

- Public homepage now uses a dedicated `public_base.html` shell and `public-*` CSS classes. Login, signup, and password pages intentionally keep the existing public/auth layout for now.
- The homepage no longer lists active surveys publicly. This is intentional for the B2B login-only positioning; survey entry points live behind the customer portal.
- Flask stats endpoint is behind the Django fallback stats contract.
- `render.yaml` includes a Flask private service blueprint, but practical deployment may remain Django-only on Render free tier.
- Some legacy documentation and source comments may still contain mojibake from earlier Windows terminal encoding issues; user-facing templates should be checked visually before demo.
