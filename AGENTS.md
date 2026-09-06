# AGENTS.md

本檔是根目錄代理指引；依使用者本次需求限定範圍，規劃與歷史紀錄不代表功能已完成。
只讀本任務需要的參考資料；實際程式、資料產物與驗證證據優先於舊對話摘要。

## 工作邊界

- 開始修改前讀本檔、確認實際 Git 根目錄與 git status，保留使用者及其他協作者修改。
- 不因文件列出命令就執行；不自行正式匯入、部署、寄信、commit 或 push。
- schema 變更可依任務產生 migration 檔；套用至開發／正式 DB 須確認目標及授權；隔離測試 DB 可依測試流程建立與套用。
- PR 前先檢查目前分支、目標分支與差異，再依團隊策略處理；不固定自動 merge／rebase。
- 不把無關 AGENTS.md、scripts 或既有未提交修改混入功能提交；指引修改需在任務範圍內。
- 不自動開子代理、啟動完整伺服器或擴大重構；使用者要求時才依範圍進行。
- 憑證不得提交 Git、放進 log 或打包至 EXE；不輸出 .env 或連線秘密。

## 已確認現況（2026-09-05 工作區盤點）

- Django 負責頁面、權限及 ORM；核心是 Survey／Question／FeedbackSubmission／Answer。
- 網站問卷為 login-only；保留 CUSTOMER／MANAGER 權限與伺服器端驗證。
- UI 修改保留註冊頁停用的 Google 登入佔位；公開／客戶頁樣式不得污染管理頁，填答身分取自登入使用者。
- Django 是唯一後端與 ORM；頁面、填答、分析協調及本機工作台共用 Django domain service，不恢復第二套 fallback 寫入路徑。
- 統計使用 Pandas／SciPy，網站與本機 Worker 共用分析契約。
- 既有 SurveyAIReportSnapshot／SurveyAIAnalysisStage 有指紋、版本、revision 與成功結果重用。
- 網頁統計／文字頁仍可能在 request 計算；Snapshot／Gemini 產生仍有同步 POST 路徑。
- 通用匯入器及 Amazon／TripAdvisor mapping 已存在；匯入器可展開 Submission／Answer。
- TripAdvisor 本機 raw／clean／report／manifest 已存在，固定版本資料為 201,295 列。
- AnalysisInput、AnswerInput、ParquetInput 與本機統計／詞典 NLP／AI schema mock 管線已存在。
- 本機 analysis-mock 是格式與流程驗證產物，不是真實 Gemini 結論或已發布的雲端 Snapshot。
- SurveyAnalysisState／AnalysisJob、版本失效、待處理合併、租約／心跳／重試／取消及發布前版本核對已在程式定義；0015～0018 已於 2026-09-06 套用至設定的 Supabase。deterministic／AI 單次命令與持續輪詢 CLI、有限展示副本及選用的發布只讀頁已存在；仍未安裝為服務或呼叫真實 Gemini。
- 桌面工作台已把統計／文字與 Gemini 分為兩段，分別顯示時間及版本狀態；Gemini 預設停用，管理員勾選 API 額度選項後才執行。Render blueprint 已收斂為 Django 單一 web service。

## 目標架構／尚未實作

- 現階段目標：收資料 → 背景統計與文字分析 → 既有 schema 及 Gemini → 版本化發布 → Render 快速展示。
- 目標由 Supabase PostgreSQL 保存所有問卷、匯入回覆、工作權威狀態及 Snapshot；本機 Parquet／DuckDB 僅作匯入前驗證，運算由本機 Worker／EXE 執行。
- 優先重用既有分析邏輯、Snapshot／AI Stage；避免新增同義模型或資料集專用平行核心。
- 隔離 PostgreSQL 17.11 已驗證雙 Worker 原子領取、租約接手與舊租約發布拒絕；Worker 程序服務安裝尚未完成。
- 發布須核對輸入版本、管線版本與工作所有權，避免過期 Worker 覆蓋新結果。
- Render 目標：統計／文字／AI 分析頁讀取已發布結果，不執行重運算；問卷收集、權限、設定及工作排程仍保留必要讀寫。
- 本機離線時沿用最後成功結果，新工作等待且不回退雲端重算，仍為待完成目標。
- Worker 目標為主動向外連線，不公開本機服務埠；本機 CLI 不等於正式 Worker。
- 模型訓練、預測及固定訓練切分仍延後；Windows GUI 與 one-folder EXE 已能列出 Supabase 問卷，依設定執行兩段分析並發布版本化結果；安裝包、簽章、正式連線與雲端驗收尚未完成。
- 本機模型產物與實驗紀錄須版本管理及備份，不能當作可隨意刪除的快取。

