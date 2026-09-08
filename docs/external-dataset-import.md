# 通用外部回饋資料匯入

此功能將 CSV、JSONL、JSONL.GZ 或 Parquet 的外部回饋資料，透過 JSON mapping 匯入既有的
`Survey`、`Question`、`FeedbackSubmission` 與 `Answer`。它不下載資料，也不呼叫
Hugging Face、Gemini 或其他外部服務。

## 安全原則

- 正式資料必須來自已查證來源；測試 fixture 不得用於正式分析。
- 匯入的 submission 不綁定使用者，不保存姓名、Email 或原始外部 user ID。
- `user_id` 等敏感欄位可在記憶體中參與 SHA-256 去重，不能映射為答案或 metadata。
- console 只顯示計數、欄位名稱及錯誤代碼，不顯示完整文字或敏感值。
- 相同 dataset name、version 與去重欄位會產生相同來源雜湊，重跑不會新增回覆。
- 所有未明確列於 `metadata_fields` 的來源欄位均不會寫入 metadata。

預設敏感欄位拒絕清單為：`user_id`、`email`、`name`、`phone`、`address`。

## Mapping 格式

參考 `feedback/fixtures/external_import_test_mapping.json`。該檔案與配套 JSONL
是 synthetic test fixture，只供自動化測試。

正式 Amazon Beauty Reviews 2023 映射位於
`feedback/import_mappings/amazon_beauty_reviews_2023.json`。該映射固定 Hugging Face
revision、保留英文評論原文、忽略圖片，並只讓 `user_id` 在記憶體內參與 SHA-256
去重。資料集頁面的授權欄位目前互相矛盾（metadata 為 CC0-1.0、card 內文為
CC BY-SA 4.0），正式公開或散布資料前須由人工確認授權。

Amazon Beauty 僅保留為次要相容性案例，不是正式展示資料。

## 主要真實展示資料：TripAdvisor 飯店評論

正式 mapping：`feedback/import_mappings/tripadvisor_hotel_reviews.json`。

來源分層記錄：

1. 評論平台來源：TripAdvisor 飯店旅客評論。
2. 原始研究資料：Li、Ott、Cardie（2013），*Identifying Manipulated Offerings on
   Review Portals*。
3. Hugging Face 整理版本：`jniimi/tripadvisor-review-rating`，由整理者將評論與評分
   整併為列式資料，並以 fastText 篩選英文評論。

固定版本為 revision `1a1b7077c997eb496e402c6e9c97d91989eb24bb`，config 為
`default`，split 為 `train`。Hugging Face 頁面標記與資料卡均列 Apache-2.0；使用時
仍應保留原始研究引用及 TripAdvisor 來源聲明。

下列 seed 42、5,000 筆遠端抽樣是早期展示與相容性驗證流程，現在標記為**歷史小樣本
流程**，不再是正式資料層。現行目標是固定版本的全量有效資料；不再重複遠端掃描或
把全量轉成 CSV。

### 歷史小樣本流程：安全準備遠端樣本

`prepare_huggingface_sample` 會核對最新 commit SHA，再透過 Dataset Viewer 提供的
Parquet 以 DuckDB 串流掃描。記憶體只保留固定大小 reservoir，不會下載完整 CSV、
載入 `.pkl`，也不會把 201,295 筆全部載入記憶體。

```powershell
python manage.py prepare_huggingface_sample `
  --mapping "feedback/import_mappings/tripadvisor_hotel_reviews.json" `
  --output "data/import/tripadvisor-hotel-sample-5000.csv" `
  --limit 5000 `
  --seed 42
```

原始 `user_id` 只在 DuckDB 串流中與 `hotel_id`、`post_date` 共同產生
`source_identity_sha256`，不會輸出至 CSV、資料庫或 console。輸出只保留 mapping
所需欄位；`review`、`char`、`stay_year`、`freq`、`lang` 均忽略。

完成後依序執行 `--schema-only` 與 `--dry-run`。只有 `overall` 或 `text` 缺失會排除
整列；個別構面或 `title` 缺失只省略該題 Answer，並記錄在
`missing_answer_counts`。

### 歷史小樣本流程：25 筆唯讀驗證紀錄（2026-09-05）

