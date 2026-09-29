# AGENTS.md

本檔只放長期有效的規則與索引。目前狀態與待辦見 [next-actions](docs/next-actions.md)，除錯線索見 [debugging-notes](docs/debugging-notes.md)；
實際程式、資料產物與驗證證據優先於任何文件或舊對話摘要。

## 工作邊界

- 開始修改前確認 Git 根目錄與 `git status`，保留使用者與其他協作者的修改；不把無關或既有未提交修改混入提交。
- 以下動作須有使用者在本次任務的明確授權：commit、push、merge、部署、正式匯入、寄信、呼叫付費 API（Gemini）、寫入開發或正式 DB（含 migrate、seed、建立帳號）。文件列出命令不等於授權。
- 產品執行期例外：管理員已啟用並設定範圍／額度的自動分析，可依設定呼叫 Gemini 並發布，不逐筆詢問；未啟用時用 mock 或停在待處理。
- 不自動開子代理、啟動完整伺服器或擴大重構，使用者要求時才依範圍進行。
- 憑證不得提交 Git、寫進 log 或打包進 EXE；不輸出 `.env` 或連線秘密。

## 架構原則

- Django 是唯一後端與 ORM；頁面、填答、分析協調與本機工作台共用 Django domain service，不恢復第二套 fallback 寫入路徑。
- 網站問卷為 login-only，保留 CUSTOMER／MANAGER 權限與伺服器端驗證；填答身分取自登入使用者。
- 網站（本機與 Render）分析頁只讀已發布結果，不在 request 內做統計、NLP 或 Gemini；運算由本機 Worker／工作台完成後版本化發布。
- 發布前核對輸入版本、管線版本、來源版本與工作租約，過期 Worker 不得覆蓋新結果；離線時沿用最後成功結果。
- Supabase 保存問卷、回覆、工作狀態與發布結果；大型固定外部資料留在版本化本機 Parquet，只發布有限結果。
- 優先重用既有分析邏輯、Snapshot 與 AI Stage；不新增同義模型或資料集專用的平行核心。
- 本機 analysis-mock 只驗證格式與流程，不是真實 Gemini 結論，mock 結果不得正式發布。
- UI：保留註冊頁停用的 Google 登入佔位；公開／客戶頁樣式不得影響管理頁。

## Schema 與分析合約

- Django models 與 migration 定義 schema；目標 DB 的實際套用狀態須另查，不能由檔案推定。只有模型狀態變更才建立 migration。
- 保護 SurveyCategory、Survey.category、Answer.analysis_text／sentiment_score／analysis_version、ImprovementDispatch.is_read；
  Survey.access_mode／AccessMode、FeedbackSubmission.source 已移除，不因舊程式恢復。變更須有明確需求與遷移計畫。
- 不重複定義統計／文字 helper，維持既有 payload 與頁面消費端合約。
- 評分 ordinal 不得標成 continuous，計數為 discrete；每項分析報有效 N、缺失與排除原因。
- 原生文字分類使用 DB KeywordCategory；JSON 是版本化 seed，不在 runtime 自動載入。同步詞典、重建 Answer 快取需授權。
- 保留中文文字分析相容性；英文詞典覆蓋率不代表準確率，未知情緒不能當成中立。
- AI 文字中的數字必須能對應所引用 evidence（見 `feedback/ai_grounding.py`）；修改 prompt 會自動改變 stage prompt 版本。

## 真實資料與隱私

- 主展示來源為 TripAdvisor；Amazon Beauty 只是次要相容性案例，授權衝突仍須查核。
- 不生成、翻譯、改寫或補造正式評論；fixture 僅供隔離測試，不得進入正式問卷。
- 外部資料透過 mapping 建立一般 Survey／Question；小型資料展開成 Submission／Answer，大型固定資料由 ParquetInput 分析，兩者共用排程、分析與發布流程。
- 資料版本須可追溯並驗證完整性；已驗證產物直接重用。細節見 [外部資料交接](docs/external-dataset-import.md)。
- 不全量轉 CSV、不讀 Pickle、不執行遠端資料集程式碼；raw／clean／report／manifest 分開，大檔受 Git 忽略，不任意刪除既有成果。
- user_id 只在受控處理中用於穩定去重，不進 clean、DB、UI 或 log；去重鍵不能依賴抽樣列序。
- 區分完全重複與同鍵不同內容；個別構面缺失保留 null，不自動排除整列。hotel_id 此版本每值一列，不能作飯店分組。
- clean 仍可能含個資：AI evidence 與公開輸出需遮蔽，不宣稱完全匿名；情緒、關鍵字與 AI 結論須標示為分析結果。
- 本機模型產物與實驗紀錄須版本管理與備份，不是可隨意刪除的快取。

