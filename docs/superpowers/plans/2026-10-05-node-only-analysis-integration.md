# 節點唯一分析入口：分段接手

基準：`main` 的 `84055bbc22e898d6623240aacdf71315fee55a68`；2026-10-05 開始前 tracked 檔案乾淨。
保留既有未追蹤 `.claude/`、`.tmp/`。PR #29 尚未合併，不將文件當作已實作或正式操作授權。
實作分支：`codex/node-only-analysis`，由上述基準分出；使用者已於 2026-10-05 授權開 PR，準備提交／推送供草稿審閱，不構成合併或部署授權。
規範正本為 [節點唯一分析規格](../specs/2026-10-04-node-only-analysis-design.md)；本檔只記分段進度、回退與驗證證據，不作另一份規格。

## 回退邊界

- 本次只修改程式、測試與文件；不接正式 DB、不套用 migration、不部署、不呼叫 Gemini。
- 修改前版本可由上述 Git commit 取回；回退只針對本次修改，不能重設整個工作目錄或刪除其他人的檔案。
- 新增節點 migration 只建立本機路徑登錄表；正式／既有本機 DB 尚未套用。已有資料、Parquet、EXE 不刪除。
- 舊工作台保留到替代入口與隔離端到端驗收完成；正式切換另需備份、還原演練與明確授權。

## 第一段：已完成的外部來源與結果接線

- 抽出既有 manifest／mapping 驗證供 CLI 與節點共用。
- 路徑登錄綁定既有不可變 `ExternalDatasetVersion`，不是只用 repo 名稱找檔。
- 節點 Worker 從登錄表取輸入；缺少登錄、內容或 mapping 變更均失敗，不替換來源。
- 外部結果沿用 C3，附來源版本／內容與 schema hash；雲端只承認已登錄版本。
- 來源換版後舊結果保留，但不得標示最新；舊 Worker 上傳只能保留歷史。
- 不新增重複的分析來源核心；小型 Answer 匯入不因有匯入批次而被改為 Parquet。

## 後續順序與門檻

1. 節點 OWNER 頁與來源登錄／定義同步已局部實作；須補雙隔離 DB 端到端、GUI 與 PostgreSQL 併發驗收。
2. Gemini 憑證庫、逐工作確認及稽核；測試使用 mock，不實際付費。
3. 雲端停止所有分析排程、明文自測範圍限制、網站自動指派節點。
4. 兩個隔離 DB 的 API 端到端驗收；PostgreSQL 併發另驗，不能用 SQLite 推定。
5. 才移除舊工作台並重新打包；正式切換另行授權。

飲料店採「新流程驗收 → 舊問卷封存」，不先 purge。任何可接收真實回覆的切換須先完成加密，或另有具風險說明的授權。

## 第一段驗證紀錄（第二段之前）

- 第一段已實作：`feedback/external_dataset.py` 共用唯讀驗證、`node/datasets.py` 版本綁定登錄、`node.0002_localdatasetlocation`，以及 Worker／C3／新鮮度契約與舊版提示。
- 未新增 `Survey.source_kind` 平行權威；沿用 `SurveyAnalysisSource`／`ExternalDatasetVersion`。尚未實作跨端來源登錄／定義契約，不把所有匯入批次轉為 external。
- 已確認測試設定：`config.settings_test`，cloud 與 node 都為 `:memory:` SQLite、假 Gemini 金鑰、記憶體寄信後端；未連正式 DB 或真實 API。
- cloud：相關 26 項通過（9.027s）；受新鮮度及模板調整影響的 9 項再驗通過（2.786s）。
- node：相關 34 項通過（14.114s）；新增回退測試 1 項通過（0.271s）；新增完整性失敗保留上一版測試 1 項通過（2.023s）。
- `makemigrations --check --dry-run --settings=config.settings_test`（node）無缺漏；只檢查狀態，不建立或套用實際 DB migration。
- 回退測試只在隔離 DB：反向移除登錄表後，問卷與不可變來源版本仍在；登錄表會消失，日後實際回退前須備份本機 DB／路徑登錄。
- 新增 PostgreSQL 來源換版與發布競爭測試；未提供 `TEST_DATABASE_URL` 與隔離確認，故未執行，不宣稱併發已驗收。
- 未重跑 25 筆或全量真實資料；沒有全量下載、正式匯入、開完整伺服器、打包、commit、push 或部署。
- 暫存測試 home 位於受忽略的 `.tmp/codex-node-only-validation-20261005/`。原 `.claude/`、`.tmp/` 既有內容保留。
- 外部結果 API 重送冪等新增 1 項通過（0.096s）；使用 Django 測試客戶端，不是雙 DB 端到端驗收。
- 最後補入交易內再次核對所有權；外部結果相關 5 項再驗通過（0.147s）。本回合涵蓋 cloud 28／node 36 項測試場景，未跑完整套件。
- 文件新增連結／路徑與 `git diff --check` 最終檢查通過。正式 Render、EXE 與完整真實資料端到端皆未驗收。

