# 本機節點成為唯一分析入口

狀態：2026-10-06 本機接手；本文件為規格正本，包含 PR #29 的五點修正。外部來源／資料集入口與 Gemini 工作確認已實作；獨立 DB 真 HTTP 和 mock 管線已局部驗收。正式切換、實機 GUI／憑證與真實 Gemini 尚未驗收，詳見交接。
來源：PR #29 固定 head `4c87fa98dd73f6f697b3c0e6f17a57847e32d37e` 的文件，於 `codex/node-only-analysis` 整合修訂。
本文件不構成 commit／push／合併／正式 DB／部署／付費 API 授權；驗證與回退紀錄見 [實作交接](../plans/2026-10-05-node-only-analysis-integration.md)。
前置規格：[本機節點架構總覽](2026-09-30-local-node-architecture-design.md)、[主控台與登入](2026-09-30-local-node-console-and-auth-design.md)、
[雲端同步](2026-10-01-cloud-sync-design.md)（本規格執行其第 11 節「切換」）、[問卷建立工具改版](2026-10-03-survey-builder-redesign-design.md)。

## 目標

本機節點（目前即開發機，視為一家公司）是唯一做分析的地方：問卷回覆經雲端收件匣進入節點，外部資料（TripAdvisor）在節點登錄與分析，
統計、文字與 Gemini 結果都由節點發布並上傳雲端展示。桌面工作台直連 Supabase 的舊路徑移除。

## 已確認的決策

| 項目 | 決策 |
|---|---|
| 收件匣加密 | **2026-10-04 使用者批准明文過渡方案，僅限模擬與自測資料**；接入真實顧客回覆前須完成加密子專案（雲端同步規格第 12 節）。 |
| 範圍 | 飲料店、TripAdvisor 與之後網站新建的問卷都由節點擁有與分析。 |
| 外部資料問卷 | 在節點主控台匯入，經既有雲端 API 建立（做法 A）。 |
| 舊 TripAdvisor 問卷 | 節點版結果上線前保留；之後封存；使用者確認無誤後再另行授權 `purge_survey`。 |
| Gemini | 節點主控台手動按鈕，確認後執行；自動分析（含額度）另行規劃。 |
| 桌面工作台 | 替代入口、雙隔離 DB 端到端與回退驗收完成後才移除直連模式；最終 EXE 僅啟動節點。 |
| 網站新建問卷 | 草稿自動指派給節點；未連接節點時提示發布後無法分析。 |

## 成功標準

1. 網站上 TripAdvisor 與飲料店的分析結果都來自節點上傳；雲端沒有任何問卷由雲端端執行或排程分析。
2. TripAdvisor 由節點主控台匯入：雲端出現節點擁有、已發布、不接受填答的外部資料問卷；結果上傳後網站顯示為最新。
3. 飲料店的 100 筆模擬填答經收件匣進入節點，節點分析後結果上傳；網站顯示已收件、已同步、已分析的筆數一致。
4. Gemini 只在 OWNER 於節點按下並確認後執行；金鑰不出現在 SQLite、log、EXE 或頁面。
5. 網站建立的草稿自動屬於節點；發布後填答走收件匣。
6. 雙擊 EXE 只啟動節點；`--legacy-workbench` 與 Dear PyGui 不再存在；煙霧測試與 CI 通過。

## 不在範圍

加密子專案；自動 Gemini 分析與額度；多節點、多組織；雲端擁有問卷的分析（移除後不再提供）；外部資料的新版本自動偵測。

## 1. 架構與資料流

```
顧客 ──填答──▶ 雲端（Render＋Supabase）
                ├─ 問卷定義正本（節點與網站經同一組寫入函式）
                ├─ 收件匣（明文暫存，節點收訖後刪正文）
                └─ 展示已發布結果 ◀──────────┐
                                             │ 結果上傳（雲端同步 C3）
本機節點（EXE：系統匣＋瀏覽器主控台）         │
  ├─ 本機 SQLite：回覆正本、外部來源登錄       │
  ├─ Worker：統計／文字 ──┐                  │
  ├─ 「執行 Gemini 分析」 ──┴─▶ 發布 ────────┘
  └─ 本機 Parquet（完整固定來源，有效筆數以 manifest 為準，原位置不複製）
```

