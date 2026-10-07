# 歷史問卷與回答接手（僅複製）

狀態：2026-10-06 已經授權唯讀匯出飲料店並匯入實際本機 DB；下方操作證據為該次紀錄。
2026-10-07 原始碼已將完成且對帳成功的歷史副本標為「僅本機」，不列為同步失敗，移除無效發布修復入口。
此次狀態修正尚未重建 EXE 或做實機畫面驗收。TripAdvisor 後續雲端登錄與上傳見
[接手計畫](superpowers/plans/2026-10-05-node-only-analysis-integration.md)，不將其與歷史副本混為一談。

「同步完成」不等於舊資料已下載。既有節點同步只處理 `owner_node` 所屬問卷的定義與收件匣；
已發布的舊問卷不能直接改指派，C4 正式搬移仍未啟用。本功能提供獨立的**歷史副本**，不是繞過指派限制。

## 資料流與邊界

1. 經授權，在 cloud 模式以 `export_survey_history` 明確指定問卷 slug，唯讀匯出 ZIP。
2. 將資料包放到本機；OWNER 由「雲端連線 → 接手歷史資料」驗證並預覽。
3. 15 分鐘內確認相同資料包，再寫入本機。確認綁定完整包 SHA-256、路徑與登入操作者。
4. 全批次在同一交易內建立一般 Survey／Question／FeedbackSubmission／Answer，沿用
   DatasetImportBatch／ImportedSubmissionSource 保存來源、原問卷定義與對帳紀錄；沒有新增 model 或 migration。
5. 相同來源及回覆 UUID 以穩定 UUID5／雜湊去重；同 ID 不同內容、本機原始資料被修改或定義換版時，整批回退，不覆蓋。

- 雲端問卷／回答／收據／指派均不修改，沒有 ACK，沒有清理、收件切換或節點領取租約。
- 本機副本使用獨立 UUID 與 `history-…` slug，標題標明「歷史副本」，停用填答與寄信。
- 不建立 SurveyDefinitionRevision／SurveySyncState，所以副本不加入目前節點同步或結果上傳。
- 工作頁顯示「僅本機保存」，雲端連線頁另列資訊提示；不算待上傳或同步失敗。以已完成且對帳成功的匯入批次判定，不只看標題。若有明確外部來源或雲端 revision，仍按該來源的正常規則檢查，不掩蓋真正錯誤。
- 「發布歷史分析」提案已延後；無須建立新雲端問卷才能繼續本機使用。未來若有真實需求，須先審查規格並另行授權。
- 不複製舊的分析／AI 結果，不觸發統計、文字、Gemini、寄信或背景工作。日後可由「分析工作」
  手動安排本機分析，本機網站只讀發布結果。這不是已完成的正式雲端接手；雲端展示依
  [節點唯一分析規格](superpowers/specs/2026-10-04-node-only-analysis-design.md) 第 4 節建立新節點問卷、驗收後再處理舊問卷，正式操作仍需另行批准。不得把本機副本直接指派或覆蓋已發布的舊雲端問卷。
- 原始 `submitted_at`、是否完整／作廢、追蹤同意、原始文字、選項代碼、題目關聯與既有匯入來源雜湊保留。
- 原始填答版本為 null 時仍為 null，不能把匯出當下定義說成填答當時定義；歷史分析只能以目前可得定義計算，
  無法重建未知的歷史語意。這是局部接手的限制，不宣稱完整歷史回測。
- 原生文字快取不移植；保留原文，避免把雲端詞典的舊快取套到本機。既有 Answer 欄位沒有刪改。
- 不匯出帳號、密碼、User ID、任意 metadata 或原始外部 user_id；姓名與 Email 預設排除。
  `--include-contacts` 需另行授權。原文仍可能含個資，ZIP 未加密，應放在受控目錄、避免公開或提交 Git。
- 雜湊證明資料包內部一致，不證明來源真實性；僅接收可信匯出工具產生的資料包。

## 一致性、容量與對帳

PostgreSQL 在第一個 SELECT 前設定 `REPEATABLE READ, READ ONLY`，問卷與全部回答讀自同一快照；
SQLite 在單一讀取交易內匯出。JSONL 每批 500 筆讀相關答案，不全量轉 CSV。

ZIP 以暫存檔完成後原子發布，拒絕覆蓋既有檔案；匯入只讀固定 ZIP 項目、不解壓路徑。
驗證／匯入共用一份私有暫存快照，避免驗證後檔案變動。上限：20 份問卷、每份 200,000 筆、
單筆 JSON 1 MiB、ZIP／解壓內容總計各 256 MiB。大型固定資料仍應使用既有 Parquet 流程。

對帳包含資料包 SHA-256、問卷定義雜湊、逐問卷 JSONL 雜湊、回覆數、答案數、題目關聯、
每筆回覆內容雜湊，以及重送時本機實際欄位與答案的再次比對。對帳範圍是本次資料包；本機可能已有其他批次，
CLI 回報包內預期筆數與本機總筆數，兩者不可混為一談。

## 操作命令（列出不構成執行授權）

先核對模式、目標 DB、指定問卷與檔案存放權限。不要在本機 node 模式把副本當成雲端正本匯出。
cloud 模式讀取現行正式 DB 前，需另行明確批准唯讀正式匯出；本機實際匯入也需批准。