## 第二段與協作收尾（2026-10-05）

- 在不提交修改的情況下移到 `codex/node-only-analysis`；保留原工作目錄與所有修改。分支不提供檔案系統隔離，Claude 仍須等本階段交接後再動同一資料夾。
- PR #29 固定 head `4c87fa98dd73f6f697b3c0e6f17a57847e32d37e` 的規格已納入本分支，補五點並改用單一來源權威、原子登錄 API 與版本綁定路徑。#29 未合併／未關閉；本分支提交規格與局部實作供草稿 PR 審閱，不自動處理 #29。
- 新增 `cloudapi/external_sources.py` 與 `datasets/register/`：節點所有權、metadata 白名單、來源版本／revision／游標同交易、exact retry 冪等、過期預期版本 409。雲端不接收本機路徑或評論。
- definition v2 的 external 專用來源 metadata 可同步至本機；來源換版同步，缺失來源拒絕並回退，不改成 Answer。沒有新增 core schema 或空 migration；`Survey.accepts_responses` 僅新增純邏輯拒絕 external。
- OWNER `/node/datasets/`：本機路徑驗證／預覽、15 分鐘簽章確認、穩定 UUID、經 API 登錄、稽核與 metadata 狀態。GET 不讀資料檔；HTTP 失敗不寫本機副本，手動重送不產生另一份問卷。檔案不搬動、不重下載。
- 雲端 API 成功但本機未完成可能暫時不一致；沒有宣稱跨端交易。實作檢查連結 generation、雲端回覆來源與本機版本，透過同一 UUID 重送恢復。
- 實際測試：cloud 首次 67 項中 3 項失敗，修正題目空代碼與查詢快取；最後來源登錄／定義／寫入／API 62 項全通過（0.858s），外部結果 5 項已通過（0.309s 的 14 項批次內）。
- node 頁面／定義同步／主控台 25 項通過（7.164s），來源換版同步 2 項通過（0.033s 的批次）；同批指定錯誤的舊測試名稱未執行，修正名稱後 Worker 1 項通過（1.128s）。受顯示更新影響的頁面 7 項再驗通過（3.087s）。
- `makemigrations --check --dry-run --settings=config.settings_test` 為 `No changes detected`。新增登錄競爭 PG 測試，但隔離環境未設定，未執行。只用了記憶體 SQLite／小 fixture／假 HTTP；未啟動完整伺服器。
- 仍待：雙隔離 DB 真 API／PG 併發／視覺驗收、Gemini 版本綁定確認、cloud 全面停止排程、明文自測白名單、網站指派節點。未刪工作台／EXE／資料，未對正式 DB migration、匯入、部署或付費。
- 收尾補上先同步定義、後綁定檔案的恢復：缺 locator 的失敗工作不會永久卡住；新綁定在沒有 deterministic 工作執行中時以既有版本重新排程。恢復＋重登錄冪等 2 項通過（0.453s）。
- 本段涵蓋 cloud 67／node 30 個相關測試場景（不將重跑累加）；未跑完整套件。新增兩個 PG 場景尚未執行。文件 13 處本機連結及 `git diff --check` 通過。

