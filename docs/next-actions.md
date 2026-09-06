# Feedback Insight Hub 下一階段行動計畫

> 狀態：執行中；各階段仍以本文件的「實作進度」及測試證據判定。migration 與程式部署一般仍須分別查證；本次實際狀態記於下方。
> 基準日期：2026-09-05。執行時仍以實際程式、Git 差異及驗證證據為準。

## 實作進度（2026-09-06）

- 本次未提交基線位於 `main`，目前 HEAD 為 `c8448b3`；工作區含既有大量修改與未追蹤產物，後續提交前必須先依功能邊界分組審查，不可把所有差異一次視為單一乾淨提交。
- 第一階段已建立 Git 忽略的 `.venv`：Python 3.12.14、Django 6.0.8、DuckDB 1.5.5、Pandas 3.0.5、SciPy 1.18.1，沿用 `requirements.txt`，未另行升級。
- 隔離 SQLite 的 `feedback`／`accounts` 受影響範圍共 204 項通過，涵蓋背景分析、匯入、Worker、發布只讀頁、回覆冪等與封存語意；測試 DB 均建立後銷毀，未呼叫付費 API。另以唯讀交易驗證 Supabase 遷移後的核心列數與唯一性。
- 第二階段第一批已實作：`SurveyAnalysisState` 保存輸入／設定／管線版本與各階段發布指標；`AnalysisJob` 保存來源版本、階段、租約、心跳、有限重試、取消與安全錯誤碼。
- Django 回覆建立、Answer／題目／詞典變更可更新版本並合併待處理工作；既有批次匯入改為每批流程只排一個等價工作。發布時會在短交易內重查租約與版本，拒絕過期 Worker，且 mock 階段不得正式發布。
- 第三階段第一批已實作：`run_analysis_worker_once` 僅領取 deterministic 工作，可從 Answer 或已登錄固定版本 Parquet 產生無原始評論的版本化本機產物；快取與輸入完整性通過後建立／重用 Snapshot，再以短交易發布統計／文字指標。`run_analysis_worker` 可供程序管理員持續輪詢，仍未安裝成 Windows／雲端服務。
- 第四階段第一批已實作：`run_ai_worker_once` 只領取 AI synthesis 工作，未帶 `--allow-paid-ai` 時不領取；沿用既有三階段 schema／遮蔽證據與 Snapshot，provider timeout 等不確定狀態不盲目重呼。僅以 mock provider 測試，未呼叫真實 Gemini。
- 第五階段第一批已實作：發布交易把有限的統計／文字及 AI 展示副本保存於 `SurveyAnalysisState`，排除大型 evidence catalog；統計、文字及 AI 狀態端點在 `ANALYSIS_READ_PUBLISHED_ONLY=True` 時只讀這些副本，不觸發 request-time 分析。同步 POST 在此模式只排背景工作；頁面顯示最新／等待新版及 AI 版本差異。本機預設關閉，Render blueprint 已啟用並部署。
- `ANALYSIS_AUTO_AI_ENABLED=True` 時，AI 工作只會在 deterministic 發布交易成功後排入；AI Worker 會在 provider 呼叫前再次核對統計與文字版本，防止以舊 Snapshot 冒充新版分析。
- 2026-09-06 已校正桌面方向：正式資料統一保存於 Supabase 的問卷模型；桌面工作台列出各問卷的資料量、最新資料／分析時間及新舊狀態，可手動或依本機設定更新統計／文字結果。Parquet 驗證與 mock 預覽降為匯入前工具，不再是正式產品主畫面。
- 2026-09-06 桌面流程已拆成兩段：第一段發布統計／文字 Snapshot，第二段在管理員勾選 API 額度選項後執行 Gemini 並發布新 Stage；列表分別顯示兩段時間與新舊狀態。隔離測試驗證未啟用時不觸及 AI worker，啟用後保留原 Snapshot 並新增 synthesis Stage。
- Django 已成為唯一後端與 ORM；舊第二服務、呼叫端、設定與依賴已移除。網站提交直接使用 Django domain service，並以回覆冪等鍵防止重送。
- 新增明確隔離的 `config.settings_test`；2026-09-06 的 `feedback`／`accounts` 受影響範圍共 204 項測試通過，未呼叫真實 Gemini。
- Windows 封裝已改用不依賴 Tcl/Tk 的 Dear PyGui；校正後 one-folder EXE 位於 `dist/FeedbackInsightHub/FeedbackInsightHub.exe`，smoke-test exit code 為 0，未包含 `.env`。新版 Supabase 畫面的實際正式連線、跨機器驗收、安裝包及程式碼簽章仍未完成。
- `0015`～`0018` 已套用至設定的 Supabase：新增匯入來源、分析工作／發布狀態與 v2 生命週期欄位，既有 3／8／150／710 筆核心列數保持一致。程序服務安裝、Render 部署與真實 Gemini 驗收仍未完成。
- 已新增 `config.settings_postgres_test` 與 PostgreSQL 專用雙 Worker 測試，強制使用獨立 `TEST_DATABASE_URL`、隔離確認旗標及測試用途資料庫名稱，避免誤接 `DATABASE_URL`。2026-09-06 使用臨時 PostgreSQL 17.11 隔離叢集執行 2 項測試，雙 Worker 原子領取、租約接手及舊租約發布拒絕均通過；測試資料庫、叢集與下載檔已停止並刪除，未連 Supabase。SQLite 跳過仍不視為通過。
- Render 上線前檢查已移除 build 階段的 `ensure_superuser` 與 `fix_empty_slugs`，避免每次部署重設帳密或執行一次性資料修復；依賴、靜態檔與 migration 保留，並補齊反向代理 HTTPS、安全 Cookie 與動態 Render hostname 設定。提交 `43c9839` 已於 2026-09-06 23:20:40（GMT+8）自動部署為 Live，耗時 1 分 35 秒；公開首頁及 CSS 為 HTTP 200，暖機首頁三次中位數 0.141 秒，管理頁未登入時正確導向登入，HTTP 正確轉 HTTPS。