```powershell
# cloud 模式：只匯出明確指定的問卷；正式資料尚未執行
python manage.py export_survey_history --survey <slug> --origin https://<網站主機> --output <新的受控ZIP路徑>

# node 模式：只驗證，不寫入
python manage.py import_survey_history --package <ZIP路徑>

# node 模式：依預覽 SHA-256 確認寫入；雲端不變
python manage.py import_survey_history --package <ZIP路徑> --confirm --expected-sha256 <預覽雜湊>
```

一般使用者也可由本機 `/node/history/` 預覽／確認。資料包預覽不做分析；大量歷史匯入建議使用 CLI，
目前表單匯入為同步交易，沒有宣稱具備大型資料背景搬移或進度續傳。

TripAdvisor 完整評論已在本機 Parquet，應由 `/node/datasets/` 驗證 manifest／mapping 並登錄；
本功能拒絕匯出外部來源問卷，避免把雲端 0 筆回答當成完整評論。

## 驗收與回退

本次執行 `node.tests.test_history_transfer`、`node.tests.test_console`、
`cloudsync.tests.test_configuration_isolation`，合計 **34 項通過**（`config.settings_test`、記憶體 SQLite）。
其中歷史資料跨資料庫驗收使用獨立暫存 cloud SQLite 子程序與 node 記憶體 DB，不啟動完整伺服器。
也驗證了人工排程後，既有 Worker 可發布本機統計／文字結果、但不產生上傳。
隔離測試不代表正式資料驗收；本次實際資料接手證據見下節。PostgreSQL 併發、EXE 更新及瀏覽器畫面尚未驗收。

隔離測試驗證唯讀 SQL、兩模式限制、來源／時間／原文保留、去重、部分寫入後衝突整批回退、
本機資料變更檢出、ZIP 上限與未知項目、固定檔案快照、確認雜湊與 OWNER 權限。
PostgreSQL SQL 契約測試不是 PostgreSQL 真實併發驗收；正式執行前仍需核對備份與目標版本。

原始雲端資料與接手前既有本機資料保持不變；錯誤匯入不留下半批副本。成功匯入後若不接受，先保留副本並另行確認，
不要自動刪資料。這段沒有正式部署、commit／push、資料清理或 Gemini 授權。

## 實際接手證據（2026-10-06）

使用者明確指定接手飲料店及完整本機 TripAdvisor，雲端全部保留。本次執行範圍：

- 正式 Supabase 連線強制 `sslmode=require`、session 預設唯讀；匯出交易在第一個 SELECT 前
  設為 `REPEATABLE READ, READ ONLY`。僅匯出 `beverage-feedback`，不包含姓名、Email 或雲端帳號。
- 包 SHA-256：`e5e9d31b702332a63a734be52e84f1472d32c518a3301bb95aa268b4a50258dc`；
  100 筆回覆、1,000 筆答案、匯出定義版本 2。匯入後逐筆再驗證為 100 筆重複、0 筆新增，對帳成立。
- 寫入實際 `%LOCALAPPDATA%\FeedbackInsightHub\data\node.sqlite3` 前，SQLite backup API
  連同 WAL 的已提交內容建立完整備份，`integrity_check=ok`；沒有執行 migration。
- 飲料店本機 slug：`history-5aa4b3c4ae745808a9df8880828a6da0`，停用填答／寄信。
- TripAdvisor 重用現有 clean 檔及 manifest／mapping；大小與 SHA-256 符合既有證據。
  既有 DuckDB 唯讀驗證 Parquet 共 201,295 列，schema 不含原始 `user_id`，沒有重新下載或建立抽樣。
  同一個本機交易透過共用 `mapping_definition`／`apply_definition` 與 `register_local_dataset`
  建立 9 題、唯一外部來源、唯一作用中版本及唯一檔案登錄；所有排程暫停。
- TripAdvisor 本機 UUID：`11c6f020-1544-5cb1-9ada-4bab03d7ce11`，slug
  `tripadvisor-hotel-review-ratings`；使用資料集頁既有穩定 UUID 規則，定義版本 0、未發布、未建立同步狀態。
  此次是本機獨立登錄，不是取得雲端舊問卷所有權，也沒有呼叫雲端來源登錄 API。
- 接手後本機：2 份問卷、100 筆 Submission、1,000 筆 Answer；TripAdvisor 201,295 列留在 Parquet，
  沒有展開到 SQLite。分析工作、結果上傳、ACK 均為 0，資料庫完整性驗證通過。
- 接手後再唯讀確認雲端：飲料店仍有 10 題／100 筆回覆／1,000 筆答案、仍啟用、未指派；
  TripAdvisor 仍有 9 題／0 筆回答及原本 201,295 列來源版本，未指派。未切換收件或刪除正文。

受控備份、資料包及對帳報告位於
`%LOCALAPPDATA%\FeedbackInsightHub\data\backups\handover-20261006-201308\`，
新資料夾 ACL 限目前 Windows 帳號；不要提交 Git 或公開分享。
保留 `node-before-handover.sqlite3`、`beverage-history.zip`、`handover-report.json`。
若需回復，須停止節點服務並另行授權操作，不能在運作中的 SQLite 直接覆蓋備份。

本次未搬入雲端既有分析結果，未執行統計／文字／Gemini、上傳、打包或部署。
現有 EXE 可重新整理問卷／資料集頁看到 DB 登錄；不據此聲稱瀏覽器畫面已驗收。