## 第三段：本機功能接手（2026-10-06）

- PR #30 已合併，基準 `main` 的 `2a48db8`；既有 CI run `37330613221` 三項通過，含 PG17 的來源換版／登錄／發布併發。以上取代前段當時「尚未執行」的狀態；本機沒有重跑 PG。
- 新分支 `codex/node-function-completion`；保留原未追蹤 `.claude/`、`.tmp/`、資料及舊 EXE。尚未 commit／push，本段沒有部署、正式匯入、真實 Gemini 或既有 DB 寫入。
- 修正兩個確認問題：外部來源設定錯誤時拒絕填答，不拋 500；歷史發布補建逐問卷隔離失敗、回滾序號、安全稽核並繼續其他上傳。連線頁顯示歷史錯誤，避免誤認已解決事件仍阻塞。
- 新增 OWNER `/node/jobs/` 與 `/node/settings/gemini/`：問卷筆數、最新資料／統計／AI 時間及版本，排程、取消、讀取已發布結果；GET 不掃 Parquet 或執行分析。新增中文稽核標籤。
- `node/gemini.py` 直接使用 Windows 憑證庫；`.env` key 在 node 留空。已確認本機 backend 是 `WinVaultKeyring`，但未對真實 vault 讀寫或驗證供應商金鑰。
- `NodeAIGrant`／`node.0003_nodeaigrant` 保存版本綁定確認、OWNER、到期、六次呼叫上限及已保留呼叫，不存秘密。每次 provider 前核對權限、租約及版本；timeout／崩潰的 in-flight 紀錄擋住自動重呼和新確認，需人工查核。相同簽章確認不重複付費工作。
- 沿用既有 Worker、Snapshot、AI Stage、evidence 驗證與 C3；只加限定工作領取及可注入 client 的接線。未改 prompt／核心 schema／統計引擎；AI 失敗不替換成功統計。

### 本段驗證與成品

- `config.settings_test` 記憶體 SQLite；HTTP 驗收使用獨立 temp cloud SQLite。只有隔離 DB 建立／套用 migration 及 fixture 寫入；既有本機 DB、Supabase 都未連線或改動。
- Gemini targeted：13 項全通過（4.710s）；新增輸入變更的 grant 終止 1 項及空環境 key 的 Worker 路徑於 2 項批次通過（0.956s）；最後 UI 兩項受影響測試通過（0.708s）。共有 14 個 Gemini 場景，不累加重跑。
- 入口／打包測試 12 項已通過（最初 25 項批次裡）；該批唯一失敗是新測試把 Job 的 `succeeded` 誤寫成 `completed`，修正斷言後 Worker 測試通過。執行緒除零為既有 logging 測試的刻意錯誤。
- 壞來源隔離 2 項、外部四列 fixture 的「登錄 → Worker → 真 HTTP 上傳 → 雲端最新結果」1 項通過（11 項批次 18.772s）；一般回覆的雙 DB 真 HTTP 端到端 1 項通過。外部測試初次 fixture 缺 survey metadata、再來斷言未排除 superseded 工作，均修正測試後通過，未放寬正式驗證。
- Gemini 的三段及 Worker 發布用 mock；真 API HTTP 的資料交換與 Gemini mock 管線分別驗證，不能宣稱正式環境完整 Gemini 端到端驗收。沒有重跑 25 筆／201,295 筆資料層驗證。
- migration 狀態檢查 `No changes detected`；文件連結與 `git diff --check` 通過。
- 新封裝命令 `scripts/build_desktop.ps1 -NodeOnly`；成品 [FeedbackInsightHubNode.exe](../../../dist/FeedbackInsightHubNode/FeedbackInsightHubNode.exe)，完整資料夾須一起保留。舊工作台來源／原 EXE 保留回退，新封装排除 `dearpygui`、`desktop_app.app`、`desktop_app.service`，runtime hook 拒絕 legacy 角色。
- 最後打包 53.7s；EXE 25,411,929 bytes，完整目錄 293,159,218 bytes（5,874 檔）。EXE SHA-256 `6ece4b641028d71bc9fa4b9b72504f3b121a01e3c24626a43d50b9728d98d19c`。
- 正常 node 設定、隔離 home 的 `--smoke-test` 退出 0（2.79s），沒有產生 SQLite DB；成品未找到 `.env`、`.sqlite3`、Parquet 或 Pickle，PYZ 未包含舊工作台。只證明模組／模板可載入，不代表 GUI 操作或安裝驗收。
- 測試／打包紀錄在受忽略的 `.tmp/codex-node-completion-20261006/`；本機兩次封裝均保留原成品名稱，新版本只更新本段新建的 Node 封裝。