隔離 PostgreSQL 可用後，只執行 `feedback.test_analysis_jobs_postgres`；使用 `config.settings_postgres_test`，並明確提供 `TEST_DATABASE_URL` 與 `TEST_DATABASE_CONFIRM_ISOLATED=1`。該角色必須能讓 Django 測試流程建立及銷毀測試資料庫，且不得指向正式 Supabase。

## 目標

建立可持續的流程：

`收資料 → 背景統計與文字分析 → 既有 schema／Gemini → 版本化發布 → Render 快速展示`

本階段完成後，分析頁只讀已發布結果；問卷收集、權限、管理設定與工作排程仍保留必要讀寫。
模型訓練與預測延後；Windows GUI 與 one-folder EXE 原型已完成本機封裝驗證，正式安裝包與跨機器驗收仍待後續處理。

## 已有基礎

- TripAdvisor 固定版本 raw／clean／report／manifest 已建立，共 201,295 列；尚未正式匯入 Supabase。
- `AnalysisInput`、資料庫串流 `AnswerInput`、`ParquetInput` 已存在；正式工作統一分析 Supabase 問卷，Parquet 入口保留匯入前驗證／相容性用途。
- 本機管線已能重用統計、文字分析及既有 AI schema，並產生 `ai_mode=mock` 的版本化本機產物。
- `SurveyAIReportSnapshot`／`SurveyAIAnalysisStage` 已提供指紋、版本、revision 與成功結果重用。
- 預設設定下網頁仍保留 request 內重運算及同步 Gemini POST；背景 Job／單次 Worker 與可選發布只讀路徑已存在，但 migration、常駐執行、正式切換與部署尚未完成。
- `hotel_id` 在此資料版本每值一列，不可用於飯店群組切分。

## 第一階段：建立可信的執行基準

1. 找出並沿用已安裝完整專案依賴的 Python 環境；不因缺套件就直接升級全部依賴。
2. 確認測試設定固定指向隔離 SQLite 或專用測試 PostgreSQL，禁止連正式 Supabase。
3. 只補缺失或受本階段修改影響的 targeted tests；已確認且輸入、程式與環境未變的資料層驗證不重跑。
4. 記錄 Python 與關鍵套件版本、測試命令、結果及時間，作為後續修改的比較基準。
5. 若同一環境根因修正兩次仍失敗，保存證據並停止擴大處理。

完成條件：

- 受影響的共用分析、工作與發布測試具有本次可重現的通過證據；沿用資料層既有驗證時須記錄其輸入與程式未變。
- 測試過程沒有正式 DB、網路付費 API、寄信或改善項目副作用。

## 第二階段：定義工作與發布合約

