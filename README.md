# Feedback Insight Hub

以 Django 作為門面層，負責：

- 使用者驗證與角色分流
- 模板頁面與後台入口
- Django Admin 與帳號管理

以 Flask 作為 feedback domain 微服務，負責：

- 首頁統計摘要
- 顧客端回饋紀錄與通知列表
- 管理端儀表板彙整
- 問卷提交寫入
- 統計分析與文字分析 API

## 架構

- `accounts/`
  - Django 帳號、登入、偏好設定
- `feedback/`
  - Django façade views
  - 問卷建置與改善公告管理
  - Flask service client
- `services/feedback_service/`
  - Flask app
  - SQLAlchemy data access
  - feedback domain API

## 本機啟動

1. 安裝依賴

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

2. 初始化 Django

```bash
python manage.py migrate
python manage.py ensure_superuser
python manage.py seed_demo
```

3. 可選：啟動 Flask 微服務

```bash
python -m flask --app services.feedback_service.app run --host 127.0.0.1 --port 5001
```

4. 啟動 Django façade

```bash
python manage.py runserver
```

目前所有問卷都採登入後填答，沒有匿名或快速填答模式。
若未設定 `FEEDBACK_SERVICE_URL`，Django 會直接使用本地 provider，適合目前的 Django-only 開發或部署方式。
若設定 `FEEDBACK_SERVICE_URL`，Django 會優先呼叫 Flask 微服務；Flask 暫時不可用時，Django 仍會在短 timeout 後自動退回本地 provider，並在冷卻期間避免重複等待。

## 共用測試基準資料

- 問卷 mock 資料放在 `feedback/fixtures/beverage_survey_mock_100.csv`。
- 此檔案用於團隊共用測試、整合驗證與展示，不是正式營運資料。
- 請勿放入真實個資；若更新內容，請在 PR 說明欄位、筆數或分布變更。

## 重要環境變數

```text
DJANGO_SECRET_KEY=replace-me
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost
DEFAULT_FROM_EMAIL=noreply@example.com
EMAIL_BACKEND=
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-email@example.com
EMAIL_HOST_PASSWORD=your-app-password
EMAIL_USE_TLS=True
EMAIL_USE_SSL=False
FEEDBACK_SERVICE_URL=http://127.0.0.1:5001
FEEDBACK_SERVICE_CONNECT_TIMEOUT=0.35
FEEDBACK_SERVICE_READ_TIMEOUT=0.8
FEEDBACK_SERVICE_FAILURE_COOLDOWN=30
```

`EMAIL_HOST` 有填值時，系統預設走 SMTP 寄信；未填時會使用 console backend（開發環境）。

## Render Blueprint

`render.yaml` 已拆成兩個 service：

- `feedback-insight-hub`: Django 對外 Web Service
- `feedback-domain-service`: Flask 私有服務

Django 透過內網 URL 呼叫 Flask 服務；兩者共用同一份 `DATABASE_URL`。

## Schema Migration 協作流程

此專案的 Django 與 Flask 共用同一個資料庫，請遵循以下流程避免協作環境故障：

1. 只用 Django migration 變更 schema，避免在 Supabase Dashboard 手動改欄位。
2. 新欄位先採 `null=True`（或安全預設值），先確保舊資料可相容。
3. 變更後同步更新 `services/feedback_service/models.py` 的 SQLAlchemy 欄位定義。
4. 部署順序建議為：先部署可相容新舊 schema 的程式碼，再執行 `python manage.py migrate`。

本次已新增 `feedback_answer.analysis_text`、`feedback_answer.sentiment_score`、`feedback_answer.analysis_version`，對應 migration：`feedback/migrations/0007_answer_analysis_text_answer_analysis_version_and_more.py`。

歷史資料回填指令：

```bash
python manage.py rebuild_text_analysis --dry-run
python manage.py rebuild_text_analysis
python manage.py rebuild_text_analysis --survey <survey-slug>
```

若只部署 Django，請不要設定 `FEEDBACK_SERVICE_URL`，系統會直接走 Django fallback。