## 有副作用的命令

- `manage.py migrate`、`migrate --check`、seed／`ensure_superuser`、匯入、重建分析、同步詞典、通知與寄信命令都會存取或寫入所選 DB，甚至實際寄信；先確認目標與授權。dry-run 不證明正式寫入冪等。
- `build.sh` 會安裝依賴、收集靜態檔並套用 migration（不 seed、不建管理員）；部署前確認目標 DB、備份與回復方案。
- 套件安裝、打包與部署命令會改變環境或服務，不因讀到文件而執行。

## 驗證

- 測試用 `--settings=config.settings_test`（記憶體 SQLite，固定測試模型與假金鑰）；PostgreSQL 併發測試用 `config.settings_postgres_test` 與獨立 `TEST_DATABASE_URL`。無法確認隔離就停止 DB 測試並回報；Django 啟動可能自動載入 `.env`。
- 只跑與修改相關的測試；CI（`.github/workflows/ci.yml`）會跑完整套件。不由測試檔存在推定通過。
- 純文件修改只檢查連結、路徑、規則一致性與 `git diff --check`。

## 按任務讀取索引

| 任務 | 僅讀相關來源 |
| --- | --- |
| 匯入／本機資料 | [外部資料交接](docs/external-dataset-import.md)、[匯入實作](feedback/importing/)、[mapping](feedback/import_mappings/) |
| 共用／背景分析 | [輸入契約](feedback/analysis_input.py)、[adapters](feedback/analysis_adapters.py)、[本機管線](feedback/background_analysis.py)、[工作協調](feedback/analysis_jobs.py) |
| 統計／文字 | [分析服務](feedback/local_service.py)、[文字管線](feedback/text_pipeline.py)、[詞典資料](feedback/data/)、[架構參考](docs/architecture.md) |
| Snapshot／Gemini | [Snapshot](feedback/ai_snapshot_service.py)、[AI Stage](feedback/ai_stage_service.py)、[數字驗證](feedback/ai_grounding.py)、[README](README.md) |
| 發布與展示 | [發布讀取](feedback/published_analysis.py)、[views](feedback/views.py)、[templates](templates/) |
| 桌面工作台／EXE | [服務](desktop_app/service.py)、[介面](desktop_app/app.py)、[打包腳本](scripts/build_desktop.ps1) |
| UI／權限／完整 URL | [架構與 UI 流程](docs/architecture.md)、[feedback URLs](feedback/urls.py)、[accounts URLs](accounts/urls.py) |
| 部署／依賴 | [README](README.md)、[render.yaml](render.yaml)、[build.sh](build.sh)、[設定](config/settings.py)、[依賴](requirements.txt) |
| Schema／migration | [feedback models](feedback/models.py)、[accounts models](accounts/models.py)、[feedback migrations](feedback/migrations/)、[accounts migrations](accounts/migrations/) |
| 除錯／migration 異常 | [除錯筆記](docs/debugging-notes.md)；變更歷史看 Git log。 |

## 工作習慣

- 用 rg 局部定位後讀必要片段；不貼大型 log、完整評論或敏感原文。
- 相同輸入、程式與環境且已有驗證證據者不重跑。
- 同一根因修正兩次仍失敗，就回報原因、證據與阻礙，不無限重試。
- Windows 讀文字明確用 UTF-8（例如 `Get-Content AGENTS.md -Encoding utf8`）；先區分終端解碼錯誤與檔案損壞，不能只因亂碼就重寫檔案。不硬編碼工作路徑。