- 固定 revision：`1a1b7077c997eb496e402c6e9c97d91989eb24bb`
- config／split：`default`／`train`
- Dataset Viewer Parquet：
  `hf://datasets/jniimi/tripadvisor-review-rating@~parquet/default/train/0000.parquet`
- 本地樣本：`.tmp_tripadvisor_sample_25.csv`
- 樣本 SHA-256：`871832b46d275b84084796e5aa9252aea2bea0433f4d0fe2c95d969890cb9e36`
- 抽樣方法：DuckDB reservoir、等機率、不放回、seed 42；只保留 25 筆及 mapping
  必要欄位。
- 抽樣耗時：約 6 分 11 秒（遠端 HTTP range scan）。
- 隱私：輸出沒有 `user_id`，保留 25 個互異且格式正確的
  `source_identity_sha256`。
- schema-only：25 筆、0 無效列；dry-run：讀取 25、有效唯一 25、抽樣 25、
  新增 0、跳過 0、重複 0，資料庫零寫入。
- 全量評分異常值掃描已依指示取消，狀態為「待固定 Parquet 下載後本機檢查」，
  不得視為已通過。

本次只在 Django 自動建立並銷毀的隔離 SQLite 測試資料庫驗證正式寫入時的隱私與
冪等性；未寫入開發、正式或 Supabase 資料庫。

### 2026-09-05 收尾與實際架構盤點

本回合開始時未發現 TripAdvisor、Parquet 或資料準備程序仍在執行，因此沒有終止
其他程序。25 筆樣本雜湊未變；欄位不含 `user_id`，25 個
`source_identity_sha256` 皆為互異且格式正確的 64 字元 SHA-256。以同一樣本重跑：

- schema-only：25 筆、0 無效列，各欄位在此小樣本皆無空值。
- zero-write dry-run：讀取 25、有效唯一 25、抽樣 25、新增 0、跳過 0、重複 0。
- `feedback.test_tripadvisor_import`：4/4 通過；隱私與冪等性只在自動建立並銷毀的
  記憶體 SQLite 測試資料庫驗證。
- 開發、正式與 Supabase 資料庫寫入：0。
- 首次嘗試因系統 PATH 沒有 Python 而未執行；改用工作區內建 Python 後完成，
  未安裝或升級套件，亦無資料副作用。
- 先前全量遠端異常值掃描已取消；目前沒有殘留程序。全量評分範圍、缺失、異常、
  `hotel_id` 語意與唯一值分布均為「待本機檢查」。

實際已存在：`Survey`／`Question`／`FeedbackSubmission`／`Answer`；以 Answer ORM 為
輸入的 Pandas／SciPy 統計與字典式 NLP；`SurveyAIReportSnapshot` 與
`SurveyAIAnalysisStage` 的指紋、版本、revision 及成功結果重用；Gemini structured
output 驗證；通用匯入 mapping、來源批次／去重追蹤，以及 Amazon、TripAdvisor
mapping。現有 TripAdvisor 樣本工具會遠端掃描 Parquet 並輸出 CSV，僅視為既有小樣本
工具，不再用於全量流程。

當時尚未存在（後續進展見下方全量資料層及本機分析盤點）：本機完整 Parquet／清理 Parquet 資料層、固定訓練切分、共用分析輸入介面、
模型產物與實驗登錄、一般分析 Job／Worker 的原子領取、租約、心跳、取消與有限重試。
目前統計及文字頁仍可在 HTTP request 中從 Answer 計算，AI snapshot／stage 也由 POST
request 同步建立或呼叫 Gemini；尚未達成 Render 僅讀已發布 Snapshot 的目標。

先前規劃的最小方案（已於本回合完成）：

1. 新增專用且受 Git 忽略的本機資料目錄與來源 manifest；固定 repo、revision、
   config、split、Parquet 路徑、大小與 SHA-256。
2. 實作單次串流下載至 `.part`，驗證完整性後原子改名；目的檔與 manifest 命中即拒絕
   重載，不轉整份 CSV、不讀 Pickle。
3. 以 DuckDB 對本機原始 Parquet 產生全量驗證報告：schema、筆數、各欄有效 N／缺失／
   異常、重複鍵，以及 `hotel_id` 的語意證據與基數分布。
4. 產生移除 `user_id`、保留穩定去重雜湊的清理 Parquet及版本 manifest；先定義
   Answer 與外部表格共用的唯讀分析輸入契約，不改核心模型、不寫 Supabase。

