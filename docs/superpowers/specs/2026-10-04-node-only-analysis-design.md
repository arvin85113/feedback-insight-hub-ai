# 本機節點成為唯一分析入口

狀態：設計方向已確認（2026-10-04）；規格待審閱；尚未實作與驗收。
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
| 桌面工作台 | 移除直連模式；EXE 只保留節點（系統匣＋瀏覽器主控台）。 |
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
  └─ 本機 Parquet（TripAdvisor 201,295 筆，原位置不複製）
```

- 雲端本來就不為 `owner_node` 已設定的問卷排程分析（`schedule_survey_analysis`）；移除工作台後，雲端擁有的問卷不再有分析入口。
  因此網站新建的問卷一律指派給節點（第 5 節），舊 TripAdvisor 問卷封存後保留最後一次發布的結果。

## 2. 外部資料問卷

### 2.1 資料模型

- `Survey.source_kind`：`answers`（預設，問卷回覆）或 `external`（外部資料），值與 `SurveyAnalysisSource.Kind` 一致，需 migration。進入問卷定義 dict 與 revision，雲端與節點同步。
- `external` 問卷：
  - `accepts_responses` 一律為 false；雲端填答頁顯示「這是外部資料分析，不接受填答」，不建立收據或收件匣項目。
  - 發布不需要收件匣開啟，也不設定 `inbox_since`（`cloudapi/writes.py` 的 `_lifecycle` 對 `external` 略過 `PublishBlocked`）。
  - 定義由 mapping 產生，建立後唯讀（同既有匯入問卷）；`source_kind` 發布後不可變。
- 既有雲端匯入問卷在 migration 中標為 `external`：判定依據為該問卷有 `DatasetImportBatch` 或 `SurveyAnalysisSource(kind=external)`
  （目前只有舊 TripAdvisor）。不影響其已發布結果。

### 2.2 節點「資料集」頁（OWNER）

1. 選擇資料集資料夾（含 `manifest.json`），以及 mapping（`feedback/import_mappings/` 內的版本化檔案，隨 EXE 打包）。
2. 沿用 `register_external_analysis_source` 的驗證：manifest 與 mapping 的資料集名稱與 revision 一致、clean Parquet 的大小與 SHA-256 相符。
   驗證邏輯移到共用函式，指令與頁面共用，不重複實作。
3. 依 mapping 產生問卷定義（沿用 `feedback/importing/service.py` 的題目與代碼規則，抽出成共用函式），`source_kind=external`。
4. 經雲端 API 建立節點問卷（`POST surveys/`，冪等），再發布（`PUT`，`published=true`）。slug 由雲端隨機產生；節點以 UUID 識別，不再以 slug 找問卷。
5. 同步取得已發布定義後，在節點本機登錄外部來源（`register_external_dataset_version`），並記錄本機路徑（2.3），排程統計／文字工作。
6. 頁面列出已登錄的資料集：名稱、版本、筆數、對應問卷、最近分析時間與上傳狀態。

### 2.3 Worker 取得外部輸入

- 新增節點本機模型 `LocalDatasetLocation`（`node` app）：`source_ref`、`manifest_path`、`mapping_key`、`verified_sha256`、`verified_at`。不同步到雲端。
- `run_analysis_worker` 在節點模式下由此表組出 `ExternalInputSpec`，不再依賴 `--external-source-ref`／`--manifest`／`--mapping` 命令列參數
  （參數保留給開發與測試）。路徑不存在或 SHA-256 不符時工作失敗並在主控台顯示原因，不改用其他資料。

### 2.4 結果與新鮮度

- 外部問卷的結果經 C3 上傳；雲端判定最新只看定義版本（`analysis_definition_version`），沒有收據所以「尚未分析」為 0。
- 外部資料換新版本時，在節點重新登錄並分析；雲端不偵測來源版本（不在範圍）。

## 3. 節點上的 Gemini

- **金鑰**：節點「設定」頁新增 Gemini API 金鑰欄位（OWNER），存於 Windows 憑證庫（`keyring`，沿用 `cloudsync/tokens.py` 的方式）；
  頁面只顯示「已設定／未設定」，可清除。節點模式的 `GOOGLE_API_KEY` 由憑證庫讀取，不讀 `.env`。
- **觸發**：問卷分析頁的「執行 Gemini 分析」按鈕。
  - 統計與文字結果已發布且為最新時才可按；未設定金鑰時說明原因。
  - 按下後顯示確認：將呼叫 Gemini 約 3 次（統計、文字、綜合）、模型名稱；確認後才排程 AI 工作。
  - Worker 執行既有 `execute_ai_job`，驗證規則不變；發布後經 C3 上傳。
- **稽核**：每次確認寫一筆 `NodeAuditEvent`（誰、何時、哪份問卷、模型）。
- Worker 平常仍不呼叫付費 API；只執行經確認而排程的 AI 工作。

## 4. 飲料店改走收件匣

- `seed_demo_beverage` 新增節點路徑，分兩段：
  - 節點端：`seed_demo_beverage --node-create` 經 API 建立節點問卷（題目與關鍵字同現行）並發布。
  - 雲端端：`seed_demo_beverage --inbox --count 100 --seed 7` 對已發布的節點問卷以 `accept_submission(user=None, ...)` 送出模擬填答，
    收據與收件匣照一般規則建立。
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

前提：本規格的實作已合併、部署、EXE 重新打包。寫入正式資料庫、改 Render 設定與付費呼叫都**逐步另行授權**；憑證由使用者親自操作。

| # | 步驟 | 執行 |
|---|---|---|
| 1 | 備份正式 Supabase 並驗證還原（2026-10-04 已做；期間有寫入則重做） | Claude，唯讀 |
| 2 | 重建本機節點 SQLite（舊檔改名保留）；開 EXE 經 `/setup/` 建立 OWNER | Claude／使用者 |
| 3 | Render 設定 `CLOUD_SYNC_PROTOTYPE_ENABLED=true`、`CLOUD_INBOX_ENABLED=true` | 使用者（Dashboard） |
| 4 | 使用者在自己的終端機執行 `create_node_device --name <節點名稱>`，權杖貼進節點「雲端連線」頁 | 使用者 |
| 5 | 節點「資料集」頁匯入 TripAdvisor：建立、發布、統計／文字、上傳 | 使用者或經授權的 Claude |
| 6 | 「執行 Gemini 分析」（TripAdvisor） | 使用者，付費 |
| 7 | 確認網站新結果後封存舊 TripAdvisor 問卷；使用者再次確認後另行授權 `purge_survey` | 授權 |
| 8 | 飲料店：雲端 `purge_survey beverage-feedback` → 節點 `--node-create` → 雲端 `--inbox` → 節點收件與分析 → Gemini | 授權、付費 |
| 9 | 草稿 `rlcpny8x` 指派給節點 | 授權 |

## 8. 測試

- 外部問卷：`source_kind` 進 definition 與 revision；發布不需收件匣、不設 `inbox_since`；填答頁拒絕且不寫收據；舊匯入問卷 migration 後為 `external`。
- 資料集頁：權限（只有 OWNER）、manifest／mapping 驗證失敗訊息、API 建立冪等、重複匯入不產生第二份問卷。
- Worker：由 `LocalDatasetLocation` 組出外部輸入；路徑或 SHA-256 不符時失敗並保留原結果。
- Gemini：未設金鑰時不能排程；確認前不排程；確認後排程一筆 AI 工作並寫稽核；金鑰不出現在回應與 log。
- 網站建立：單一節點時自動指派；無節點時提示；多節點時阻擋。
- 種子：`--inbox` 送出的回覆產生收據、收件匣項目與序號；`--node-create` 冪等。
- 端對端（沿用 C2／C3 的雙隔離資料庫測試）：外部問卷從匯入、分析到上傳後雲端顯示最新；飲料店從收件到上傳。
- EXE：沒有 `--legacy-workbench`；煙霧測試通過；打包腳本不再收集 dearpygui。
