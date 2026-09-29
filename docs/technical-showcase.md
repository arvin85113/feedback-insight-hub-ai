# Feedback Insight Hub — 技術展示

本文件說明主要設計決策並連到實作位置，不複製程式碼（程式碼以原始檔為準）。

## 1. 技術選型

| 層 | 技術 | 選擇理由 |
|---|---|---|
| Web | Django 6（唯一後端與 ORM） | 權限、表單、ORM 與 migration 一體，網站與本機工作台共用同一套 models 與 domain service |
| 資料庫 | Supabase PostgreSQL；本機與測試用 SQLite | 雲端託管 PostgreSQL；測試用記憶體 SQLite，併發測試另用 PostgreSQL 17 |
| 統計 | pandas＋SciPy | 依資料型態自動選擇檢定 |
| 文字 | jieba＋自建詞典（中文）、英文詞典 | 可解釋、可版本化；未命中的情緒標示為未知而非中立 |
| 大型資料 | DuckDB＋本機 Parquet | 20 萬筆評論不進雲端資料庫，只發布有限結果 |
| AI | Gemini（Vertex express mode） | 結構化輸出＋後端驗證，只由本機工作台呼叫 |
| 桌面 | Dear PyGui＋PyInstaller one-folder EXE | 不依賴 Tcl/Tk，可在管理者電腦執行重運算 |
| 部署 | Render（Gunicorn＋WhiteNoise）、GitHub Actions CI | 合併到 `main` 前必須通過兩個 Python 版本的全套測試 |

## 2. 架構：網站收資料，本機算，版本化發布

- 網站負責登入、問卷、填答、權限與工作排程；統計、文字與 Gemini 都在本機工作台／Worker 執行
  （[analysis_worker](../feedback/analysis_worker.py)、[ai_worker](../feedback/ai_worker.py)）。
- 回覆、題目或詞典變更會提升問卷的輸入／設定版本並合併待處理工作
  （[analysis_jobs](../feedback/analysis_jobs.py)、[signals](../feedback/signals.py)）。
- 工作以租約、心跳、有限重試與取消管理；發布前在短交易內重查租約與版本，過期 Worker 不能覆蓋新結果。
- 頁面只讀已發布的有限展示副本（[published_analysis](../feedback/published_analysis.py)），不在 request 內運算。

## 3. 資料可追溯

- 外部匯入以批次紀錄來源版本、檔案 SHA-256、mapping 版本與抽樣種子；每筆回覆有 namespace＋穩定 record key，
  同鍵不同內容列為衝突而不覆寫（[importing](../feedback/importing/)）。
- 大型資料以 `ExternalDatasetVersion` 登錄不可變來源（revision、清理版本、內容雜湊、列數），
  資料列留在本機 Parquet（[analysis_sources](../feedback/analysis_sources.py)）。
- 統計與 Parquet 分析共用同一個輸入契約（[analysis_input](../feedback/analysis_input.py)、
  [analysis_adapters](../feedback/analysis_adapters.py)）。

## 4. 統計分析

- 題目資料型態採分析用途分類：`continuous`、`discrete`、`nominal`、`ordinal`、`text`。
- 依型態組合自動選擇檢定：Welch t-test、單因子 ANOVA、Mann-Whitney U、Kruskal-Wallis、卡方、Pearson、Spearman；
  不符條件時回報略過原因，而非靜默省略；每項結果報告有效 N、缺失與排除原因
  （[local_service](../feedback/local_service.py)）。

## 5. 文字分析

- 斷詞、同義詞正規化、停用詞與情緒詞典評分（[text_pipeline](../feedback/text_pipeline.py)、[詞典](../feedback/data/)）。
- 關鍵字分類規則存於資料庫 `KeywordCategory`，由管理者在文字洞察頁維護。
- 分別報告覆蓋率與情緒命中率；覆蓋率不等於準確率。

## 6. Gemini 分析

- 三個階段：統計解讀、文字洞察、綜合決策與改善草稿（[ai_stage_service](../feedback/ai_stage_service.py)）。
- 每階段使用 JSON schema 結構化輸出；後端再驗證引用的 evidence ID、長度與數量上限。
- **數字必須有依據**：AI 文字中的數字只能是所引用 evidence 的數值、樣本數或標籤中的數字，
  自行推算的比例或目標值會被拒絕（[ai_grounding](../feedback/ai_grounding.py)）。
- Prompt、schema 與上限的內容雜湊構成 stage 版本；修改 prompt 不會誤用舊結果。
- 輸出截斷、格式錯誤或限流時以精簡模式重試一次；逾時等結果不確定的錯誤不盲目重試。
- 沒有文字 evidence 時跳過文字階段呼叫；mock 結果不得正式發布。

## 7. 權限與登入

- 問卷為 login-only；管理者與顧客以 mixin 分權（[views](../feedback/views.py)）。
- 共用登入入口：登入後依角色導向；入口與角色不符時導向自己的工作區（[accounts/views](../accounts/views.py)）。

## 8. 前端

- 設計變數（色彩、圓角、陰影、字體）定義於 [app.css](../static/css/app.css) 的 `:root`；
  元件層（按鈕、表單、卡片、分頁、管理端外框）在 [ui.css](../static/css/ui.css)。
- 頁面腳本位於 [static/js](../static/js/)；統計與文字頁共用 `analysis-tabs.js`。

## 9. 可靠性

- `/healthz/`（不碰資料庫）與 `/healthz/db/`；資料庫無法連線時回 503 並自動重新整理（[health](../config/health.py)）。
- 排程每三天呼叫 `/healthz/db/`，避免 Supabase 免費方案因閒置暫停（[keepalive](../.github/workflows/keepalive.yml)）。
- 部署環境缺少 `DJANGO_SECRET_KEY` 時拒絕啟動，Render 上預設 `DEBUG=False`（[settings](../config/settings.py)）。
- 測試設定固定測試模型並使用假金鑰，不讀開發者 `.env`（[settings_test](../config/settings_test.py)）。

## 10. 桌面工作台

- 第一段發布統計與文字，第二段（需勾選同意使用 API 額度）執行 Gemini（[desktop_app/service](../desktop_app/service.py)）。
- Gemini「最新」判定同時核對資料版本、模型名稱與 prompt 版本。
- 錯誤寫入本機輪替日誌，不含憑證（[desktop_app/__main__](../desktop_app/__main__.py)）。