## 全量本機 Parquet 資料層（2026-09-05）

不可變來源鎖定檔為
`feedback/import_mappings/tripadvisor_hotel_reviews_source.lock.json`：

下載任務須固定來源 revision 與 Viewer 轉換 commit，保存來源對應證據及全部分片清單／大小；
浮動分支別名不能作為固定版本證據。版本及本地完整性驗證命中就重用，不重複遠端掃描。

- 來源 repo：`jniimi/tripadvisor-review-rating`
- 來源 revision：`1a1b7077c997eb496e402c6e9c97d91989eb24bb`
- Dataset Viewer `/parquet` 回應的 `x-revision` 與上述來源 revision 相同。
- Viewer `refs/convert/parquet` 當時解析為不可變 commit
  `22d127d6bcd9f652435844608051c3ea2fccfd91`；下載 URL 固定使用此 SHA，不使用
  `~parquet` 或 `main`。
- Viewer 清單只有一個分片：`default/train/0000.parquet`，220,909,380 bytes；Hub
  LFS SHA-256 為
  `288d4befe7257460de95d63f0e1553f33b17d075c733739e5577358782f60571`。

`prepare_local_dataset` 使用 15 秒連線／60 秒讀取 timeout，串流寫入 `.part`，失敗最多
重試一次。只有大小、Hub LFS SHA-256 與 Parquet 可讀性都通過才原子改名。自行計算的
本地 SHA-256 用於完整性比對，本身不視為來源真實性證明。完整 manifest 命中後只驗證
本地產物，不發送請求或重跑全量報告。

```powershell
python manage.py prepare_local_dataset `
  --source-lock feedback/import_mappings/tripadvisor_hotel_reviews_source.lock.json `
  --output-root data/local/tripadvisor-review-rating
```

產物分層且全部受 Git 忽略：

- raw：`data/local/tripadvisor-review-rating/raw/default/train/0000.parquet`
- clean：`data/local/tripadvisor-review-rating/clean/tripadvisor-clean-1a1b7077c997.parquet`
- report：`data/local/tripadvisor-review-rating/report/full-validation-1a1b7077c997.json`
- manifest：`data/local/tripadvisor-review-rating/manifest/dataset-manifest.json`

Windows raw 根目錄 ACL 僅保留目前使用者與 SYSTEM 完整存取。raw 仍含 `user_id`；clean
移除 `user_id`，但評論正文可能含個資，不能宣稱完全匿名。

全量檢查只執行一次，總耗時 36.541 秒：輸入 201,295 列、保留 201,295 列、基本排除
0、完全重複移除 0、同鍵不同內容衝突 0，計數可完整對帳。六項評分皆為 1–5，缺失與
非法值均為 0；正文缺失、空白、超過 50,000 字及日期異常均為 0。clean 大小
68,261,685 bytes，SHA-256 為
`8892cf5be77ea70df321aa05c090ea86d76a0d7fbbe10cf620e408e0ba0309c5`。

`hotel_id` 的固定資料卡語意是飯店唯一識別碼；但本版本實際有 201,295 個不同值，且
每值只有一列，因此不得用於飯店分組或群組切分。這是資料行為證據，不單靠分布宣稱
已證明欄位的真實世界語意。

清理口徑：缺失／非法 `overall`、缺失或空白或過長正文、缺失身分鍵構成欄位、不可解析
日期才排除；個別構面缺失保留為 null，非法非空值改為 null 並計數。完全相同列每個
穩定鍵留一列；同鍵不同內容全部排除並回報衝突，不靜默選擇。正文不翻譯、改寫、補造
或截斷。

### 後續本機分析盤點（2026-09-05；程式存在不等於正式部署）

