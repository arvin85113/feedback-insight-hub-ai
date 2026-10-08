# 外部回饋資料：匯入與本機資料層

外部資料有兩條路徑，依資料大小選擇：

| 資料 | 路徑 | 資料列存放 |
|---|---|---|
| 小型資料（需要逐筆線上管理） | `import_feedback_dataset` 匯入成 `FeedbackSubmission`／`Answer` | Supabase |
| 大型固定資料（例如 TripAdvisor 201,295 筆） | 本機 Parquet ＋ `register_external_analysis_source` 登錄為分析來源 | 本機；Supabase 只存問卷定義、來源版本與發布結果 |

> **大型資料不得匯入 Supabase。** 免費方案資料庫上限 500 MB，超過即轉為唯讀、網站無法收問卷。
> 例：10 萬筆 TripAdvisor 樣本曾佔 391 MB 中的約 377 MB。

兩條路徑共用相同的排程、統計／文字分析、Snapshot 與版本化發布流程。

### 本機節點接手（未切換正式流程）

- `register_external_analysis_source` 沿用既有 CLI；會寫入所選 DB，必須先確認模式、目標及授權。`--dry-run` 僅驗證，不建立登錄或工作。
- 在 node 模式，除既有來源版本外，同一交易保存 `LocalDatasetLocation`；Worker 可直接依工作版本取檔，不必重傳 CLI 路徑。cloud 模式不保存本機路徑。
- 登錄會核對 clean 大小／SHA-256、manifest、mapping 與來源 revision；mapping 與 manifest 的檔案雜湊也綁定。不是重新全量檢查，也不等於上游來源真實性或授權驗證。
- C3 外部結果附固定來源身分；雲端必須已登錄同一版本。來源換版後，舊結果只能保留為上一版，舊工作晚到不切換展示指標。
- 雲端來源登錄 API、問卷定義同步及 OWNER 資料集頁已局部實作並經隔離測試，尚缺雙 DB 真 API／PG／GUI 驗收；不能只執行本機登錄就認定 Render 已接通。規範見 [節點唯一分析](superpowers/specs/2026-10-04-node-only-analysis-design.md)，證據見 [分段接手與回退](superpowers/plans/2026-10-05-node-only-analysis-integration.md)。

## 安全原則

- 正式資料必須來自已查證來源；測試 fixture 不得用於正式分析。
- 匯入的 submission 不綁定使用者，不保存姓名、Email 或原始外部 user ID。
- `user_id` 等敏感欄位只在記憶體中參與 SHA-256 去重，不能映射為答案或 metadata。
- console 只顯示計數、欄位名稱及錯誤代碼，不顯示完整文字或敏感值。
- 相同 dataset name、version 與去重欄位產生相同來源雜湊，重跑不會新增回覆。
- 未明確列於 `metadata_fields` 的來源欄位不會寫入 metadata。
- 預設敏感欄位拒絕清單：`user_id`、`email`、`name`、`phone`、`address`。
- 不下載遠端資料集程式碼、不讀 Pickle、不把大型資料整份轉成 CSV。

## TripAdvisor 飯店評論（主要展示資料）

- Mapping：`feedback/import_mappings/tripadvisor_hotel_reviews.json`
- 來源鎖定檔：`feedback/import_mappings/tripadvisor_hotel_reviews_source.lock.json`
- 來源分層：TripAdvisor 旅客評論 → Li、Ott、Cardie（2013）*Identifying Manipulated Offerings on
  Review Portals* → Hugging Face `jniimi/tripadvisor-review-rating`（整理者以 fastText 篩選英文評論）。
- 固定版本：revision `1a1b7077c997eb496e402c6e9c97d91989eb24bb`，config `default`，split `train`；
  Viewer 轉換 commit `22d127d6bcd9f652435844608051c3ea2fccfd91`，單一分片 220,909,380 bytes。
- 授權：頁面與資料卡均列 Apache-2.0；使用時保留原始研究引用與 TripAdvisor 來源聲明。

### 本機資料層

```powershell
python manage.py prepare_local_dataset `
  --source-lock feedback/import_mappings/tripadvisor_hotel_reviews_source.lock.json `
  --output-root data/local/tripadvisor-review-rating
```

串流下載至 `.part`，大小、Hub LFS SHA-256 與 Parquet 可讀性都通過才原子改名；manifest 命中時只驗證
本地產物，不重新下載或重跑全量報告。產物全部受 Git 忽略：