- **目標而非現況**：在排程服務層阻止 cloud 模式的所有分析排程，包含尚未指派節點的問卷；不能只移除執行入口。
  現行僅擋已設定 `owner_node` 的問卷，全面阻擋待實作並驗收。無節點草稿必須顯示不能分析，不回退至 Render 運算。
- 明文過渡批准只適用於模擬／自測；另以伺服器端明確允許的問卷／資料模式限制收件，不能僅依全站 `CLOUD_INBOX_ENABLED`。
  開關未啟用或不在允許範圍的真實填答沿用既有流程或明確拒絕，不宣稱已加密；正式切換前另行驗收與授權。

## 2. 外部資料問卷

### 2.1 資料模型

- 沿用 `SurveyAnalysisSource.Kind` 與不可變 `ExternalDatasetVersion` 作為唯一來源權威；不新增同義的 `Survey.source_kind`。
- definition v2 僅對 external 問卷附加 `external_source`：來源／清理／mapping 版本、內容與 schema SHA-256、筆數、來源時間、允許的 provenance；不得含本機路徑、評論或識別資訊。一般問卷不改既有合約。
- 來源建立／換版使用專用 `POST /api/node/v1/datasets/register/`，將問卷、來源版本、definition revision 與變更游標寫入同一交易；節點只從雲端回覆／同步取得定義與來源 metadata。
- external 問卷 `accepts_responses` 一律 false，即使誤啟用 `is_active` 也不能填答；發布不依賴收件匣、不設定 `inbox_since`。mapping 題目凍結；語意改變建立另一份問卷，不改寫舊版本。
- 只有 `SurveyAnalysisSource(kind=external)` 才是 Parquet 問卷。有 `DatasetImportBatch` 不代表 external；小型匯入 Answer 保持原流程。既有問卷不以廣泛 migration 自動改類型。

### 2.2 節點「資料集」頁（OWNER）

1. 輸入既有 manifest 與 mapping 的本機路徑，先預覽再確認登錄。不得重新下載、複製完整檔案或轉存 CSV；EXE mapping 打包與原生檔案選擇器待後續驗收。
2. 沿用 `register_external_analysis_source` 的驗證：manifest 與 mapping 的資料集名稱與 revision 一致、clean Parquet 的大小與 SHA-256 相符。
   驗證邏輯移到共用函式，指令與頁面共用，不重複實作。
3. 共用 `mapping_definition` 沿用匯入器題目與代碼規則。問卷 UUID 綁 repo／mapping 識別碼／schema hash，題目 UUID 綁問卷與代碼；同一來源重送不另建問卷。
4. `POST datasets/register/` 同時建立、發布並登錄 external 來源，帶 `expected_version`。exact retry 先檢查再比對版本；不同來源須符合目前版本，409 不自動覆蓋。slug 由雲端產生；UUID 才是兩端身分。
5. 回覆失敗／逾時不寫本機副本，使用同一 UUID 明確重送；雲端可能已成功，不宣稱跨 API 與本機 DB 是單一交易。本機驗證回覆來源、雲端連結 generation，再於短交易建立定義／來源／路徑與稽核，排程統計／文字。
6. OWNER 頁只查有限 metadata，不在 GET 掃 Parquet 或雜湊；確認綁使用者、檔案 hash、雲端連結與預期版本，逾期或變更須重驗。
7. 入口顯示名稱、版本、筆數、路徑登錄狀態、最近本機發布時間、最近一次上傳狀態與結果連結；不把上一份已上傳結果誤稱為目前版本已完成。頁面存在不代表端到端驗收。
8. 曾跳過雲端登錄、只建立本機來源的副本，須由 OWNER 明確確認續接雲端來源登錄；不可只補一筆同步標記便宣稱完成接手。採用雲端定義後，以該版本重新排程統計／文字；相同登錄重送不重複失效。版本衝突停止並提示人工核對，不自動覆蓋；Gemini 仍須另外確認。

### 2.3 Worker 取得外部輸入

