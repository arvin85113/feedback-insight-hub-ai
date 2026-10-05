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