共用分析輸入已包含 [契約](../feedback/analysis_input.py) 與
[AnswerInput／ParquetInput adapters](../feedback/analysis_adapters.py)。
[本機分析管線](../feedback/background_analysis.py) 重用統計、詞典 NLP 與既有 AI schema，
[CLI](../feedback/management/commands/analyze_local_dataset.py) 只產出本機 mock 結果。
已有 201,295 列、約 23.24 秒的 analysis-mock 產物紀錄；mock 不是真實 Gemini 結論。
目前另有 [工作協調服務](../feedback/analysis_jobs.py) 及
[單次確定性 Worker](../feedback/management/commands/run_analysis_worker_once.py)：前者定義版本、
租約、心跳、有限重試、取消及發布前核對。正式產品流程統一從 Supabase 的 Answer 產生
無原始評論的本機產物，再把統計／文字 Snapshot 指標原子發布。另有需明確付費授權旗標的
[單次 AI Worker](../feedback/management/commands/run_ai_worker_once.py)，以及預設停用的
`ANALYSIS_READ_PUBLISHED_ONLY` 頁面切換。發布交易會把有限展示副本保存到狀態表，Render
讀取時不需載入完整 Snapshot evidence catalog。0015～0018 已套用至設定的 Supabase；
雙 Worker 競爭已通過隔離 PostgreSQL 17.11 驗證，真實 Gemini 仍未呼叫。

[持續輪詢 Worker](../feedback/management/commands/run_analysis_worker.py) 可依序領取 deterministic
與已明確授權的 AI 工作，不開放本機服務埠；目前只是 CLI，尚未安裝為系統服務。
`ANALYSIS_AUTO_AI_ENABLED` 預設關閉；啟用時只在新版統計／文字成功發布的同一交易內排入
對應 AI 工作，AI Worker 呼叫 provider 前仍會重查基礎版本。

2026-09-07 架構校正：網站問卷回覆仍以 Supabase 為權威來源；大型固定外部資料則由
Supabase 保存 `Survey／Question`、來源版本、工作狀態及版本化發布結果，完整列保留在
固定版本本機 Parquet。`AnswerInput` 與 `ParquetInput` 共用相同統計、文字、工作租約及
Snapshot 發布流程，不另建資料集專用分析核心。通用匯入器仍保留給小型或確實需要逐筆
線上管理的資料，不再要求把大型資料全部展開成 Answer。

2026-09-07 已以固定 clean SHA-256
`8892cf5be77ea70df321aa05c090ea86d76a0d7fbbe10cf620e408e0ba0309c5` 完成 201,295 筆
正式 deterministic 工作並發布 Snapshot；發布 payload 14,912 bytes，不含 `user_id` 或
評論全文。評論長度保留原始字元數計算，展示改為固定區間並增加四分位數及第 95 百分位。

尚待整合：Worker 程序服務安裝，以及部署後正式啟用發布只讀模式；問卷收集、權限、
設定與工作排程仍保留必要讀寫。預設設定下頁面仍有 request 計算與同步 Gemini POST 路徑。

### 分析來源登錄（大型 Parquet）

大型資料不再由桌面程式以問卷 slug 或預設資料夾推測。`SurveyAnalysisSource` 指定問卷
目前使用一般 Answer 或外部資料；`ExternalDatasetVersion` 保存不可變的來源名稱、revision、
清理版本、內容 SHA-256、列數、mapping 版本與非敏感來源證據。`AnalysisJob` 和發布 manifest
固定保存來源 kind／ref／version；來源切換後，舊 pending 工作會取消，舊 Worker 在發布前會被拒絕，
舊 Snapshot 仍保留以供回看。

使用 `register_external_analysis_source --dry-run` 先核對 manifest 與 mapping；確認目標資料庫及
授權後才執行實際登錄。登錄不寫入任何 Parquet 列、評論、`user_id` 或本機路徑。桌面端以
`%LOCALAPPDATA%\FeedbackInsightHub\datasets.json` 將已登錄的 `source_ref`／`source_version`
精確對應到本機資料根目錄；檔案不存在或版本不符時顯示「本機未就緒」，不會退回分析舊 Answer。
完整外部資料不再經 Supabase 遠端逐列串流；本機 Parquet 完整性會在工作開始前驗證。
現階段先完成背景分析到發布展示；訓練切分與預測模型延後。GUI 與 one-folder EXE 原型已提供本機資料驗證及 mock 分析預覽；安裝包、跨機器驗收及雲端發布操作尚未完成。

2026-09-06 的 `feedback`／`accounts` 受影響範圍共 204 項通過；測試 DB 均建立後銷毀，
未呼叫真實 Gemini、未寄信。另以臨時 PostgreSQL 17.11 隔離叢集通過 2 項雙 Worker
原子領取、租約接手及舊租約發布拒絕測試；Supabase 僅執行已授權 migration 及唯讀完整性核對。