- 節點 `LocalDatasetLocation` 以 OneToOne 綁不可變 `ExternalDatasetVersion`，保存 manifest／mapping 路徑與兩者 SHA-256、驗證時間。不同步到雲端；不是只按 repo 找最新檔。
- `run_analysis_worker` 在節點模式下由此表組出 `ExternalInputSpec`，不再依賴 `--external-source-ref`／`--manifest`／`--mapping` 命令列參數
  （參數保留給開發與測試）。路徑不存在或 SHA-256 不符時工作失敗並在主控台顯示原因，不改用其他資料。
- 定義先同步、路徑稍後登錄時，缺 locator 的失敗工作可在檔案綁定後重新安排；不虛增輸入版本，也不重複啟動仍在執行中的 deterministic 工作。

### 2.4 結果與新鮮度

- 外部結果經 C3，附固定 source_ref／source_version／內容與 schema SHA-256／mapping 身分；雲端只接受已登錄的不可變來源。
- 最新判斷同時核對分析定義與作用中來源版本，不僅看題目；來源換版、內容或 schema 不符時舊結果保留為最後成功結果，不冒充最新。外部無收據，未分析收據數為 0，不代表分析完成。
- 登錄與發布共用 Survey 親列鎖；在短交易重新核對所有權、來源與發布指標。舊 Worker 晚上傳只進歷史，不切換展示；PostgreSQL 競爭另驗。
- 不自動偵測遠端資料更新；管理員手動登錄換版。相同來源版本 metadata 不可覆寫。
- 沿用 C3：已綁定節點問卷的本機發布與 `ResultUpload(pending)` 在同一交易完成；背景同步通常每 5 分鐘送出，斷線保留、漏建補建，同一發布重送沿用 UUID／序號／雜湊。無須每次手動同步或重新分析才上傳。
- 工作頁分別顯示本機發布與目前發布版本的上傳狀態；100% 只表示本機工作完成。未綁定、未排入或上傳失敗時明確警示；連線成功不等於結果已上傳，也不以舊版上傳狀態代表目前版本。

## 3. 節點上的 Gemini

- **金鑰**：節點「設定」頁新增 Gemini API 金鑰欄位（OWNER），存於 Windows 憑證庫（`keyring`，沿用 `cloudsync/tokens.py` 的方式）；
  頁面只顯示「已設定／未設定」，可清除。已授權節點 client 直接讀憑證庫；節點 `settings.GOOGLE_API_KEY` 留空，不讀 `.env`，避免舊路徑繞過工作確認。
- **觸發**：問卷分析頁的「執行 Gemini 分析」按鈕。
  - 統計與文字結果已發布且為最新時才可按；未設定金鑰時說明原因。
  - 確認綁定 OWNER、工作／Snapshot、輸入指紋、來源／設定／管線／prompt 版本、模型與呼叫上限；顯示估計呼叫數，實際可能因已有 Stage 少於三次。
  - 確認後來源或版本改變即失效；領取與每次外部呼叫前再核對，不能把全域 `allow_paid_ai` 當作所有工作的授權。付費 timeout 結果不確定時不盲目重呼，不宣稱跨 DB／API exactly-once。
  - Worker 執行既有 `execute_ai_job`，驗證規則不變；發布後經 C3 上傳。
- **稽核**：每次確認寫一筆 `NodeAuditEvent`（誰、何時、哪份問卷、模型）。
- Worker 平常仍不呼叫付費 API；只執行經確認而排程的 AI 工作。mock 僅供隔離測試，不正式發布為 AI 結論。

## 4. 飲料店改走收件匣

- `seed_demo_beverage` 新增節點路徑，分兩段：
  - 節點端：`seed_demo_beverage --node-create` 經 API 建立節點問卷（題目與關鍵字同現行）並發布。
  - 雲端端：`seed_demo_beverage --inbox --count 100 --seed 7` 僅對伺服器端允許的自測問卷以 `accept_submission(user=None, ...)` 送出明確標示的模擬填答，
    收據與收件匣照一般規則建立。
- 先建立新問卷、驗收同步／分析／展示，再封存舊問卷；不得先 purge。清除仍須獨立批准。
- 關鍵字分類（`KeywordCategory`）只在節點建立（雲端同步規格第 1 節：詞典為本機資料）。

## 5. 網站新建問卷