### 仍需授權與驗收

- 正常啟動會 migrate 本機 DB；先確認使用隔離新 home 或備份既有 DB，再另行授權啟動／遷移。`node.0003` 不在 cloud 模式載入，不代表其他未來 migration 可直接合併部署。
- 實機 GUI、Windows vault 讀寫、真實 Gemini 與上傳正式站尚未驗收；不把 smoke／mock／SQLite 推定為這些證據。新確認流程的 PostgreSQL 併發未驗收。
- 雲端全面停止排程、明文自測範圍限制、網站自動指派、飲料店新流程 seed、正式切換與舊工作台退役仍待；本段只完成本機接線，不更改正式站閘門。
- 回退：先停止新節點；備份整個 node home（DB／產物／路徑／稽核），Windows 憑證另依 vault 管理。不要直接反向 migration 或刪 grant；使用已備份的原版本／原 home 回退，不刪歷史產物或重設工作目錄。

## 授權操作：建立雲端節點（2026-10-06）

- 使用者另行授權建立節點。唯讀核對 `.env` 所選 Supabase cloud DB、既有 NodeDevice 表及同名／本機憑證槽皆無紀錄後，以既有 `NodeDevice.issue` 在短交易新增 `Arvin-PC`；節點數由 0 變 1。
- UUID `ac108368-2fa8-4591-8b4d-eccb5b0ca398`，狀態 active；提交後重新連線唯讀核對成功。只寫一筆 NodeDevice，沒有 migrate、問卷指派、收件切換、回覆搬移或結果發布。
- 原始權杖只在記憶體中處理，保存到目前 Windows 使用者的憑證庫 `FeedbackInsightHub`／`cloud-token:https://feedback-insight-hub-ai.onrender.com`；讀回與雲端雜湊核對成功。沒有輸出權杖、寫入檔案／DB 明文或 clipboard。
- 尚未測試／啟用正式同步 API，也未修改本機 CloudLink、連結雲端、commit／push 或部署；後續連結與正式切換仍另行授權。建立節點不代表問卷已同步。

## 自動上傳工作流校正（2026-10-07，原始碼完成／正式接線未執行）

