# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Feedback Insight Hub** — a bilingual (Traditional Chinese / English) feedback and survey management platform. Django handles presentation, authentication, and ORM; a Flask microservice handles the feedback domain with analytics. The two services share the same PostgreSQL database (Supabase in production).

## Current Collaboration Baseline (2026-05-09)

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
- Password reset / password change flows added via Django built-in auth views (`accounts/urls.py`). Templates live in `templates/accounts/`.
- Notification AJAX mark-as-read added: `MarkNoticeReadView` at `/app/notifications/<pk>/read/`. `ImprovementDispatch.is_read` field added in migration `feedback/0007_add_is_read_to_improvementdispatch.py`.
- Unread notification count injected via `feedback/context_processors.py` → `unread_notification_count`; registered in `TEMPLATES.context_processors`.
- Email backend auto-detects SMTP vs console: if `EMAIL_HOST` env var is set, Django uses SMTP; otherwise falls back to console (safe for local dev without `.env` config).
- Text analysis selected-survey view should show KPI summary, word cloud, keyword cards, category sentiment distribution, text question list, and keyword-category rules.
- A regression was fixed where duplicate `text_analysis_summary()` / `category_sentiment_summary()` definitions in `feedback/models.py` overrode sentiment logic and caused category sentiment to appear as empty. Keep only one active definition for each helper.

### 2026-05-09 UI, analytics, and email baseline