1. 盤點現有 Snapshot／AI Stage 狀態與唯一約束，確認哪些欄位可直接重用。
2. 定義統一的分析來源識別：來源類型、來源版本、clean hash、管線版本及輸入指紋；回覆新增／修改／刪除須更新輸入版本，分析題目、詞典或管線變更須更新設定／管線版本。
3. 版本更新與工作排程在來源交易成功後觸發；批次匯入期間合併相同來源與目標版本的工作，避免逐列排程。
4. GET 只讀既存版本與發布指標，不以全量 Answer 或 Parquet 掃描判斷是否需要重算。
5. 定義最小工作狀態：等待、執行、成功、失敗、取消；包含 owner、租約、心跳、嘗試次數及安全錯誤碼。
6. 定義原子領取、租約到期、有限重試與取消語意，避免兩個 Worker 同時發布同一工作。
7. 若執行途中來源版本變更，舊工作結果保留為歷史但不得標為最新；交易成功後必須確保新版工作已排入或已有等價待處理工作。
8. 雲端正式結果位置選定為 Supabase PostgreSQL 的既有 `SurveyAIReportSnapshot`／`SurveyAIAnalysisStage`；Render 只讀其中有限大小的展示 payload 與發布指標。
9. 決定外部資料如何關聯既有 Snapshot；優先延伸共用來源合約，避免建立 TripAdvisor 專用核心模型。
10. 定義發布前置條件：工作所有權仍有效、輸入與管線版本未變、上游階段完整、輸出 schema 驗證通過。
11. 本機原子改名只代表本機產物完成；結果上傳並驗證後，才在短交易內重查租約／版本並更新最新發布指標。
12. 只有確定需要保存的新狀態才修改 models 並產生 migration；本階段先審查 migration，不套用開發或正式 DB。

完成條件：

- 工作、來源、結果及版本之間有單一且可測試的權威關係。
- 過期 Worker、重複請求與已取消工作不能覆蓋較新的成功結果。
- 來源交易提交後必有對應新版工作，批次操作不產生大量等價工作。
- mock、真實 Gemini 與正式發布狀態在資料與 UI 合約上可清楚區分。

## 第三階段：本機 Worker／CLI

1. 將現有單次本機分析包成可領取工作的 CLI／service，Worker 僅主動向外連線。
2. 以 `AnalysisInput` 從 Supabase Answer 串流必要欄位，保存固定輸入版本後執行統計與文字分析；外部資料須先匯入成一般問卷。
3. 長工作定期更新心跳與進度，於階段邊界檢查取消旗標。
4. 每一階段先寫暫存產物，驗證 schema、hash 與版本後完成本機產物；雲端發布另依上傳、驗證與短交易流程執行。
5. 本機離線時保留最後成功結果；新工作維持等待，不切換至 Render 重算。
6. 限制 log 只含工作 ID、版本、計數、耗時及安全錯誤碼，不輸出評論、user ID 或憑證。
7. 統計與文字階段可在各自驗證通過後先發布；AI 使用獨立狀態及版本繼續處理。
8. AI 失敗時保留新版統計／文字與上一版成功 AI，展示資料須明確標示兩者來源版本及生成時間不同。

Targeted tests：

- 兩個 Worker 只能有一個成功領取。
- 租約續期、逾期接手、取消及有限重試符合定義。
- 純計算與一般狀態測試可用 SQLite；原子領取、租約接手、過期 Worker 發布防護必須使用隔離 PostgreSQL，不以 SQLite 通過推定正式 DB 行為。
- 輸入檔於分析中變更時拒絕發布。
- 中斷只留下未發布暫存產物；重跑可安全恢復。
- 相同輸入與管線版本命中快取，不重算全量資料。
- 回覆或分析設定於工作途中變更時，舊結果不會成為最新，且新版工作已排入。

## 第四階段：Gemini 與版本化發布

1. 先把統計與文字聚合結果轉成既有 evidence／AI Stage schema；文字證據須先遮蔽，並限制證據數量、單筆長度及總長度，可在限制內包含必要的完整短評論。
2. 開發驗證預設 mock；真實付費 API 只有在任務授權後啟用。
3. 產品運行時，由管理員設定資料範圍、模型、額度與自動分析開關；啟用後可依設定呼叫 Gemini。
4. 沿用既有設定入口與必要欄位，不為自動額度另建大型管理 UI。
5. AI API 呼叫重試與一般工作重試分開計數及判斷；timeout、連線中斷等結果不確定狀態不得盲目重呼。
6. 不宣稱跨資料庫與外部 API 可保證付費呼叫 exactly-once；保存請求識別、狀態與供人工判斷的安全證據，降低重複付費風險。
7. Gemini 輸出必須通過 structured output、evidence refs、長度及列舉值驗證。
8. 發布保存來源版本、管線／prompt／schema／模型版本、生成時間、耗時及結果 hash。
9. mock 結果只供測試及格式驗證，不得作為正式 AI 結論發布。
10. 寄信與建立改善項目使用獨立授權及操作，不由分析成功自動觸發。

