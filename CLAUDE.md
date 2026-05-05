# CLAUDE.md

## 開發規則（重要）

- 每次修改程式碼前，必須先列出修改計畫，等待我確認後才能動手
- 不可以直接修改檔案，一定要先問我

## 我負責的範圍

- 通知中心（Notice Center）：/dashboard/notices/
- 部分顧客中心（Customer Center）：/app/notifications/

## 注意事項

- 不要修改 seed_demo.py
- 測試用假資料請使用 seed_notification_test

---

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Feedback Insight Hub** — a bilingual (Traditional Chinese / English) feedback and survey management platform. Django handles presentation, authentication, and ORM; a Flask microservice handles the feedback domain with analytics. The two services share the same PostgreSQL database (Supabase in production).

## Current Collaboration Baseline (2026-04-28)

- The product is now fully login-only. Old quick/hybrid access modes have been removed from UI, admin, runtime payloads, and schema.
- `Survey.access_mode` and `FeedbackSubmission.source` were removed in migration `feedback/0008_remove_feedbacksubmission_source_and_more.py`.
- Supabase production database has already applied migration `feedback.0008`; `feedback_survey.access_mode` and `feedback_feedbacksubmission.source` are confirmed removed.
- Current practical deployment is Django-only fallback. Keep `FEEDBACK_SERVICE_URL` unset unless the Flask service is explicitly deployed and kept schema-compatible.
- Pandas/SciPy stats are implemented in Django fallback (`feedback/local_service.py`) and shown in `stats_overview.html` via `inferential_analysis`.
- Flask `/api/stats` still returns the legacy stats payload and does not yet include Pandas `inferential_analysis`; enabling Flask for stats would skip the new inference panel for now.
- Google login on signup is an intentional disabled placeholder owned by another teammate. Do not remove it as stale UI.
- Manager analysis-related pages now use a unified survey-index first flow: pick a survey from list cards, then drill into stats / text analysis / improvements / notices.
- Customer portal has been split into account profile (`/accounts/profile/`) and notification preferences (`/accounts/preferences/`). The customer home page focuses on account summary, submission records, and notification summaries.
- Uncommitted local collaboration files may exist (`CLAUDE.md`, `.claude/settings.local.json`, `scripts/`). Do not mix them into unrelated feature commits unless requested.

## Commands

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Initialize database and seed demo data
python manage.py migrate
python manage.py ensure_superuser
python manage.py seed_demo

# Diagnose text encoding / mojibake safely in Windows terminals
python scripts/diagnose_text_encoding.py
python scripts/diagnose_text_encoding.py --preview CLAUDE.md

# Start Flask microservice (port 5001)
python -m flask --app services.feedback_service.app run --host 127.0.0.1 --port 5001

# Start Django dev server (port 8000)
python manage.py runserver
```

`.env` is auto-loaded via `python-dotenv` at `config/settings.py` startup. Copy `.env.example` to `.env` before first run.

### Production (Render)

```bash
# Django
gunicorn config.wsgi:application

# Flask
gunicorn services.feedback_service.app:app --bind 0.0.0.0:10000
```

### Custom Management Commands

```bash
python manage.py ensure_superuser        # Create admin from env vars (ADMIN_USERNAME/EMAIL/PASSWORD)
python manage.py seed_demo               # Seed example survey + keyword categories
python manage.py seed_notification_test  # Seed 4 test users, survey, submissions, improvement dispatch + email
```

## Architecture

### Two-Layer Service Design

```
Django (port 8000)  →  service_client.py  →  Flask microservice (port 5001)
                                        ↘  local_service.py (fallback)
                                              ↓
                                    Shared PostgreSQL (Supabase) / SQLite DB