- 核對正本 `2026-10-01-cloud-sync-design.md` 第 5、7 節及 `2026-10-04-node-only-analysis-design.md` 第 2、3、4 節：節點所屬來源本機發布後應自動建立既有 C3 上傳紀錄，再由背景同步送出，不要求每次手動同步。
- 實際本機 SQLite 僅以 `mode=ro`／`query_only` 查證：飲料店歷史副本、TripAdvisor 均有本機發布，但定義 revision、同步狀態、上傳紀錄全部為 0。根因是先前兩份接手都只建本機副本，未完成正式節點來源接線；不是憑證消失或 Gemini 必須重呼。
- 原始碼補強：同一發布重送沿用 UUID／序號／雜湊；同步流程將漏建、未綁定或失敗回傳為 `results_incomplete`，不把網路成功當成全部上傳。工作／資料集頁只查目前發布版本的上傳狀態，不掃所有歷史 payload；進度 100% 明示本機完成，來源未綁定列入待處理。
- TripAdvisor 續接入口重用原資料集登錄服務、穩定 UUID、確認與稽核。採用較新的雲端定義後補排統計／文字；交易內再查綁定狀態，確切重送不重複失效。未綁定副本與雲端版本相同或倒退時拒絕，不只補一筆 revision 冒充驗證；不變更舊結果 evidence 的版本。
- 飲料店歷史副本仍保留本機，沒有繞過所有權直接上傳至舊問卷。原定新節點問卷／限定自測收件流程尚待實作與授權操作，不把歷史副本當成該流程已完成。
- 驗證：`config.settings_test`、記憶體 SQLite、獨立暫存 node home；工作流／排程／頁面／AI Worker 等 63 項通過（37.720s），最後資料集與工作頁補強 23 項複驗通過（8.438s，含 1 個新增場景），再驗上傳重試／C3 追補／連線頁 15 項通過（2.331s），共 79 個不重複場景。舊上傳測試需兩份結果時改為建立兩次實際發布，不再用同一發布重送假造第二份。AI provider 與雲端接收皆為 mock；未據此宣稱正式 Render 或 PostgreSQL 併發驗收。初次批次唯一失敗為新增測試插入位置造成錯誤斷言，修正測試後通過，未放寬產品規則。
- 本段沒有連正式雲端 API、改開發／正式 DB、重跑真實資料或真 Gemini，沒有 commit／push／部署／重建 EXE。舊 EXE 仍是先前進度條版本，必須另行更新才能使用此次工作流提示。
- 後續依序：批准後重建 EXE；批准正式雲端來源登錄與本機統計／文字重算，再驗證 TripAdvisor 的發布 → C3 → 雲端目前版本；飲料店依正本另做新節點流程。舊問卷、答案與備份全部保留，封存／清理及 Gemini 仍需獨立授權。

## 授權操作：TripAdvisor 正式接線與結果上傳（2026-10-07）

- 使用者在「重建 EXE、TripAdvisor 雲端登錄、重算統計／文字並自動上傳；舊資料保留、不呼叫 Gemini」的明確範圍下回覆「開始」。本段未執行 commit／push／部署、migration、飲料店改動、收件切換或 Gemini。
- 回退備份：原 Node EXE 完整目錄保留於 `.tmp/node-workflow-exe-backup-20261007-192929/`（5,878 檔、原 EXE 雜湊核對一致）；本機 SQLite 以備份 API 擷取含 WAL 的已提交資料，完整性檢查 `ok`。DB 與操作對帳位於 `%LOCALAPPDATA%/FeedbackInsightHub/data/backups/trip-cloud-workflow-20261007-193321/`，限制為目前 Windows 使用者存取。備份雲端節點定義清單及舊問卷前後中繼資料摘要，未備份／清理雲端整庫。
- 實際來源沿用已驗證的 201,295 列 Parquet，內容 SHA-256 `8892cf5be77ea70df321aa05c090ea86d76a0d7fbbe10cf620e408e0ba0309c5`。登錄仍走既有 API 與穩定 UUID `11c6f020-1544-5cb1-9ada-4bab03d7ce11`，雲端／本機採用定義版本 1、雲端 slug `pka6fikw`，沒有另造平行資料模型或展開 Submission／Answer。
- 首次完整預檢遇 Render 暫時逾時，僅建立本機備份；服務恢復後再完成預檢。登錄回覆暴露來源時間比較錯誤：本機 `+08:00` 與雲端等價 UTC 字串原被誤判不同，導致雲端已建立、本機交易回滾。改為送出 UTC 並以驗證後的時間值比較，其他來源 metadata 仍須相同；重送同一 UUID 恢復，沒有建立重複問卷。兩個新增場景驗證等價時區可重送與真正不同時間拒絕；資料集頁相關 13 項隔離測試通過（13.289s）。
- 本機限定領取 deterministic 工作 #7，lease 1,800 秒，不啟動整個 Worker 或服務。全量統計 9.118 秒、文字 70.832 秒，發布 Snapshot #4；發布交易自動建立 C3 紀錄，UUID `c9f1aa35-c0b8-4a33-a504-5f2da896513c`、序號 1。AI payload 為空，舊 AI 產物保留但不重標為此次版本。
- 本次由限定來源的既有上傳服務送出該紀錄：uploaded 1、stale 0、failed 0；內容雜湊 `d79966d7986ea548343d37af247da50d6368440594f70ee949816c5ec7a89759`，以本次操作 JSON 序列化量測 17,119 bytes。只傳既有有限結果合約，不含完整評論或本機路徑。此證明發布自動入列與既有服務實際上傳，不宣稱正常啟動器整個五分鐘排程已做實機等待驗收。
- 上傳後以正式 Supabase 的 `REPEATABLE READ, READ ONLY` 交易呼叫共用發布讀取服務：結果 available、is_latest 為 true，statistics／text freshness 為 true，analyzed_unique 為 201,295，AI freshness 為 false；新問卷雲端 Submission 數為 0。三份舊雲端問卷的定義、啟用／封存狀態、回覆／答案筆數、發布指標及內容摘要與操作前相同；本機其他來源的發布狀態未變。對帳證據為上述備份目錄的 `cloud-publication-verified.json`。
- 最終 Node 封裝包含 UTC 修正；EXE 25,483,349 bytes，SHA-256 `0f21f6c4ab6927c46ed542b86b57e8bf72685ff44e0585d1dd2071b008e1952d`。獨立暫存 home 的 smoke 退出 0、未建立 SQLite；五份 UI 資產與原始碼一致，封裝未找到 `.env`、SQLite、Parquet 或 Pickle。只做 smoke，未啟動完整節點。
- 使用 computer-use 瀏覽器唯讀開啟 Render 的 `/dashboard/stats/?survey=pka6fikw`，正確導向登入頁；沒有可用的已登入工作階段，未登入、不處理憑證。因此正式結果服務已驗證，但登入後 GUI 顯示仍待使用者驗收。舊來源仍保留，列表可能有同名 TripAdvisor；應以新 slug／UUID 區分。
- 後續正常使用：開啟更新後的 Node 封裝並保持執行，發布自動入列，啟動器每 300 秒呼叫同步；離線時保留待上傳紀錄。飲料店歷史副本仍未綁定，不能據此宣稱其新節點收件流程或上傳已完成。

