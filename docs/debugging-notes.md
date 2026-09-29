# 除錯筆記

只收錄仍會影響除錯判斷的事實；一般變更歷史請看 Git log。

## Migration 歷史中的異常

- `feedback/migrations` 有三個 `0007_*` 與兩個 merge migration（`0009`、`0010`），來自早期多人並行開發，屬正常合併結果。
- 2026-05 曾有一條外部分支的 migration 會 `DROP COLUMN` 移除 `Answer.analysis_text`、`analysis_version`、
  `sentiment_score`，並在 `build.sh` 用 `migrate --fake` 繞過。該 migration 已從程式庫刪除、未套用。
  若在某個資料庫的 `django_migrations` 看到 `0010_remove_answer_analysis_text_and_more` 或
  `0011_improvementdispatch_is_read`，代表該資料庫曾被錯誤操作，需先比對實際欄位再處理，不可直接 `migrate`。

## 帳號與郵件

- 新註冊的顧客必須驗證 Email 才能登入（`User.is_email_verified`）；管理者與 superuser 不受限。
  導入驗證前已存在的帳號由 `verify_existing_users` 指令標記為已驗證。
- 密碼重設信透過 SendGrid HTTP API 寄送（`accounts/emails.py`），不經 SMTP；
  重設連結以 context 中的 `protocol`／`domain` 組成，不依賴 `request`。函式名稱 `send_password_reset_email_via_resend`
  是歷史命名，實際呼叫的仍是 SendGrid。

## 本機開發陷阱

- `staticfiles/`（`collectstatic` 產物）存在時，WhiteNoise 會優先送出其中的舊 CSS／JS，蓋過 `static/` 原始碼。
  本機改樣式看不到效果時，先刪除 `staticfiles/`（已被 Git 忽略）。
- 修改 CSS／JS 後需更新樣板中的 `?v=` 版本參數，否則瀏覽器會繼續使用快取。
- Windows PowerShell 5.1 以系統字碼頁讀取無 BOM 的腳本；含中文的 `.ps1` 須存成 UTF-8 with BOM
  （`scripts/build_desktop.ps1` 已處理）。
- `django.setup()` 會自動載入專案根目錄 `.env`；測試一律使用 `--settings=config.settings_test`，
  它會固定測試模型並使用假金鑰。