Targeted fixture tests 共 4 項全部通過：下載中斷重試一次仍不發布、完整 raw 快取不連網、
清理／重複與衝突計數、完整 manifest 快取重用。測試未連正式資料庫。

主要設定：

- `mapping_version`：mapping 版本。
- `dataset`：`name`、`version`、`source_url`、`license`。
- `survey`：`title`、可選 `slug`、`description`、`is_active`。
- `timestamp_field`：來源時間欄位，可接受 Unix 秒、Unix 毫秒或 ISO datetime。
- `deduplication_fields`：產生穩定 SHA-256 的來源欄位。
- `source_item_id_field`：可選的非敏感外部項目識別欄位。
- `metadata_fields`：允許保存至來源 metadata 的非敏感欄位白名單。
- `questions`：來源欄位與既有 Question 的映射。

Question 題型支援：

- `integer`
- `decimal`
- `scale`
- `single_choice`
- `short_text`
- `long_text`

measurement level 使用既有值：`nominal`、`ordinal`、`discrete`、`continuous`、`text`。

允許的正規化規則只有：

- `strip`
- `integer`
- `decimal`
- `boolean`
- `datetime`
- `text_length`
- `safe_truncate`

未設定 `safe_truncate` 的文字會在 50,000 字元內原樣保存；超過限制時整列排除並
記錄 `text_too_long`，不會靜默截斷。正式 Amazon 映射未使用 `safe_truncate`。

設定檔不會執行 Python 表達式，不支援 `eval` 或自訂程式碼。

## CLI

先安全檢查結構：

```powershell
python manage.py import_feedback_dataset `
  --input "data/import/reviews.jsonl.gz" `
  --mapping "data/import/reviews.mapping.json" `
  --schema-only
```

完整解析、清理及抽樣，但不寫入資料庫：

```powershell
python manage.py import_feedback_dataset `
  --input "data/import/reviews.jsonl.gz" `
  --mapping "data/import/reviews.mapping.json" `
  --dry-run `
  --limit 25 `
  --seed 42
```

正式全量匯入固定清理 Parquet（會寫入目標 DB，執行前須確認 migration、連線及授權）：

```powershell
python manage.py import_feedback_dataset `
  --input "data/local/tripadvisor-review-rating/clean/tripadvisor-clean-1a1b7077c997.parquet" `
  --mapping "feedback/import_mappings/tripadvisor_hotel_reviews.json" `
  --all-rows `
  --batch-size 500
```

Amazon Beauty CSV 範例：

```powershell
python manage.py import_feedback_dataset `
  --input "data/import/amazon_beauty_reviews_dataset.csv" `
  --mapping "feedback/import_mappings/amazon_beauty_reviews_2023.json" `
  --dry-run `
  --limit 1000 `
  --seed 42
```

通過 dry-run 後移除 `--dry-run` 才會正式寫入。Hugging Face dataset card 的舊範例
使用 `full`，目前 Dataset Viewer 則公開為 `default/train`；匯出本地 CSV 前應先以
`dataset.keys()` 確認實際載入版本的 split 名稱。

`--limit` 使用固定 seed 的等機率 reservoir sampling，不按評分類別設定配額，也不會
把來源檔整批載入記憶體。相同檔案、mapping、limit、seed 與資料順序可重現相同樣本。
此模式只供歷史小樣本／相容性驗證；`--all-rows` 逐批匯入全部有效唯一列，不抽樣，
`DatasetImportBatch.requested_limit` 與 `random_seed` 記為 null、`sampling_method` 記為 `all_valid_rows`。

## 驗證匯入

完成後應確認：

1. `DatasetImportBatch` 的來源、檔案 SHA-256、mapping 版本、seed 與計數正確。
2. 每份正式匯入 submission 都有一筆 `ImportedSubmissionSource`。
3. `source_namespace + source_record_key` 沒有重複；同鍵不同 `content_sha256` 必須列為衝突，不得覆寫。
4. submission 的 `user`、`respondent_name`、`respondent_email` 與追蹤同意均為空或關閉。
5. 統計入口可依匯入問卷讀取 Answer。
6. 正式問卷沒有 TEST／fixture submission。

完整大型資料檔、原始使用者識別碼及任何憑證都不得提交到 Git。