- 雲端模式 `SurveyCreateView`：`CLOUD_SYNC_PROTOTYPE_ENABLED` 開啟且恰有一個啟用中的 `NodeDevice` 時，於同一交易建立草稿並 `assign_survey_to_node`。
- 沒有啟用中的節點：仍可建立草稿，建立頁與編輯頁顯示「尚未連接本機節點，發布後不會產生分析」。
- 多個節點：不在範圍；建立頁要求選擇節點前先阻擋並說明（不自動挑選）。
- 已發布問卷的指派規則不變（發布後不可變更）。

## 6. 移除桌面工作台

- 刪除 `desktop_app/app.py`（Dear PyGui）、`--legacy-workbench` 角色與相關環境準備、`requirements` 的 dearpygui、打包腳本的 `--collect-all dearpygui`。
- `desktop_app/service.py` 中只供工作台使用的部分刪除；資料集登錄與驗證等仍需要的邏輯移到節點資料集服務（2.2、2.3）。
- 煙霧測試只檢查節點模板與分析管線版本。
- 文件（README、architecture、next-actions）移除工作台操作說明。

## 7. 切換步驟（Runbook）

前提：完整實作與雙隔離 DB／PostgreSQL／EXE 驗收完成；合併前檢查 `build.sh` 與 Render 自動部署的 migration 副作用。現有備份／還原只屬先前回報，本回合未重新確認，正式操作前須核對。寫入正式資料庫、改 Render 設定與付費呼叫都**逐步另行授權**；憑證由使用者親自操作。

| # | 步驟 | 執行 |
|---|---|---|
| 1 | 核對既有備份與還原證據；有新寫入或證據不足時重新備份並於隔離 DB 驗證還原 | 另行授權；還原不是唯讀操作 |
| 2 | 重建本機節點 SQLite（舊檔改名保留）；開 EXE 經 `/setup/` 建立 OWNER | Claude／使用者 |
| 3 | 確認伺服器端自測範圍限制已驗收；再依授權設定 Render 開關，不因全站 flag 開啟而接入真實資料 | 使用者（Dashboard） |
| 4 | 使用者在自己的終端機執行 `create_node_device --name <節點名稱>`，權杖貼進節點「雲端連線」頁 | 使用者 |
| 5 | 節點「資料集」頁匯入 TripAdvisor：建立、發布、統計／文字、上傳 | 使用者或經授權的 Claude |
| 6 | 「執行 Gemini 分析」（TripAdvisor） | 使用者，付費 |
| 7 | 確認網站新結果後封存舊 TripAdvisor 問卷；使用者再次確認後另行授權 `purge_survey` | 授權 |
| 8 | 飲料店：建立新節點問卷 → 限定自測的收件匣 → 收件／分析／上傳／展示驗收 → 經確認封存舊問卷；任何 purge 與 Gemini 另行批准 | 分階段授權 |
| 9 | 草稿 `rlcpny8x` 指派給節點 | 授權 |

## 8. 測試

- 外部問卷：來源 metadata 進 definition／revision／同步；發布不需收件匣、不設 `inbox_since`；填答拒絕且不寫收據；一般匯入不被誤改類型。
- 來源 API：所有權、撤銷、metadata 白名單、交易回退、相同版本冪等、舊 expected_version 換版拒絕；不可變 metadata 與來源改版的新鮮度。
- 排程與明文界線：cloud 包含未指派問卷均不排程；允許範圍外的真實資料不能進明文原型。
- 資料集頁：權限（只有 OWNER）、manifest／mapping 驗證失敗訊息、API 建立冪等、重複匯入不產生第二份問卷。
- Worker：由 `LocalDatasetLocation` 組出外部輸入；路徑或 SHA-256 不符時失敗並保留原結果。
- Gemini：未設金鑰／未確認不能排程；確認版本改變失效；只執行綁定工作與呼叫上限；重送確認不重複付費，timeout 不確定狀態停待處理；稽核不含金鑰或未遮蔽評論。
- 網站建立：單一節點時自動指派；無節點時提示；多節點時阻擋。
- 種子：`--inbox` 送出的回覆產生收據、收件匣項目與序號；`--node-create` 冪等。
- 端對端（沿用 C2／C3 的雙隔離資料庫測試）：外部問卷從匯入、分析到上傳後雲端顯示最新；飲料店從收件到上傳。
- EXE：沒有 `--legacy-workbench`；煙霧測試通過；打包腳本不再收集 dearpygui。