完成條件：

- mock 與付費 API 路徑使用相同驗證與發布合約。
- Gemini 無法取得原始識別資訊；文字證據均已遮蔽並受數量與長度上限約束。
- 發布失敗不影響上一版成功 Snapshot。

## 第五階段：Render 快速展示

1. 統計、文字與 AI 分析頁改讀 Supabase 中既有 Snapshot／Stage 的最新發布指標與有限大小展示 payload，不在 request 內掃描全量資料或呼叫 Gemini。
2. Render 不下載全量原文、大型 evidence catalog 或本機分析產物；需要的明細先在背景工作中裁切、遮蔽及彙整。
3. 沒有新成功結果時顯示最後成功版本、資料時間、狀態與限制；不得用 Render 即時計算補位。
4. 統計／文字已有新版而 AI 失敗或仍執行時，展示新版統計／文字與上一版成功 AI，清楚標示版本差異。
5. 管理員可沿用既有設定啟用自動分析範圍／額度，並查看等待、執行、失敗及取消狀態。
6. 問卷提交、登入權限、設定與排程保留必要讀寫；正式流程只使用 Django。
7. 部署前確認依賴鎖定、migration 目標與 Render 回復方案，避免網站程式落後於已套用 schema。

完成條件：

- 分析頁的回應時間不隨 201,295 列資料量線性增加。
- request 路徑沒有 DuckDB 全量掃描、NLP、訓練或 Gemini 呼叫。
- 權限、版本標示、離線狀態及最後成功結果可由 targeted tests 驗證。

## 最終端到端驗收

1. 在已授權環境記錄 migration 產生、審查、套用及回復方案；正式 DB 套用與部署須另有任務授權。
2. 在已授權的真實 API 環境驗證：建立新回覆 → 交易後自動排程 → Worker 領取 → 統計／文字發布 → 真實 Gemini → Render 顯示新版。
3. 驗證本機離線時新工作等待且最後成功結果可讀；恢復連線後可續行。
4. 驗證 AI 失敗仍保留新版統計／文字及上一版 AI，且版本差異顯示正確。
5. 驗證重新整理頁面不觸發重算，舊 Worker 不能覆蓋新版發布指標。
6. 分別記錄 Render 冷啟動與暖機後的頁面耗時、查詢次數及 payload 大小。
7. 部署前保存 DB／發布狀態回復點；若 migration、Worker 或展示驗收失敗，依回復方案恢復上一版程式及發布指標。

完成條件：

- 端到端證據含輸入版本、工作 ID、階段版本、發布時間及 Render 顯示版本，且不含敏感原文或憑證。
- 真實 API 與正式部署步驟只在對應任務已授權的環境執行。

## 延後範圍

- 預測 A：五個構面預測 overall，Dummy＋簡單模型，以 MAE 評估。
- 預測 B：text 預測低分，Dummy＋簡單模型，以 Macro-F1、低分 Recall、混淆矩陣評估。
- 固定 train／validation／test、模型產物及實驗登錄。
- Windows 安裝包／程式碼簽章／跨機器驗收、雲端發布操作，以及更細粒度的長工作進度與取消。

模型項目須在背景分析與發布流程穩定後另立執行計畫；桌面封裝在目前 GUI targeted tests 與本機資料驗證通過後繼續。

## 執行與查核索引

- 資料版本、下載、清理與隱私：[external-dataset-import.md](external-dataset-import.md)
- 代理邊界與安全規則：[AGENTS.md](../AGENTS.md)
- 現行系統與 UI：[architecture.md](architecture.md)
- 產品與 AI 流程：[README.md](../README.md)
- 實際依賴：[requirements.txt](../requirements.txt)
- 部署腳本：[build.sh](../build.sh) 與 [render.yaml](../render.yaml)

每一階段開始前重新確認 git status 與受影響程式；只跑相關測試，不重跑輸入、程式及環境皆未變的昂貴驗證。