## Schema 與相容性

- Django models 與 migration 定義 schema；目標 DB 實際套用狀態須另查，不能由檔案或歷史紀錄推定。
- 只有模型狀態／schema 變更才建立 migration；方法與純邏輯修改不建立空 migration。
- 不新增第二套 ORM schema 鏡像；共用資料合約以 Django models、migration 及分析輸入介面為準。
- 保護 SurveyCategory、Survey.category、Answer.analysis_text／sentiment_score／analysis_version、ImprovementDispatch.is_read。
- Survey.access_mode、Survey.AccessMode、FeedbackSubmission.source 已移除；不因舊程式或樣板恢復。
- 上述保護與移除規則可依明確需求及協調好的遷移計畫變更；不能當作永久 DB 事實。
- 不重複定義統計／文字 helper；維持既有 payload 與頁面消費端合約。
- 評分 ordinal 不得誤標 continuous；計數 discrete；各分析報有效 N、缺失與排除原因。
- 原生文字分類使用 DB KeywordCategory；JSON 是版本化 seed，非 runtime 自動載入。
- 規則變更需檢查同步流程；實際同步、重建 Answer 快取均需任務授權。
- 保留中文文字分析相容性；英文詞典覆蓋率不代表準確率，未知情緒不能當成中立。

## 真實資料與發布

- 主展示來源為 TripAdvisor；Amazon Beauty 僅保留次要相容性案例，授權衝突仍須查核。
- 不生成、翻譯、改寫或補造正式評論；fixture 僅供隔離測試，不得進入正式問卷。
- 外部正式資料須透過 mapping 建立一般 `Survey／Question／FeedbackSubmission／Answer`，與網站問卷共用排程、分析及發布流程；Parquet 直接分析只保留資料準備／相容性用途。
- 資料版本須可追溯並驗證完整性；已驗證產物重用，下載與快取細節按需讀取 [外部資料交接](docs/external-dataset-import.md)。
- 不全量轉 CSV、不讀 Pickle 或執行遠端資料集程式碼。
- raw／clean／report／manifest 分開；限制 raw 存取，大檔受 Git 忽略，不任意刪除既有成果。
- user_id 僅在受控處理中參與穩定去重；不進 clean、DB、UI 或 log；鍵不能依賴抽樣列序。
- 區分完全重複與同鍵不同內容衝突；個別構面缺失依任務保留 null，不自動排除整列。
- hotel_id 此版本每值僅一列，不能作飯店分組；值分布不能單獨證明真實世界語意。
- clean 仍可能含個資；AI evidence／公開輸出需遮蔽，不宣稱完全匿名。
- 情緒、關鍵字及 AI 結論須標示為分析結果；上傳者授權標記不等於完整權利保證。
- 開發及驗證期間，真實付費 API 與正式發布須有任務授權。
- 產品運行時，已由管理員啟用並設定範圍／額度的自動分析，可依設定呼叫 Gemini 及發布結果，不逐筆詢問。
- 未啟用者使用 mock 或停在待處理；寄信與建立改善項目另行授權。

## 命令用途與副作用

以下只是用途索引，依當次任務選擇執行，不能視為初始化或 PR 必跑清單。