| 層 | 路徑 | 說明 |
|---|---|---|
| raw | `data/local/tripadvisor-review-rating/raw/default/train/0000.parquet` | 含 `user_id`；目錄 ACL 僅目前使用者與 SYSTEM |
| clean | `data/local/tripadvisor-review-rating/clean/tripadvisor-clean-1a1b7077c997.parquet` | 移除 `user_id`；評論正文仍可能含個資 |
| report | `data/local/tripadvisor-review-rating/report/full-validation-1a1b7077c997.json` | 全量驗證報告 |
| manifest | `data/local/tripadvisor-review-rating/manifest/dataset-manifest.json` | 版本與完整性證據 |

clean 版本：201,295 列，68,261,685 bytes，SHA-256
`8892cf5be77ea70df321aa05c090ea86d76a0d7fbbe10cf620e408e0ba0309c5`。六項評分皆為 1–5；
缺失、非法值、空白正文、日期異常、完全重複與同鍵衝突皆為 0。

清理口徑：缺失／非法 `overall`、缺失或空白或超過 50,000 字的正文、缺失身分鍵、不可解析日期才排除整列；
個別構面缺失保留為 null。完全相同列每個穩定鍵留一列；同鍵不同內容全部排除並回報衝突。
正文不翻譯、改寫、補造或截斷。

`hotel_id` 在此版本有 201,295 個不同值、每值一列，**不得用於飯店分組或群組切分**。

### 登錄為分析來源

```powershell
python manage.py register_external_analysis_source --survey <slug> --manifest <manifest> --mapping <mapping> --dry-run
```

先以 `--dry-run` 核對 manifest 與 mapping，確認目標資料庫與授權後才移除 `--dry-run`。登錄只寫入
`SurveyAnalysisSource`／`ExternalDatasetVersion`（來源 ref、revision、清理版本、內容 SHA-256、列數、
mapping 版本與非敏感證據），不寫入任何資料列、評論、`user_id` 或本機路徑。來源切換後舊 pending 工作
會取消，舊 Worker 在發布前會被拒絕。

本機節點在主控台「資料集」登錄已驗證的 manifest 與 mapping，本機路徑與驗證雜湊只保存在節點（`LocalDatasetLocation`），Worker 依登錄的不可變版本讀取 Parquet；Supabase 只保存來源版本與有限結果。

## 小型資料匯入（`import_feedback_dataset`）

Mapping 範例：`feedback/fixtures/external_import_test_mapping.json`（synthetic，只供測試）。
Amazon Beauty Reviews 2023（`feedback/import_mappings/amazon_beauty_reviews_2023.json`）僅為次要相容性
案例；其授權標示互相矛盾（metadata CC0-1.0、資料卡 CC BY-SA 4.0），公開或散布前須人工確認。

Mapping 主要設定：

- `mapping_version`；`dataset`（`name`、`version`、`source_url`、`license`）
- `survey`（`title`、可選 `slug`、`description`、`is_active`）
- `timestamp_field`：Unix 秒、Unix 毫秒或 ISO datetime
- `deduplication_fields`：產生穩定 SHA-256 的來源欄位
- `source_item_id_field`：可選的非敏感外部項目識別欄位
- `metadata_fields`：允許寫入來源 metadata 的欄位白名單
- `questions`：來源欄位與 Question 的映射；題型 `integer`、`decimal`、`scale`、`single_choice`、
  `short_text`、`long_text`；measurement level `nominal`、`ordinal`、`discrete`、`continuous`、`text`
- 正規化規則僅限 `strip`、`integer`、`decimal`、`boolean`、`datetime`、`text_length`、`safe_truncate`；
  設定檔不執行任何 Python 表達式

未設定 `safe_truncate` 的文字在 50,000 字元內原樣保存，超過則整列排除並記錄 `text_too_long`。

```powershell
# 1. 只檢查結構
python manage.py import_feedback_dataset --input <file> --mapping <mapping> --schema-only
# 2. 完整解析、清理與抽樣，不寫資料庫
python manage.py import_feedback_dataset --input <file> --mapping <mapping> --dry-run --limit 1000 --seed 42
# 3. 確認目標資料庫、容量與授權後，移除 --dry-run 才會寫入
```

`--limit` 使用固定 seed 的等機率 reservoir sampling；`--all-rows` 逐批匯入全部有效唯一列
（`sampling_method` 記為 `all_valid_rows`），只適用於小型資料。

### 匯入後檢查

1. `DatasetImportBatch` 的來源、檔案 SHA-256、mapping 版本、seed 與計數正確。
2. 每份匯入 submission 都有一筆 `ImportedSubmissionSource`。
3. `source_namespace + source_record_key` 沒有重複；同鍵不同 `content_sha256` 列為衝突，不得覆寫。
4. submission 的 `user`、`respondent_name`、`respondent_email` 與追蹤同意均為空或關閉。
5. 正式問卷沒有 TEST／fixture submission。

完整大型資料檔、原始使用者識別碼及任何憑證都不得提交到 Git。
