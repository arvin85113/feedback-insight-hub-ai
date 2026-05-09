# 程式碼庫巡檢：建議任務清單（2026-05-08）

以下任務是根據目前程式碼庫狀態整理，分別對應：拼字修正、錯誤修正、註解/文件差異修正、測試改進。

## 1) 拼字錯誤修正任務

**任務：修正文字分析使用指南中的指令名稱拼字/命名錯誤。**

- 問題：`feedback/data/USAGE_GUIDE.md` 曾提到不存在的 `seed_beverage_demo` command，但程式庫中實際存在的飲料店示範資料指令是 `seed_demo_beverage`。這屬於文件中的命名錯誤（可視為拼字/指令字串錯誤）。
- 影響：新成員照文件執行會直接失敗，降低 onboarding 效率。
- 建議修正：
  1. 將 `seed_beverage_demo` 改為 `seed_demo_beverage`。
  2. 補上該指令用途簡述（例如：建立飲料店示範資料與問卷）。

## 2) 錯誤修正任務（Bug Fix）

**任務：修正 README 的本機啟動指令在 bash 環境不可用的問題。**

- 問題：`README.md` 的 Local Setup 使用 `copy .env.example .env` 與 `.venv\Scripts\activate`，這是 Windows cmd/PowerShell 風格；目前專案環境為 bash（Linux）。
- 影響：在 macOS/Linux/WSL 會直接失敗，屬於可重現操作錯誤。
- 建議修正：
  1. 改為跨平台指引（分成 Windows 與 Unix-like 兩段）。
  2. Unix-like 範例改用：`cp .env.example .env`、`source .venv/bin/activate`。

## 3) 程式碼註解或文件差異修正任務

**任務：補齊「文件宣稱有測試」與「實際測試內容空白」之間的差異說明。**

- 問題：README 描述功能完整（登入、統計、文字分析、改善追蹤），但 `accounts/tests.py` 與 `feedback/tests.py` 仍是 Django 預設空樣板。
- 影響：文件可讀性高但缺少可驗證性，會讓讀者誤判品質成熟度。
- 建議修正：
  1. 在 README 新增「目前自動化測試覆蓋範圍」段落，誠實標示現況。
  2. 若短期內不補測試，至少新增 roadmap 與優先測試清單。

## 4) 測試改進任務

**任務：建立最小可行測試集（MVP Test Suite），先覆蓋四條核心流程。**

- 建議先新增：
  1. `accounts`：註冊成功、登入後導向頁面。
  2. `feedback`：登入後可建立問卷（權限與必填欄位）。
  3. `feedback`：填答送出後會建立 `FeedbackSubmission` 與 `Answer`。
  4. `service_client`：`FEEDBACK_SERVICE_URL` 不可用時能 fallback 到 `local_service`。
- 驗收標準：
  - `python manage.py test` 可穩定通過。
  - 至少有一個測試驗證 fallback 邏輯（mock requests 失敗情境）。