```

**`feedback/service_client.py`** implements a circuit-breaker pattern: it tries the Flask microservice first, and on failure automatically falls back to `feedback/local_service.py` (which queries the DB via Django ORM). A `disabled_until` timestamp prevents retry storms (default 30s cooldown). If `FEEDBACK_SERVICE_URL` is not set at all, the local provider is used exclusively.

### Key Directories

| Path | Purpose |
|---|---|
| `config/` | Django settings, URLs, WSGI/ASGI |
| `accounts/` | Django app: users, roles, preferences |
| `feedback/` | Django app: surveys, views, service client, local service |
| `services/feedback_service/` | Flask microservice: API routes, SQLAlchemy models, analytics |
| `templates/` | Django HTML templates (all UI) |
| `static/css/app.css` | Single hand-written CSS file, no external framework |

### Flask API Endpoints (`services/feedback_service/app.py`)

- `GET /health` — health check
- `GET /api/home` — homepage stats
- `GET /api/customers/<user_id>/home` — customer dashboard
- `GET /api/customers/<user_id>/notifications` — customer notifications
- `GET /api/dashboard` — manager dashboard metrics
- `GET /api/stats?survey=<slug>` — survey charts and statistical analysis
- `GET /api/text-analysis?survey=<slug>` — keyword frequency analysis
- `POST /api/surveys/<slug>/submissions` — submit survey responses

### Django URL Structure (`feedback/urls.py`)

| URL | View | Name |
|---|---|---|
| `/` | HomeView | `feedback:home` |
| `/app/` | CustomerHomeView | `feedback:customer-home` |
| `/app/notifications/` | CustomerNotificationsView | `feedback:customer-notifications` |
| `/app/notifications/<pk>/read/` | MarkNoticeReadView | `feedback:notice-mark-read` |
| `/dashboard/` | DashboardView | `feedback:dashboard` |
| `/dashboard/forms/` | SurveyManagerView | `feedback:survey-manager` |
| `/dashboard/forms/new/` | SurveyCreateView | `feedback:survey-create` |
| `/dashboard/forms/<slug>/builder/` | SurveyBuilderView | `feedback:survey-builder` |
| `/dashboard/stats/` | StatsOverviewView | `feedback:stats-overview` |
| `/dashboard/text-analysis/` | TextAnalysisView | `feedback:text-analysis` |
| `/dashboard/improvements/` | ImprovementListView | `feedback:improvement-list` |
| `/dashboard/notices/` | NoticeCenterView | `feedback:notice-center` |
| `/dashboard/notices/<pk>/` | NoticeDetailView | `feedback:notice-detail` |
| `/survey/<slug>/` | SurveyDetailView | `feedback:survey-detail` |
| `/survey/<slug>/success/` | SurveySubmitSuccessView | `feedback:survey-success` |
| `/survey/<slug>/improvement/new/` | ImprovementCreateView | `feedback:improvement-create` |
| `/accounts/login/` | — | `accounts:login` |
| `/accounts/logout/` | — | `accounts:logout` |
| `/accounts/signup/` | — | `accounts:signup` |
| `/accounts/preferences/` | — | `accounts:preferences` |
| `/accounts/profile/` | — | `accounts:profile` |

### Roles

Two user roles (`accounts/models.py`): `CUSTOMER` and `MANAGER`. Role-based access is enforced via Django mixins in views (`ManagerRequiredMixin`, `CustomerRequiredMixin`, `DashboardBaseMixin`).

### Survey Access

All surveys require login. `Survey.AccessMode`, `Survey.access_mode`, and `FeedbackSubmission.source` have been removed from the active schema. There is no anonymous or quick-access mode.

`SurveyDetailView.dispatch` enforces the following checks in order, rendering in-place at the survey URL for all non-auth cases:

1. **Unauthenticated** → redirect to `/accounts/login/?next=<path>` (the only redirect case).
2. **`survey.is_active == False`** → render survey page with `survey_notice` message; form is hidden.
3. **No questions** → render survey page with `survey_notice` message; form is hidden.
4. **Customer already submitted** → render survey page with `survey_notice` message; form is hidden. Managers are exempt from this check.

The user flow is: scan QR code → redirect to login if not authenticated → fill survey after login. Inactive surveys are not listed on the home page (`is_active=True` filter in both `local_service.py` and Flask `app.py`).

### Survey Create Flow

`SurveyCreateView` handles `/dashboard/forms/new/`. On valid POST:
1. `slug` is auto-generated from `slugify(title)`. If collision exists, appends `-2`, `-3`, etc.
2. `improvement_tracking_enabled` is always forced to `True` (not user-editable).

`SurveyCreateForm` fields: `title`, `category`, `description`, `thank_you_email_enabled`, `is_active`.

After creation, redirects to `feedback:survey-builder` for the new survey's slug.

### Survey Category

`SurveyCategory` model (`feedback/models.py`) — optional classification for surveys.

- `name`: unique CharField
- `Survey.category`: nullable FK → `SurveyCategory` (`SET_NULL`)

`SurveyManagerView` supports:
- `?sort=newest` (default) / `?sort=oldest` / `?sort=title`
- `?category=<id>` — filter by category

Admin: `SurveyCategoryAdmin` registered; `improvement_tracking_enabled` is `readonly` in `SurveyAdmin`.

### Survey Builder

`SurveyBuilderView` (`/dashboard/forms/<slug>/builder/`) has two functional tabs:

| Tab | key | Content |
|---|---|---|
| 題目設定 | `questions` | Question list (with inline edit) + add-question form |
| 問卷設定 | `settings` | `SurveyEditForm`: title, category, description, is_active (toggle), thank_you_email_enabled (checkbox), slug (read-only + copy button) |

Response count and latest response time are shown inline in the builder page header, alongside links to stats and text-analysis. Tab state is preserved via `?tab=<key>` query param on redirect after POST.

POST actions (`action` hidden input):
- `delete-question` — delete a question by `question_id`
- `edit-question` — update a question via `QuestionCreateForm(instance=question)`
- `update-survey` — update survey metadata via `SurveyEditForm(instance=survey)`
- (default, no action) — add a new question

`SurveyEditForm` fields: `title`, `category`, `description`, `is_active`, `thank_you_email_enabled`.

### Text Analysis

Tokenizes submission text using regex supporting both Chinese characters and English words (2+ chars). Filters a hardcoded stop-word list. Returns top 20 keywords by frequency with `category` field (looked up from `KeywordCategory`). Found in `services/feedback_service/analysis.py` and mirrored in `feedback/local_service.py`.

Text analysis UI (`templates/feedback/text_analysis.html`) now matches the survey-manager / stats index pattern:
- category pills and sort dropdown: `newest` (default), `oldest`, `title`
- list of surveys with text-analysis availability status
- selecting a survey via `?survey=<slug>` opens the keyword analysis panel
- old right-side selector / execute button flow has been removed

### Statistical Analysis

`feedback/local_service.py` contains the current Pandas/SciPy statistical engine used by the Django fallback stats path.

The project uses an **analysis-purpose data type model**, not a pure Stevens four-scale model and not a pure data-science-only categorical/numeric split. The goal is to let the builder capture the minimum information needed for safe automated Pandas analysis.

`get_survey_pandas_stats(survey)` returns:
- `charts`: template-compatible chart records (`type="numeric"` or `type="category"`).
- `inferential_analysis`: automatic statistical test records. Each record can include `analysis_family`, `method_key`, `test_name`, `statistic`, `p_value`, `effect_size`, `effect_label`, `is_significant`, `insight`, `warning`, or `skipped_reason`.

Rules:
- `continuous`: numeric quantity with meaningful magnitude, such as score, money, time, or ratio. It gets numeric summaries and can be a dependent variable (DV) in t-test / ANOVA.
- `discrete`: count-like or code-like numeric value, such as visit count, item count, or numeric level code. It gets numeric summaries only and is not automatically used as a DV.
- `nominal`: unordered category, such as department, region, role, or issue type. It gets category distribution; single-choice nominal questions can be independent variables (IV).
- `multiple_choice + nominal`: split/explode frequency chart only, not IV, because one submission may belong to multiple groups.
- `ordinal`: ordered category where spacing is not guaranteed, such as very satisfied / satisfied / neutral / dissatisfied. It gets category distribution only and is intentionally excluded from t-test / ANOVA.
- `text`: handled by text analysis, not the stats inference engine.

Inference rules:
- `nominal IV x continuous DV`: 2 valid groups use Welch independent-samples t-test; 3 to 5 valid groups use one-way ANOVA. Each group needs at least 2 numeric values. Effect size is Cohen's d for t-test and eta squared for ANOVA.
- `nominal x nominal`: uses chi-square test of independence for single-choice nominal questions. Multiple-choice nominal questions are excluded because one response can belong to multiple groups. Effect size is Cramer's V.
- `nominal IV x ordinal DV`: uses Mann-Whitney U for 2 groups and Kruskal-Wallis for 3 to 5 groups. Ordinal questions need `options_text` so the engine can safely map labels to ranks.
- `continuous x continuous`: uses Pearson correlation.
- Any pair involving ordinal ranks in correlation uses Spearman correlation.
- Numeric charts include count, mean, median, std, min, max, and 95% confidence interval for the mean when there are at least 2 values.
- Invalid combinations return `skipped_reason`.

Important: this engine is currently wired through Django fallback (`feedback/local_service.py`). Flask `/api/stats` has not yet been upgraded to this Pandas contract.

Stats overview UI (`templates/feedback/stats_overview.html`) is structured as an analysis workflow:
- default entry page is a survey index, aligned with survey manager: category pills, sort dropdown, and survey cards with "查看統計"
- survey selector and KPI strip
- flow strip: select survey -> read data types -> recommend methods -> validate conditions and explain
- data map cards for each question
- method router cards explaining descriptive stats, mean comparison, categorical association, rank tests, and correlation
- descriptive statistics cards
- inferential analysis grouped by `analysis_family`, with executed results and skipped-condition cards

Builder UI rules:
- `short_text` / `long_text` -> fixed `text`.
- `single_choice` -> user chooses `ordinal` or `nominal`.
- `multiple_choice` -> fixed `nominal`.
- `scale` -> user chooses `continuous` or `ordinal`.
- `integer` / `decimal` -> user chooses `continuous` or `discrete`; current UI defaults to `continuous`.

Survey builder UI current state:
- Question cards now include a lightweight answer preview below the title row.
- `scale` preview renders radio-style points, using `question.options` when present and defaulting to 1-5 when empty.
- `single_choice` / `multiple_choice` previews render radio/checkbox option rows, capped to the first 5 options.
- Text and numeric questions render disabled-looking input/textarea previews.
- The builder add-question form shows the next question number and a lightweight usage hint when the question kind changes.
- Manager dashboard pages no longer render the global Django messages banner from `dashboard_base.html`; the frontend `base.html` messages block remains available for non-dashboard pages.

`SurveyFormBuilder` widget rules (actual survey fill form):
- `single_choice` → `RadioSelect` (previously `<select>`).
- `multiple_choice` → `CheckboxSelectMultiple` (unchanged).
- `scale` with `options_text` → `RadioSelect` using those options.
- `scale` without `options_text` → `IntegerField(min_value=1, max_value=5)`.
- `short_text` / `long_text` / `integer` / `decimal` → unchanged.

### Notice Center

`/dashboard/notices/` now follows the same survey-index first pattern as stats and text analysis. It lists surveys with category filter and sort controls; selecting a survey via `?survey=<slug>` opens the notice list for that survey. The old right-side survey selector flow has been removed.

`NoticeCenterView.get_context_data` provides survey list context (`survey_rows`, `categories`, `current_category`, `current_sort`) and selected survey notice detail context (`selected_survey`, `selected_notices`).

### Improvement List Page

`/dashboard/improvements/` also uses the survey-index first pattern. Selecting a survey opens its improvement tracking workspace. The page supports an improvement-tracking toggle per survey; if tracking is disabled, inline creation is blocked and the UI explains why.

POST actions:
- `toggle-tracking` — enable / disable `Survey.improvement_tracking_enabled`
- inline create improvement — only available when tracking is enabled

The older accordion-only behavior is no longer the primary page structure.

### Customer Portal

`/app/` is the customer-facing dashboard. It shows:
- account summary and latest status
- submission record cards with status filters: `all`, `pending`, `tracking`, `improved`
- each submission row shows survey title, category pill, status pill, and concise metadata: `<answer_count> 題已作答，提交時間：YYYY/M/D`
- answer snippets are intentionally not shown in the submission list to avoid leaking context such as organization / department answers into the overview
- notification summary links to `/app/notifications/`

Submission payloads from both Django fallback and Flask include:
- `submitted_at` — ISO timestamp for machine use
- `submitted_date` — display date, formatted `YYYY/M/D`
- `submitted_datetime` — display datetime, formatted `YYYY/M/D HH:MM`

`/accounts/preferences/` is now notification-specific:
- global notification opt-in switch
- per-filled-survey follow-up switches based on `FeedbackSubmission.consent_follow_up`
- category pills and sort controls

`/accounts/profile/` owns user profile data such as name, email, and organization. Keep profile fields out of notification preferences.

### Manager Workspace Layout

The manager sidebar (`dashboard_base.html`) is fixed: `position: sticky; height: 100vh` on `.manager-sidebar`, with `.manager-shell` set to `height: 100vh; overflow: hidden` and `.manager-main` set to `overflow-y: auto; height: 100vh`. This makes the sidebar stay in place while only the right content area scrolls.

### Signup Form (`/accounts/signup/`)

`CustomerSignUpForm` fields: `username`, `first_name`, `email`, `notification_opt_in`, `password1`, `password2`. `last_name` and `organization` have been removed. The signup page includes a disabled Google login placeholder button (coming soon) above the email form, separated by a divider.

## Environment Variables

Copy `.env.example` to `.env`. Key variables:

| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | — | Required in production |
| `DEBUG` | `True` | Set `False` in production |
| `ALLOWED_HOSTS` | — | Comma-separated |
| `DATABASE_URL` | SQLite | PostgreSQL URL for production (Supabase) |
| `FEEDBACK_SERVICE_URL` | — | Flask URL; omit to use Django fallback only. Recommended unset unless Flask is deployed and stats parity is updated. |
| `FEEDBACK_SERVICE_CONNECT_TIMEOUT` | `0.35` | Seconds |
| `FEEDBACK_SERVICE_READ_TIMEOUT` | `0.8` | Seconds |
| `FEEDBACK_SERVICE_FAILURE_COOLDOWN` | `30` | Seconds before retrying Flask |
| `ADMIN_USERNAME` | — | Used by `ensure_superuser` command |
| `ADMIN_EMAIL` | — | Used by `ensure_superuser` command |
| `ADMIN_PASSWORD` | — | Used by `ensure_superuser` command |

## Data Models

**Django ORM** (source of truth): `SurveyCategory`, `Survey`, `Question`, `FeedbackSubmission`, `Answer`, `KeywordCategory`, `ImprovementUpdate`, `ImprovementDispatch` in `feedback/models.py`. `Survey.access_mode` and `FeedbackSubmission.source` no longer exist. `User` (extends `AbstractUser`) with `role` and `notification_opt_in` in `accounts/models.py`.

**SQLAlchemy models** in `services/feedback_service/models.py` mirror the Django schema — they read/write the same tables. When adding fields, update both ORMs and create a Django migration.

### ImprovementListView Context

`ImprovementListView.get_context_data` provides `survey_groups`, a list of dicts:

```python
{
    "survey": Survey,           # Survey instance
    "improvements": [...],      # list of ImprovementUpdate for this survey
    "create_url": str,          # reverse("feedback:improvement-create", args=[survey.slug])
}
```

Fetched in 2 queries (no N+1): one for all improvements, one for all surveys; grouped in Python.

## Migrations

| Migration | Description |
|---|---|
| `feedback/0001` – `0004` | Initial schema |
| `feedback/0005` | Remove QUICK/HYBRID choices from Survey.access_mode and FeedbackSubmission.source |
| `feedback/0006` | Data migration: convert existing hybrid/quick records to login |
| `feedback/0007` | Add SurveyCategory model; add Survey.category FK |
| `feedback/0008` | Remove obsolete Survey.access_mode and FeedbackSubmission.source columns |

## Deployment

Deployed on **Render** (see `render.yaml`):
- `feedback-insight-hub` (type: web) — Django, built via `build.sh`
- `feedback-domain-service` (type: pserv) — Flask private service

`build.sh` runs: `pip install`, `migrate`, `ensure_superuser`, `seed_demo`, `collectstatic`.

Static files served by Whitenoise. `DATABASE_URL` must be set manually in Render dashboard for both services (points to Supabase PostgreSQL).

## Dependencies

```
Django==6.0.3
dj-database-url==3.0.1
Flask==3.1.2
gunicorn==23.0.0
psycopg[binary]==3.3.3      # psycopg3, not psycopg2
python-dotenv==1.0.1
pandas==2.3.3
requests==2.32.5
scipy==1.16.3
SQLAlchemy==2.0.43
whitenoise==6.9.0
```

No frontend JS/CSS framework. All UI is custom HTML + `static/css/app.css`.

## 通知系統（Notification System）

### Context Processor

`feedback/context_processors.py` 提供 `unread_notification_count`，對所有已登入的顧客（非 manager）查詢未讀 `ImprovementDispatch` 數量，注入所有 template context，讓 `base.html` 導覽列可直接使用 `{{ unread_notification_count }}`。已在 `config/settings.py` 的 `TEMPLATES.context_processors` 中註冊。

### Email 設定

透過 `.env` 環境變數控制，預設為 console backend（開發用）。正式寄信需設定以下變數：

| 變數 | 值 |
|---|---|
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` |
| `EMAIL_HOST` | `smtp.gmail.com` |
| `EMAIL_PORT` | `587` |
| `EMAIL_USE_TLS` | `True` |
| `EMAIL_HOST_USER` | 你的 Gmail 帳號 |
| `EMAIL_HOST_PASSWORD` | Gmail App Password（16 碼） |
| `DEFAULT_FROM_EMAIL` | 你的 Gmail 帳號 |

Gmail App Password 產生：Google 帳號 → 安全性 → 兩步驟驗證 → 應用程式密碼。

### 導覽列未讀 Badge

`base.html` 顧客端導覽列的「通知」連結帶有 `.nav-badge`，顯示 `{{ unread_notification_count }}`（大於 0 才顯示）。樣式定義在 `static/css/app.css`：紅色圓形、`position: absolute` 定位於連結右上角，帶 `box-shadow` 增加視覺層次。

### AJAX 標記已讀流程

`/app/notifications/` 頁面（`customer_notifications.html`）：

1. 每條通知 row 帶有 `data-pk`、`data-is-read`、`data-survey-url` 屬性
2. 點擊整條通知時，JS 判斷若未讀，向 `/app/notifications/<pk>/read/` 發送 AJAX POST
3. 請求帶 `X-Requested-With: XMLHttpRequest` header，CSRF token 從 cookie 取得
4. `MarkNoticeReadView` 偵測到 AJAX 請求後回傳 `{"ok": True}`（非 AJAX 則 redirect）
5. 前端收到回應後即時更新：移除 `record-row-unread` class、badge 數字 -1、pill 改為「已讀」
6. UI 更新完成後導向 `data-survey-url`（對應問卷詳情頁）
