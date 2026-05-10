# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

> **Sync note:** This file mirrors `CLAUDE.md` (for Claude) with tool-specific naming. When updating one, update the other. For the full change history see `docs/CHANGELOG.md`.

## Project Overview

**Feedback Insight Hub** — a bilingual (Traditional Chinese / English) feedback and survey management platform. Django handles presentation, authentication, and ORM; a Flask microservice handles the feedback domain with analytics. The two services share the same PostgreSQL database (Supabase in production).

## Current Collaboration Baseline (2026-05-10)

- The product is now fully login-only. Old quick/hybrid access modes have been removed from UI, admin, runtime payloads, and schema.
- `Survey.access_mode` and `FeedbackSubmission.source` were removed in migration `feedback/0008_remove_feedbacksubmission_source_and_more.py`.
- Supabase production database has already applied migration `feedback.0008`; `feedback_survey.access_mode` and `feedback_feedbacksubmission.source` are confirmed removed.
- Current practical deployment is Django-only fallback. Keep `FEEDBACK_SERVICE_URL` unset unless the Flask service is explicitly deployed and kept schema-compatible.
- Pandas/SciPy stats are implemented in Django fallback (`feedback/local_service.py`) and shown in `stats_overview.html` via `inferential_analysis`.
- Flask `/api/stats` still returns the legacy stats payload and does not yet include Pandas `inferential_analysis`; enabling Flask for stats would skip the new inference panel for now.
- Google login on signup is an intentional disabled placeholder owned by another teammate. Do not remove it as stale UI.
- Manager analysis-related pages now use a unified survey-index first flow: pick a survey from list cards, then drill into stats / text analysis / improvements / notices.
- Customer portal has been split into account profile (`/accounts/profile/`) and notification preferences (`/accounts/preferences/`). The customer home page focuses on account summary, submission records, and notification summaries.
- Uncommitted local collaboration files may exist (`AGENTS.md`, `CLAUDE.md`, `scripts/`). Do not mix them into unrelated feature commits unless requested.
- Password reset / password change flows added via Django built-in auth views (`accounts/urls.py`). Templates live in `templates/accounts/`.
- Notification AJAX mark-as-read added: `MarkNoticeReadView` at `/app/notifications/<pk>/read/`. `ImprovementDispatch.is_read` field added in migration `feedback/0007_add_is_read_to_improvementdispatch.py`.
- Unread notification count injected via `feedback/context_processors.py` → `unread_notification_count`; registered in `TEMPLATES.context_processors`.
- Email backend auto-detects SMTP vs console: if `EMAIL_HOST` env var is set, Django uses SMTP; otherwise falls back to console (safe for local dev without `.env` config).
- Text analysis selected-survey view should show KPI summary, word cloud, keyword cards, category sentiment distribution, text question list, and keyword-category rules.
- A regression was fixed where duplicate `text_analysis_summary()` / `category_sentiment_summary()` definitions in `feedback/models.py` overrode sentiment logic and caused category sentiment to appear as empty. Keep only one active definition for each helper.

### 2026-05-10 UI and UX baseline