## 範圍收斂：歷史副本僅本機（2026-10-07，原始碼完成）

- 「發布歷史分析」提案擱置，不新增第三種來源、雲端契約或持久化接手流程；保留提案文件供未來有實際需求時另行審查。
- 共用唯讀判斷依 `cloud-history` 批次的完成狀態、`copy_only` 與 `reconciled`；未綁定且非明確外部來源的已驗證副本標為「僅本機」。工作頁移除無效發布修復入口，第四階段顯示「僅本機保存」；雲端連線頁另列資訊，不算待上傳或同步失敗。
- 名稱相似、未對帳／失敗匯入不會被誤認。已有雲端 revision 或明確外部來源者仍按正常上傳流程核對；本機分析失敗及真實上傳失敗保留警告，不被歷史標籤掩蓋。單一歷史副本不阻塞其他已綁定來源上傳。
- 驗證：`config.settings_test`、記憶體 SQLite、獨立暫存 node home；發布狀態／同步／連線頁／工作頁 48 項通過（9.915s），既有歷史匯出 → 匯入 → Worker 發布測試補上狀態核對後通過（1 項，24.256s），共 49 個不重複場景。另將唯讀測試改為依實際 Answer 表名核對，再複驗該項通過；狀態查詢僅 SELECT 且不讀答案。紀錄在 `.tmp/local-history-status-tests.log`、`.tmp/local-history-import-status-test.log`、`.tmp/local-history-readonly-status-test.log`。
- 此次未連正式 DB 或 API、未改實際本機 DB、未跑 migrate／真 Gemini／部署／commit／push，也未重建 EXE 或做實機 GUI 驗收。先前 Node 封裝不含本節修正，不能由源碼測試推定使用者目前執行的畫面已更新。
- 原規格後續仍是新節點飲料店限定自測收件流程、雲端排程與指派範圍限制等，不以歷史副本直接覆蓋舊問卷。舊資料、備份與既有發布結果全部保留。