| 命令／操作 | 用途與限制 |
| --- | --- |
| git status／git diff／git diff --check | 本機狀態、差異與空白檢查；不改分支歷史。 |
| python manage.py check | Django 設定檢查；先確認 Python 依賴與設定載入副作用。 |
| python manage.py test <target> | 只跑相關測試；可能建立／銷毀測試 DB，先確認隔離，禁止使用正式 DB。 |
| python manage.py migrate --check | 查 migration 套用狀態，會存取所選 DB；不能當成離線檢查。 |
| python manage.py makemigrations | 產生 migration 檔；僅模型狀態／schema 修改才需要。 |
| python manage.py migrate | 修改所選 DB schema／資料；開發／正式 DB 須確認目標及授權，隔離測試 DB 可依測試流程套用。 |
| seed_demo／其他 seed／ensure_superuser | 會寫資料／帳號；僅明確授權與已確認目標環境才執行。 |
| seed_notification_test／通知與寄信命令 | 可能寫 DB 及實際寄信；查設定與副作用後依授權執行。 |
| 匯入、重建分析、同步詞典 | 查相應指令的 preview／dry-run 行為；dry-run 不證明正式寫入冪等性。 |
| 套件安裝、build.sh、部署命令 | 可能改環境、DB 或發布服務；不因讀文件而執行。 |

- build.sh 不執行 seed_demo、管理員建立或一次性資料修復；仍會套用 migration，正式部署前須確認目標 DB、備份與回復方案。
- 沿用已確認的 Python 環境並核對測試證據，不由測試檔存在推定通過；暫時環境事件記於交接文件。
- 測試 DB 隔離不能確認就停止 DB 測試並回報；Django 啟動可能自動載入 .env。
- 純文件修改只驗證連結／路徑、規則一致性與 git diff --check，不跑整套 Django 測試。

## 按任務讀取索引

| 任務 | 僅讀相關來源 |
| --- | --- |
| 匯入／本機資料 | [外部資料交接](docs/external-dataset-import.md)、[匯入實作](feedback/importing/)、[mapping](feedback/import_mappings/) |
| 共用／背景分析 | [輸入契約](feedback/analysis_input.py)、[adapters](feedback/analysis_adapters.py)、[本機管線](feedback/background_analysis.py) |
| 統計／文字 | [分析服務](feedback/local_service.py)、[文字管線](feedback/text_pipeline.py)、[詞典資料](feedback/data/)、[架構參考](docs/architecture.md) |
| Snapshot／Gemini | [Snapshot](feedback/ai_snapshot_service.py)、[AI Stage](feedback/ai_stage_service.py)、[AI Report](feedback/ai_report_service.py)、[README](README.md) |
| UI／權限／完整 URL | [架構與 UI 流程](docs/architecture.md)、[templates](templates/)、[feedback URLs](feedback/urls.py)、[accounts URLs](accounts/urls.py) |
| 部署／依賴 | [README](README.md)、[render.yaml](render.yaml)、[build.sh](build.sh)、[設定](config/settings.py)、[依賴來源](requirements.txt) |
| Schema／migration | [Django models](feedback/models.py)、[accounts models](accounts/models.py)、[feedback migrations](feedback/migrations/)、[accounts migrations](accounts/migrations/) |
| 歷史 UI／遷移事件 | [CHANGELOG](docs/CHANGELOG.md)；只查相關日期或事件，不要求每回合全讀。 |

## 歷史資訊與省上下文

- 2026-05 UI 細節與 migration 事故已在 CHANGELOG／架構參考；歷史命令不構成現行授權。
- 完整 URL、依賴版本及 migration 清單以程式來源為準，不在本檔複製；作用域 AGENTS 不作歷史倉庫。
- 用 rg 局部定位後讀必要片段；不掃全 repo、不貼大型 log、完整評論或敏感原文。
- 相同輸入、程式及環境且已有驗證證據者不重跑；有新差異或疑慮才補相關驗證。
- 同一根因修正兩次仍失敗就回報原因、證據及阻礙，不無限重試。
- Windows 文字讀取明確用 UTF-8，例如 Get-Content AGENTS.md -Encoding utf8。
- 先區分終端解碼錯誤與檔案損壞；不能只因亂碼顯示就重寫檔案，不硬編碼工作路徑。
