# Feedback Insight Hub Audit Report

Last updated: 2026-05-09

## Current Stable Baseline

Feedback Insight Hub is currently a login-only Django-first feedback platform. Django handles presentation, authentication, ORM, survey submission, manager workspaces, customer portal, statistics, text analysis integration, and notification flows. Flask remains present as a legacy/domain service, but the practical deployment path is Django fallback unless `FEEDBACK_SERVICE_URL` is explicitly configured and verified.

## 2026-05-09 Update Summary

- Public homepage and auth pages were moved toward a quieter Claude Design-inspired visual system.
- Customer portal, notification history, notification preferences, profile, password reset, and password change pages were aligned to the same customer-facing style.
- Manager pages keep the existing manager dashboard shell to avoid cross-contaminating the public/customer redesign.
- Gmail SMTP password reset was configured and tested locally with a Google App Password.
- Stats overview now supports bar-style distribution display for numeric continuous/discrete questions, matching the categorical bar display.
- No schema changes were introduced in this batch.

## UI Status

| Area | Status |
|---|---|
| Public homepage | Uses public landing layout; no public Active Surveys list because the product is B2B login-only. |
| Login/signup | Restyled; still uses existing Django auth routes. |
| Password reset/change | Restyled and functional. Password reset requires SMTP env vars for real delivery. |
| Customer portal | Uses simplified nav and card layout; notifications and latest status are separated clearly. |
| Notification history | Uses aligned heading scale, KPI cards, and scrollable latest notification list. |
| Notification preferences | Global notification toggle plus per-submitted-survey switches. |
| Profile | Includes account information and profile fields; password-change CTA is primary. |
| Manager survey index pages | Category pills, sort control, and shared list-card layout remain active. |
| QR Code expansion | Uses an expansion pattern that avoids blocking other controls. |

## Analytics Status

| Feature | Status |
|---|---|
| Descriptive statistics | Available in Django fallback. |
| Categorical bar charts | Available. |
| Continuous/discrete numeric distribution bars | Available when `chart.counts` exists. |
| Inferential analysis | Available in Django fallback through Pandas/SciPy output. |
| Flask stats parity | Not complete; Flask stats remains legacy and should not be relied on for the inference panel. |

## Email Status

Gmail SMTP delivery works only when all required environment variables are configured:

```env
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=<gmail-address>
EMAIL_HOST_PASSWORD=<google-app-password>
DEFAULT_FROM_EMAIL=<sender>
```

Use a Google App Password. A normal Google account password will fail with `SMTPAuthenticationError 535`.

## Schema Guardrails

Do not remove these fields without a coordinated migration plan:

| Model | Field | Purpose |
|---|---|---|
| `Survey` | `category` | Survey categorization and index filters. |
| `Answer` | `analysis_text` | Cached/normalized text analysis source. |
| `Answer` | `sentiment_score` | Sentiment support for text analysis. |
| `Answer` | `analysis_version` | Text analysis cache versioning. |
| `ImprovementDispatch` | `is_read` | Customer notification read state. |

These fields were intentionally removed and must not be reintroduced:

| Model | Field |
|---|---|
| `Survey` | `access_mode` |
| `FeedbackSubmission` | `source` |

## Do Not Stage

- `.claude/settings.local.json`
- `.env`
- local credential files
- Supabase password notes

## Verification Checklist

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
```

Manual pages to smoke test:

- `/`
- `/accounts/login/`
- `/accounts/signup/`
- `/accounts/password-reset/`
- `/accounts/password-change/`
- `/app/`
- `/app/notifications/`
- `/accounts/preferences/`
- `/accounts/profile/`
- `/dashboard/`
- `/dashboard/stats/`