- Survey fill page (`survey_detail.html`) is now a step-by-step one-question-per-page form with a progress bar.
- Step 0 shows read-only respondent info (name + email auto-filled from `request.user`) and the `consent_follow_up` checkbox. Respondent name/email are no longer editable fields.
- The improvement-tracking KPI card was removed from the survey fill page (irrelevant from the customer's perspective).
- The form uses `novalidate` to prevent HTML5 browser validation from blocking submission on hidden steps. Django server-side validation still runs.
- Each step card has a `data-has-error` attribute set by the template; JS always stays in step mode and navigates to the first error step on validation failure.
- Survey manager list cards now show stat chips (題目 / 回覆 / 最近回覆, font-size 18px) and a 3-day response trend mini bar chart inside the clickable area.
- Stats / text-analysis / improvement / notice center pages now use the same `.survey-row-body` card layout as the survey manager, with page-specific first chip and the same green background on the clickable area.
- Builder scale question preview no longer truncates at 7 options; CSS uses `flex-wrap: wrap`.
- Builder header meta (response count, stats/text-analysis links) is now displayed at a larger, more prominent size using `.builder-meta-link` green outline button style.
- `seed_demo.py` scale question now includes `options_text` for 1–10 to avoid the fallback 1–5 IntegerField.

### 2026-05-09 UI, analytics, and email baseline

- Public homepage, login/signup, password reset/change, and customer-facing pages were restyled toward a quieter visual language.
- Manager pages intentionally keep the existing manager dashboard shell; do not let public/customer CSS changes pollute manager workspace pages.
- Customer portal nav is simplified to Home / Customer Portal / Notifications / Profile / Logout. Notifications and Profile should have visible active/background states.
- Notification history, preferences, profile, and password flows now use the newer customer/public styling.
- Gmail SMTP password reset works when `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and `DEFAULT_FROM_EMAIL` are configured. `EMAIL_HOST_PASSWORD` must be a Google App Password.
- Stats descriptive charts now include distribution bars for continuous/discrete numeric questions when `chart.counts` exists, matching the categorical bar display.

### Codex Windows Encoding Notes

Current working location is intentionally simplified to `C:\Projects\Project`. Keep this path for Codex work instead of moving the project back to Desktop or another path with Chinese / synced-folder segments.

When Codex reads Markdown or other text files in PowerShell, use explicit UTF-8 decoding:

```powershell
Get-Content README.md -Encoding utf8
Get-Content AGENTS.md -Encoding utf8
```

To verify whether a file is genuinely corrupted or only displayed with the wrong terminal decoding:

```powershell
python scripts\diagnose_text_encoding.py --preview AGENTS.md
```

### ⚠️ Schema fields that must NOT be reverted

The following fields exist in the database (Supabase production) and are used by production code. **Never remove them from `feedback/models.py` or create a migration that drops them without a coordinated schema migration plan:**

| Model | Field | Added in |
|---|---|---|
| `Survey` | `category` (FK → SurveyCategory) | `feedback/0007_add_survey_category.py` |
| `Answer` | `analysis_text` | `feedback/0007_answer_analysis_text_answer_analysis_version_and_more.py` |
| `Answer` | `sentiment_score` | same |
| `Answer` | `analysis_version` | same |
| `ImprovementDispatch` | `is_read` | `feedback/0007_add_is_read_to_improvementdispatch.py` |

The following fields were **intentionally removed** and must NOT be added back:

| Model | Field | Removed in |
|---|---|---|
| `Survey` | `access_mode` | `feedback/0008_remove_feedbacksubmission_source_and_more.py` |
| `FeedbackSubmission` | `source` | same |

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
python scripts/diagnose_text_encoding.py --preview AGENTS.md

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
| `/accounts/password-reset/` | PasswordResetView | `accounts:password_reset` |
| `/accounts/password-reset/done/` | PasswordResetDoneView | `accounts:password_reset_done` |
| `/accounts/reset/<uidb64>/<token>/` | PasswordResetConfirmView | `accounts:password_reset_confirm` |
| `/accounts/reset/done/` | PasswordResetCompleteView | `accounts:password_reset_complete` |
| `/accounts/password-change/` | PasswordChangeView | `accounts:password_change` |
| `/accounts/password-change/done/` | PasswordChangeDoneView | `accounts:password_change_done` |

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

### Survey Fill Form (Step-by-Step)

`survey_detail.html` is a stepped one-question-per-page form:
- Step 0: read-only respondent info (name + email from `request.user`) + `consent_follow_up` checkbox only.
- Steps 1–N: one question per step.
- Navigation: "開始填答 →" on step 0, "下一題 →" on middle steps, "送出回饋" on last step.
- `<form novalidate>` prevents browser HTML5 validation from blocking on hidden steps.
- `data-has-error` attribute on each step card; JS navigates to first error step on server-side validation failure.
- `respondent_name` and `respondent_email` are read from `request.user` in the view POST handler, not from form fields.

### Text Analysis

Text analysis is dictionary-driven and cached on `Answer` rows.

Core files:
- `feedback/text_pipeline.py` — tokenization, synonym normalization, sentiment scoring, and `ANALYSIS_VERSION`.
- `feedback/data/` — stopwords, synonyms, keyword/category map, sentiment dictionaries, negation words, and intensifiers.
- `feedback/local_service.py` — Django fallback payload with keywords, summary, and category sentiment distribution.
- `services/feedback_service/app.py` — Flask payload mirrors the same text-analysis contract.

`Answer` cached fields:
- `analysis_text`: normalized text used for keyword analysis.
- `sentiment_score`: approximate sentiment score from dictionary rules.
- `analysis_version`: text pipeline version used when the row was computed.

Management commands:
- `python manage.py rebuild_text_analysis --dry-run` — preview historical text-answer rebuild.
- `python manage.py rebuild_text_analysis` — backfill `analysis_text`, `sentiment_score`, and `analysis_version`.
- `python manage.py sync_keyword_categories --dry-run` / `python manage.py sync_keyword_categories` — sync `feedback/data/keyword_category_map.json` into `KeywordCategory`.
- `python manage.py top_uncategorized_keywords --survey <slug>` — inspect high-frequency uncategorized terms.

Payload keys from `service_client.get_text_analysis(slug)`:
- `keywords`: list of keyword rows with `keyword`, `count`, and `category`.
- `summary`: includes answer coverage and average sentiment score.
- `category_sentiments`: per-category positive / neutral / negative counts.

Rule source-of-truth policy for text analysis:
- Runtime classification uses `KeywordCategory` rows from DB.
- `feedback/data/keyword_category_map.json` is a versioned seed file, not auto-loaded at runtime.
- Team workflow should be: edit JSON -> run `sync_keyword_categories --dry-run` -> run `sync_keyword_categories` on target environment.
- Avoid using manual DB edits as the primary update path, otherwise JSON and DB will drift.

`TextAnalysisView` must pass all three payload sections into template context (`keywords`, `analysis_summary`, `category_sentiments`). If only `keywords` is passed, the word cloud/sentiment panel will partially render or appear empty.

`keyword_summary()` in `feedback/models.py` pre-loads all `KeywordCategory` rules (1 query) then matches with fuzzy substring containment, avoiding N+1 queries.

### Statistical Analysis

`feedback/local_service.py` contains the current Pandas/SciPy statistical engine used by the Django fallback stats path.

`get_survey_pandas_stats(survey)` returns:
- `charts`: template-compatible chart records (`type="numeric"` or `type="category"`).
- `inferential_analysis`: automatic statistical test records.

Data type rules:
- `continuous`: numeric quantity with meaningful magnitude. Gets numeric summaries; eligible DV for t-test / ANOVA / Pearson.
- `discrete`: count-like numeric. Gets numeric summaries only; not auto-used as DV.
- `nominal`: unordered category. Gets frequency chart; single-choice can be IV.
- `ordinal`: ordered category without guaranteed equal spacing. Gets frequency chart; used in rank tests.
- `text`: handled by text analysis pipeline only.

Inference rules:
- `nominal IV x continuous DV`: Welch t-test (2 groups) or one-way ANOVA (3–5 groups).
- `nominal x nominal`: chi-square (single-choice only). Effect size: Cramer's V.
- `nominal IV x ordinal DV`: Mann-Whitney U (2 groups) or Kruskal-Wallis (3–5 groups).
- `continuous x continuous`: Pearson correlation.
- Ordinal-rank pairs: Spearman correlation.

Important: this engine is wired through Django fallback only. Flask `/api/stats` has not been upgraded to this Pandas contract.

### Notice Center

`/dashboard/notices/` follows the survey-index first pattern. It lists surveys with category filter and sort controls; selecting a survey via `?survey=<slug>` opens the notice list.

### Improvement List Page

`/dashboard/improvements/` uses the survey-index first pattern. Selecting a survey opens its improvement tracking workspace with a per-survey tracking toggle.

POST actions:
- `toggle-tracking` — enable / disable `Survey.improvement_tracking_enabled`
- inline create improvement — only available when tracking is enabled

### Customer Portal

`/app/` is the customer-facing dashboard:
- Account summary and latest status.
- Submission record cards with status filters: `all`, `pending`, `tracking`, `improved`.
- Each submission row shows survey title, category pill, status pill, and metadata (`<answer_count> 題已作答，提交時間：YYYY/M/D`).
- Answer snippets are intentionally not shown to avoid leaking context.
- Notification summary links to `/app/notifications/`.

`/accounts/preferences/` — global notification opt-in + per-survey follow-up switches.
`/accounts/profile/` — user profile data (name, email, organization). Keep profile fields out of preferences.

### Manager Workspace Layout

The manager sidebar (`dashboard_base.html`) is fixed: `position: sticky; height: 100vh` on `.manager-sidebar`, with `.manager-shell` set to `height: 100vh; overflow: hidden` and `.manager-main` set to `overflow-y: auto; height: 100vh`.

### Signup Form (`/accounts/signup/`)

`CustomerSignUpForm` fields: `username`, `first_name`, `email`, `notification_opt_in`, `password1`, `password2`. The signup page includes a disabled Google login placeholder button (coming soon) — do not remove it.

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | — | Required in production |
| `DEBUG` | `True` | Set `False` in production |
| `ALLOWED_HOSTS` | — | Comma-separated |
| `DATABASE_URL` | SQLite | PostgreSQL URL for production (Supabase) |
| `FEEDBACK_SERVICE_URL` | — | Omit to use Django fallback only (recommended) |
| `FEEDBACK_SERVICE_CONNECT_TIMEOUT` | `0.35` | Seconds |
| `FEEDBACK_SERVICE_READ_TIMEOUT` | `0.8` | Seconds |
| `FEEDBACK_SERVICE_FAILURE_COOLDOWN` | `30` | Seconds before retrying Flask |
| `ADMIN_USERNAME` | — | Used by `ensure_superuser` |
| `ADMIN_EMAIL` | — | Used by `ensure_superuser` |
| `ADMIN_PASSWORD` | — | Used by `ensure_superuser` |
| `EMAIL_HOST` | — | If set, auto-switches to SMTP backend |
| `EMAIL_PORT` | `587` | SMTP port |
| `EMAIL_USE_TLS` | `True` | |
| `EMAIL_USE_SSL` | `False` | Mutually exclusive with TLS |
| `EMAIL_HOST_USER` | — | SMTP username / Gmail address |
| `EMAIL_HOST_PASSWORD` | — | Gmail App Password (16-digit; requires 2FA) |
| `DEFAULT_FROM_EMAIL` | `noreply@feedback-platform.local` | Sender address |

## Data Models

**Django ORM** (source of truth): `SurveyCategory`, `Survey`, `Question`, `FeedbackSubmission`, `Answer`, `KeywordCategory`, `ImprovementUpdate`, `ImprovementDispatch` in `feedback/models.py`. `ImprovementDispatch.is_read` (bool, default False) tracks customer read status. `User` (extends `AbstractUser`) with `role` and `notification_opt_in` in `accounts/models.py`.

**SQLAlchemy models** in `services/feedback_service/models.py` mirror the Django schema. When adding fields, update both ORMs and create a Django migration.

## Migrations

| Migration | Description |
|---|---|
| `feedback/0001` – `0004` | Initial schema |
| `feedback/0005` | Remove QUICK/HYBRID choices |
| `feedback/0006` | Data migration: convert hybrid/quick to login |
| `feedback/0007_add_survey_category` | Add SurveyCategory; add Survey.category FK |
| `feedback/0007_answer_analysis_text_...` | Add Answer cached text-analysis fields |
| `feedback/0007_add_is_read_to_improvementdispatch` | Add `is_read` to ImprovementDispatch |
| `feedback/0008` | Remove Survey.access_mode and FeedbackSubmission.source |
| `feedback/0009_merge_20260428_2019` | Merge migration |
| `feedback/0010_merge_20260505_2155` | Merge migration |

## Deployment

Deployed on **Render** (see `render.yaml`):
- `feedback-insight-hub` (type: web) — Django, built via `build.sh`
- `feedback-domain-service` (type: pserv) — Flask private service

`build.sh` runs: `pip install`, `migrate`, `ensure_superuser`, `seed_demo`, `collectstatic`.

## Git Collaboration Rules

**Before opening a PR, always run:**

```bash
git fetch origin && git merge origin/main
python manage.py check
python manage.py migrate --check
python -m py_compile feedback/models.py feedback/views.py feedback/local_service.py
```

**Hard rules:**

1. Never edit `feedback/models.py` without creating a matching migration.
2. Never remove `SurveyCategory`, `Survey.category`, `Answer.analysis_text/sentiment_score/analysis_version`, or `ImprovementDispatch.is_read`.
3. Never add back `Survey.access_mode` or `FeedbackSubmission.source`.
4. Feature branches must be rebased / merged from latest `main` before PR.
5. Do not commit `AGENTS.md`, `CLAUDE.md`, or `scripts/` in feature PRs unless explicitly requested.

## Dependencies

```
Django==6.0.3
dj-database-url==3.0.1
Flask==3.1.2
gunicorn==23.0.0
psycopg[binary]==3.3.3
python-dotenv==1.0.1
pandas==2.3.3
requests==2.32.5
scipy==1.16.3
SQLAlchemy==2.0.43
whitenoise==6.9.0
```

No frontend JS/CSS framework. All UI is custom HTML + `static/css/app.css`.

### Notification System

`feedback/context_processors.py` provides `unread_notification_count` injected into every template context. Registered in `TEMPLATES.context_processors` in `config/settings.py`.

**AJAX mark-as-read flow:**
1. Each notification row has `data-pk`, `data-is-read`, `data-survey-url` attributes.
2. On click, JS POSTs to `/app/notifications/<pk>/read/` with `X-Requested-With: XMLHttpRequest` and CSRF cookie.
3. `MarkNoticeReadView` returns `{"ok": true}` for AJAX, or redirects for non-AJAX.
4. Frontend removes `record-row-unread` class, decrements badge, updates pill to "已讀", navigates to `data-survey-url`.

## Merge Incident Log - 2026-05-07

See `docs/CHANGELOG.md` for the full incident record. Summary: a dangerous migration chain (`0010_remove_answer_analysis_text_and_more`) from `origin/main` was identified and removed before production deployment. The production-safe schema was preserved.