- Public homepage, login/signup, password reset/change, and customer-facing pages were restyled toward a quieter Claude Design-inspired visual language.
- Manager pages intentionally keep the existing manager dashboard shell; do not let public/customer CSS changes pollute manager workspace pages.
- Customer portal nav is simplified to Home / Customer Portal / Notifications / Profile / Logout. Notifications and Profile should have visible active/background states.
- Notification history, preferences, profile, and password flows now use the newer customer/public styling.
- Gmail SMTP password reset works when `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and `DEFAULT_FROM_EMAIL` are configured. `EMAIL_HOST_PASSWORD` must be a Google App Password.
- Stats descriptive charts now include distribution bars for continuous/discrete numeric questions when `chart.counts` exists, matching the categorical bar display.
- `.claude/audit-report.md` and `docs/audit-2026-05-09.md` record the May 9 UI/email/stats handoff. Keep `.claude/settings.local.json` out of commits.

### Codex Windows Encoding Notes

Claude Code usually reads these Markdown files without extra handling. Codex on Windows may show mojibake when PowerShell uses its default text decoding for UTF-8 files that contain Traditional Chinese or symbols such as `—`, `→`, and `⚠️`.

Current working location is intentionally simplified to `C:\Projects\Project`. Keep this path for Codex work instead of moving the project back to Desktop or another path with Chinese / synced-folder segments.

When Codex reads Markdown or other text files in PowerShell, use explicit UTF-8 decoding:

```powershell
Get-Content README.md -Encoding utf8
Get-Content CLAUDE.md -Encoding utf8
Get-Content docs\audit-2026-05-09.md -Encoding utf8
Get-Content .claude\audit-report.md -Encoding utf8
```

To verify whether a file is genuinely corrupted or only displayed with the wrong terminal decoding, use the repository diagnostic script:

```powershell
python scripts\diagnose_text_encoding.py --preview README.md
python scripts\diagnose_text_encoding.py --preview CLAUDE.md
python scripts\diagnose_text_encoding.py --preview docs\audit-2026-05-09.md
python scripts\diagnose_text_encoding.py --preview .claude\audit-report.md
```

If the diagnostic script outputs correct Unicode escapes / readable content, do not rewrite the file to "fix" mojibake; read it again with `-Encoding utf8`.

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

Text analysis UI (`templates/feedback/text_analysis.html`) now matches the survey-manager / stats index pattern:
- category pills and sort dropdown: `newest` (default), `oldest`, `title`
- list of surveys with text-analysis availability status
- selecting a survey via `?survey=<slug>` opens the keyword analysis panel
- selected survey view includes KPI summary, word cloud, keyword cards, category sentiment distribution, text question list, and keyword-category rules
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
| `EMAIL_HOST` | — | If set, auto-switches `EMAIL_BACKEND` to SMTP. Leave unset for console (local dev). |
| `EMAIL_PORT` | `587` | SMTP port |
| `EMAIL_USE_TLS` | `True` | |
| `EMAIL_USE_SSL` | `False` | Mutually exclusive with TLS |
| `EMAIL_HOST_USER` | — | SMTP username / Gmail address |
| `EMAIL_HOST_PASSWORD` | — | Gmail App Password (16-digit; requires 2FA enabled) |
| `DEFAULT_FROM_EMAIL` | `noreply@feedback-platform.local` | Sender address in outgoing mail |

## Data Models

**Django ORM** (source of truth): `SurveyCategory`, `Survey`, `Question`, `FeedbackSubmission`, `Answer`, `KeywordCategory`, `ImprovementUpdate`, `ImprovementDispatch` in `feedback/models.py`. `Survey.access_mode` and `FeedbackSubmission.source` no longer exist. `ImprovementDispatch.is_read` (bool, default False) tracks customer read status. `User` (extends `AbstractUser`) with `role` and `notification_opt_in` in `accounts/models.py`.

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
| `feedback/0007_add_survey_category` | Add SurveyCategory model; add Survey.category FK |
| `feedback/0007_answer_analysis_text_answer_analysis_version_and_more` | Add cached text-analysis fields to Answer (`analysis_text`, `analysis_version`, `sentiment_score`) |
| `feedback/0007_add_is_read_to_improvementdispatch` | Add `is_read` to ImprovementDispatch |
| `feedback/0008` | Remove obsolete Survey.access_mode and FeedbackSubmission.source columns |
| `feedback/0009_merge_20260428_2019` | Merge migration joining category/schema-removal branch with text-analysis field branch |
| `feedback/0010_merge_20260505_2155` | Merge migration joining is_read branch with 0009 (2026-05-05 integration) |

## Deployment

Deployed on **Render** (see `render.yaml`):
- `feedback-insight-hub` (type: web) — Django, built via `build.sh`
- `feedback-domain-service` (type: pserv) — Flask private service

`build.sh` runs: `pip install`, `migrate`, `ensure_superuser`, `seed_demo`, `collectstatic`.

Static files served by Whitenoise. `DATABASE_URL` must be set manually in Render dashboard for both services (points to Supabase PostgreSQL).

## Git Collaboration Rules

**Before opening a PR, always run:**

```bash
git fetch origin && git merge origin/main   # sync to latest main first
python manage.py check                       # must show 0 issues
python manage.py migrate --check            # must show no pending migrations
python -m py_compile feedback/models.py feedback/views.py feedback/local_service.py
```

**Hard rules to prevent schema regression:**

1. **Never edit `feedback/models.py` without creating a matching migration.** Run `python manage.py makemigrations` after every model change and commit the migration file.
2. **Never remove `SurveyCategory`, `Survey.category`, `Answer.analysis_text/sentiment_score/analysis_version`, or `ImprovementDispatch.is_read`** — these fields exist in the Supabase production database. See the ⚠️ table in the Collaboration Baseline section.
3. **Never add back `Survey.access_mode` or `FeedbackSubmission.source`** — they were intentionally dropped in migration `0008`.
4. **Feature branches must be rebased / merged from latest `main` before PR**, not from an older snapshot. Working from an outdated base causes fields that `main` already has to appear as "new" in your diff, and fields that `main` removed to silently return.
5. **Do not commit `CLAUDE.md`, `.claude/settings.local.json`, or `scripts/` in feature PRs** unless the PR is explicitly about updating those files.

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

### Notification System

`feedback/context_processors.py` provides `unread_notification_count` — queries unread `ImprovementDispatch` rows for the logged-in customer (not managers) and injects it into every template context. Registered in `TEMPLATES.context_processors` in `config/settings.py`.

**Navbar unread badge:** `base.html` customer nav link shows `.nav-badge` when `unread_notification_count > 0`. Styled in `static/css/app.css` as a red circle positioned top-right of the link.

**AJAX mark-as-read flow** (`customer_notifications.html`):
1. Each notification row has `data-pk`, `data-is-read`, `data-survey-url` attributes.
2. On click, JS checks if unread, then POSTs to `/app/notifications/<pk>/read/` with `X-Requested-With: XMLHttpRequest` and CSRF cookie.
3. `MarkNoticeReadView` returns `{"ok": true}` for AJAX, or redirects for non-AJAX.
4. Frontend removes `record-row-unread` class, decrements badge, updates pill to "已讀", then navigates to `data-survey-url`.

**Gmail App Password:** Google Account → Security → 2-Step Verification → App Passwords. Set `EMAIL_HOST=smtp.gmail.com` and `EMAIL_HOST_PASSWORD=<16-char-app-password>` in `.env`.

## Merge Incident Log - 2026-05-07

Context: `origin/main` contained teammate updates for text-analysis/improvement integration, but also introduced a dangerous migration chain:

- `feedback/migrations/0010_remove_answer_analysis_text_and_more.py`
- `feedback/migrations/0011_improvementdispatch_is_read.py`
- `build.sh` workaround: `python manage.py migrate feedback 0011 --fake`

Why this is dangerous:

- `0010_remove_answer_analysis_text_and_more.py` removes `Answer.analysis_text`, `Answer.analysis_version`, and `Answer.sentiment_score`.
- These columns already exist in Supabase production and are part of the text-analysis cache/sentiment pipeline.
- Running that migration on production would execute `DROP COLUMN` for those fields and break the newer text-analysis flow.
- Faking `0011` in `build.sh` is also unsafe because it can mark migrations as applied without actually creating required database columns.

Resolution used:

- Merged `origin/main` into local `main` without committing immediately.
- Kept the local production-safe schema:
  - `SurveyCategory`
  - `Survey.category`
  - `Answer.analysis_text`
  - `Answer.analysis_version`
  - `Answer.sentiment_score`
  - `ImprovementDispatch.is_read`
- Removed the dangerous/duplicate remote migration files from the merge result:
  - `0010_remove_answer_analysis_text_and_more.py`
  - `0011_improvementdispatch_is_read.py`
- Removed the `build.sh` fake migration workaround.
- Kept the safe remote additions:
  - `https://feedback-insight-hub-pa75.onrender.com` in `CSRF_TRUSTED_ORIGINS`
  - `text_analysis_summary()` and `category_sentiment_summary()` helper functions in `feedback/models.py`

Validation before commit:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python -m py_compile feedback/models.py feedback/views.py feedback/local_service.py accounts/views.py
```

Expected result: no pending model migrations and no planned migration operations against Supabase.
