# 雲端同步：問卷定義、收件匣與結果快取（子專案 8 第一階段）

狀態：設計方向已確認；規格草案待審閱；尚未實作與驗收（2026-10-01）。共用決策見
[本機節點架構總覽](2026-09-30-local-node-architecture-design.md)；本機節點基礎見
[主控台與登入](2026-09-30-local-node-console-and-auth-design.md)。

## 目標

讓一台本機節點（目前即開發機，視為一家公司）與雲端（Render＋Supabase）同步：
問卷定義以雲端為正本、兩端都能編輯；顧客在雲端填答，回覆經收件匣進入本機；本機分析結果發布回雲端供遠端查看。
分析只讀本機副本與本機回覆，雲端離線不影響本機計算。

**本階段是原型**：只以隔離環境與測試資料驗收。正式網站預設關閉收件匣，真實顧客回覆維持現行流程，
直到加密子專案完成並另行批准切換（見第 12 節）。

## 成功標準

1. 兩端在線、認證有效且無待處理衝突時，在雲端或本機主控台修改問卷，另一端 5 分鐘內（或按「立即同步」後）看到相同定義；
   離線、認證失效或有衝突時，兩端顯示「待同步」或「待處理」，不承諾完成時間。版本不一致的修改被拒絕，不會覆蓋。
2. 顧客在雲端送出，資料提交後才顯示成功；回覆經收件匣進入本機，保留原始提交時間與狀態，寫成一般 `FeedbackSubmission`／`Answer`，沿用現有分析。
3. 本機寫入後崩潰、ACK 遺失或併發、部分成功、重送，都不產生遺漏、重複或重複扣減；同 ID 不同內容被隔離為衝突，不阻塞其他回覆。
4. 本機發布的分析結果出現在雲端分析頁，崩潰或回應遺失後重送不產生重複版本；問卷定義已變更時，舊結果保留為最後成功結果但不標為最新。
5. 網站依第 1 節「數量定義」顯示各項數量；各題有效 N 仍在分析結果內單獨報告。
6. 本機離線時雲端照常收件；超過處理期限或容量門檻時兩端都有明確警示，併發送出不會超收，未保存時不會顯示成功。
7. 以上以兩個獨立隔離資料庫（雲端、本機）經 API 交換的端對端測試驗收。

## 不在本階段範圍

端對端加密（下一子專案，且為接入真實資料的前提）；改善進度與顧客通知的雲端代寄；多節點、多組織；
依歷史版本讀取定義的分析輸入（本階段改以限制語意變更處理，見第 2 節）；
TripAdvisor 完整分析（子專案 2）；機器學習窗口（子專案 3）；正式網站切換與真實資料搬移的執行。

## 1. 資料歸屬

| 資料 | 正本 | 另一端 | 保留規則 |
|---|---|---|---|
| 問卷定義（問卷、題目） | 雲端 | 本機同步副本 | 指派給節點的問卷與題目在兩端都**只停用或封存、不硬刪**（第 2 節） |
| 問卷分類 | 雲端 | 本機依問卷定義內的分類名稱建立或對應 | 雲端刪除分類時，本機問卷的分類清空，不刪分類下的問卷 |
| 歷史問卷版本（`SurveyDefinitionRevision`） | 雲端 | 本機保存已同步或被回覆引用的版本 | **建立後不可修改**；兩端都不刪除仍被回覆引用的版本 |
| 回覆正文 | 本機 | 雲端收件匣暫存 | 雲端收訖後刪除正文；本機長期保存 |
| 填答收據（誰、何時、狀態，無答案） | 雲端 | 本機不需要 | 長期保存，供顧客紀錄、已填答判斷與計數 |
| 本機分析設定與詞典（`KeywordCategory`、管線設定） | 本機 | 不同步 | 本階段只在本機維護 |
| 分析歷史（Snapshot、AI Stage、本機工作） | 本機 | 不同步 | 沿用現有保留方式 |
| 雲端結果快取與發布指標 | 本機（發布端） | 雲端保存已上傳的發布版本 | 保留目前套用版本與最後成功版本（第 7 節） |

問卷只有雲端一個寫入點：本機主控台編輯問卷時經 API 寫入雲端，成功後同步回本機副本；本機離線時問卷唯讀。

**狀態不混用**：雲端收據狀態（`received`／`synced`／`quarantined`）只描述收件與同步；本機 `AnalysisJob` 租約、心跳與分析狀態只描述本機運算。
兩者不互相推斷、不共用欄位。雲端對 `owner_node` 已設定的問卷**不排程任何分析工作**（第 3 節）。

**數量定義**（網站與本機總覽一致）：

| 名稱 | 定義 |
|---|---|
| 已收件 | 收據與既有 `FeedbackSubmission` 依 `submission_uuid` 取聯集後的筆數 |
| 待收 | 收件匣 `pending` 筆數 |
| 已同步 | 收據 `synced` 筆數（僅收件匣路徑） |
| 雲端既有（未搬移） | 仍只存在雲端 `FeedbackSubmission`、尚未搬移的回覆數，單獨列出，不併入已同步 |
| 衝突 | 收件匣或收據 `quarantined` 筆數 |
| 已分析 | 雲端目前套用的發布版本所涵蓋的**唯一回覆數**（由發布指標提供）；不是分析執行次數，也不代表 Gemini 已完成 |
| 排除 | 目前發布版本中因無效、作廢或不符條件而排除的回覆數（由發布指標提供，含原因） |

## 2. 識別碼、版本與雜湊

- 跨端一律以 UUID 對應，**不使用任一端的數字主鍵**：`Survey.uuid`、`Question.uuid`（兩種模式皆新增，migration 補值）。
以下版本、revision、語意限制與「不硬刪」規則**只套用在 `owner_node` 已設定的問卷**；未指派節點的問卷維持現行行為，
本階段不改變正式網站既有問卷。

- `Survey.definition_version`：雲端每次成功修改問卷或其題目時遞增；每個版本存一份不可修改的 `SurveyDefinitionRevision`
  （`survey`、`version`、`definition`：完整定義 JSON，含問卷封存狀態與每題啟用狀態），兩端都保存。
- 雲端網站的問卷編排表單也帶 `definition_version`，與 API 用同樣的條件式寫入；不一致時拒絕並提示「版本不一致，請重新載入」。
- **不硬刪**：刪除問卷改為封存（既有 `archived_at`），刪除題目改為停用（`is_active=False`），兩者都是一次定義變更。
  因此雲端尚未搬移的舊答案不會被 CASCADE 刪除，同步也不需要另外的刪除墓碑。
- **已有回覆的題目不得改變語意欄位**（`kind`、`data_type`、`options_text`）：雲端網站與 API 一律拒絕並提示
  「此題已有回覆，請新增題目取代並停用舊題」。「已有回覆」以雲端收據或既有 `FeedbackSubmission` 為準。
  標題、說明、順序、啟用狀態可改，新增題目不受限。現有分析 adapter 依目前題目定義讀取答案，此限制確保舊答案不被套用新語意。
- 回覆：`submission_uuid` 為穩定 ID，沿用 `FeedbackSubmission.idempotency_key`；現有填答表單已帶此欄位送出，收件匣路徑直接沿用。
- **內容雜湊**（`hash_version = 1`）：對收件封套中的 `answers` 物件做標準 JSON 序列化——物件鍵排序、`separators=(",", ":")`、
  `ensure_ascii=False`、UTF-8——再取 SHA-256。**不做任何字串正規化**，字串內容（含空白、Unicode 形式）原樣參與計算；
  因此同一雜湊同時代表「內容相同」與「原文未被改動」。「緊湊格式」只指 JSON 結構中的分隔符號，不刪除字串內空白。
- 結果：每次本機發布有 `publish_uuid`、`content_hash` 與該問卷的單調遞增 `publish_sequence`（第 7 節）。

## 3. 雲端（cloud 模式）

- `NodeDevice`：名稱、權杖雜湊（不存明文）、狀態（啟用／撤銷）、`last_seen_at`。權杖在管理頁產生並只顯示一次；可撤銷、可重新產生。
- `Survey.owner_node`（可空）：指派給節點的問卷。權杖只能讀寫 `owner_node` 為自身的問卷、其收件匣與結果。
  **雲端的分析排程對 `owner_node` 已設定的問卷一律略過**（包含 `Answer`／`Question` 訊號觸發的排程），分析只在本機進行。
- `Survey.inbox_since`（可空）：該問卷切換到收件匣的時間點（第 11 節）。
- `ChangeClock`（單列）與 `SurveyChange`（`seq`、`survey`、`definition_version`）：見第 4 節「提交安全的變更序列」。
- `InboxSubmission`：`submission_uuid`（唯一）、`survey`、`envelope`（見下）、`content_hash`、`hash_version`、
  `payload_version`（本階段 1，保留給加密版本）、`size_bytes`、`received_at`、`state`（`pending`／`quarantined`）。
- **收件封套** `envelope`：`submission_uuid`、`survey_uuid`、`definition_version`、`definition_history`（`recorded`／`migration_baseline`）、
  `submitted_at`（原始提交時間）、`consent_follow_up`、`is_complete`、`voided_at`、`respondent`（`cloud_user_ref` 不透明字串、
  `name`、`email` 快照）、`answers`（以題目 uuid 為鍵的原始值）。
- `SubmissionReceipt`：`submission_uuid`（唯一）、`survey`、`user`、`submitted_at`、`consent_follow_up`、`content_hash`、
  `status`（`received`／`synced`／`quarantined`）、`synced_at`、`last_known_improvement_status`。不含答案正文。
  已填答判斷、顧客「我的回饋紀錄」、問卷計數同時讀取收據與既有 `FeedbackSubmission`，依 `submission_uuid` 去重。
- `InboxCounter`（每節點一列）：`pending_count`、`pending_bytes`。
- `PublishedResultRecord`：`publish_uuid`（唯一）、`survey`、`publish_sequence`、`content_hash`、結果的版本資訊（第 7 節）、`received_at`。
- 收件開關 `CLOUD_INBOX_ENABLED`（預設 `False`）：只有開關開啟**且**問卷已設定 `inbox_since` 時，該問卷送出才走收件匣；否則維持現行流程。

## 4. API（`/api/node/v1/`，cloud 模式）

每個請求以 `Authorization: Bearer <權杖>` 驗證；權杖撤銷回 401，存取非本節點資源回 404。log 不記錄答案正文。

| 方法 | 路徑 | 行為 |
|---|---|---|
| GET | `surveys/snapshot/` | 回傳本節點全部問卷的目前定義（含已封存問卷與已停用題目）與銜接游標 `cursor`（見下）；供首次連結與游標失效時完整重新同步 |
| GET | `surveys/changes/?cursor=<seq>&limit=50` | 依 `seq` 升冪回傳 `seq > cursor` 的本節點變更（每筆附該版本完整定義），及 `next_cursor`、`has_more`；游標不屬於本節點或早於保留期回 410 |
| GET | `surveys/<uuid>/revisions/<version>/` | 取回指定版本的不可變定義 |
| POST | `surveys/` | 本機主控台建立問卷：雲端建立並設 `owner_node` 為本節點、版本 1、寫 revision 與變更紀錄；帶本機產生的 `survey_uuid`，重送同 uuid 冪等 |
| PUT | `surveys/<uuid>/` | 帶 `expected_version`；條件式寫入（第 2 節，含封存問卷、停用題目），同時寫 revision 與變更紀錄；不一致回 409 與目前版本；違反語意限制回 422 |
| GET | `inbox/?limit=100` | 只回 `state=pending`，依 `(received_at, submission_uuid)` 排序 |
| POST | `inbox/ack/` | 逐筆 `[{submission_uuid, content_hash}]`，逐筆回傳狀態（第 6 節） |
| POST | `inbox/quarantine/` | 本機回報無法寫入的項目（內容衝突、定義版本無法取得）；雲端改為 `quarantined`，不再出現在一般批次 |
| POST | `results/` | 上傳一份已發布結果（第 7 節） |
| POST | `heartbeat/` | 更新 `last_seen_at`；回傳待收筆數、容量、處理期限、資料庫用量，以及每份問卷雲端已套用的最大 `publish_sequence` |

**提交安全的變更序列**：任何問卷定義變更的交易，先以 `UPDATE ChangeClock SET value = value + 1 RETURNING value`（或同等的 `select_for_update`）
取得序號並寫入 `SurveyChange`，到交易提交前都持有該列鎖。因此取得較大序號的交易一定在較小序號的交易提交或回滾之後才能取號，
序號順序即提交順序，不會出現「10 未提交而 11 已可見」的空洞。定義變更頻率低，序列化寫入的成本可接受。
`surveys/snapshot/` **先**讀取已提交的 `ChangeClock.value` 作為銜接游標 C，**再**讀取全部定義：序號不大於 C 的變更在讀取 C 時都已提交，
必然包含在之後讀到的定義中；讀定義時可能已看到序號大於 C 的較新變更，這些會在之後的 `changes` 再次送達。
本機套用定義一律依 `definition_version` 冪等（本機版本已大於或等於時略過），因此重複送達不影響結果；此順序在 PostgreSQL 預設隔離層級下也成立。
`SurveyChange` 保留 90 天，早於保留期的游標回 410。

## 5. 本機（node 模式）

- `CloudLink`（單例）：API 網址、雲端回報的節點 ID、問卷游標、上次成功同步時間、最近錯誤分類與訊息。權杖存 Windows 認證管理員（`keyring`）。
  API 網址或節點 ID 改變（重新連結）時清空游標並走 `snapshot` 完整重新同步。
- `PendingAck`：`submission_uuid`、`content_hash`、`created_at`。
- `ResultUpload`：`publish_uuid`、`survey`、`publish_sequence`、`content_hash`、`status`（`pending`／`uploaded`／`stale`／`failed`）、
  `attempts`、`last_error`。
- `FeedbackSubmission` 調整（兩種模式共用 schema）：`submitted_at` 由 `auto_now_add` 改為 `default=timezone.now`（可寫入原始時間，一般建立行為不變）；
  新增 `respondent_ref`（可空字串，存雲端的不透明顧客識別）。本機 `user` 維持空值：**雲端顧客 ID 不得當成本機 User 主鍵**。
- 設定頁「雲端連線」：輸入網址與權杖、「測試連線」成功後才儲存；可中斷連結。
- **本機問卷編輯改經 API**：node 模式下，現有的建立問卷、問卷編排、刪除問卷與題目等頁面不再直接寫本機資料庫，
  改呼叫 `POST`／`PUT surveys/`（刪除改為封存或停用），成功後立即拉取變更更新本機副本；未連結雲端或離線時，這些操作停用並顯示「離線中，問卷唯讀」。
  本機副本只由定義同步寫入。
- 同步模組 `cloudsync/`：`client.py`（HTTP、逾時、錯誤分類）、`definitions.py`、`inbox.py`、`results.py`、`survey_write.py`。
  啟動器每 5 分鐘排程一次，主控台提供「立即同步」。

**定義同步**：首次或重新連結時呼叫 `snapshot`，在一個本機交易內套用並保存游標；之後逐頁取回變更，每頁在一個本機交易內套用並保存
該頁的 revision，**交易提交後才推進游標**。收到 410 時改走 `snapshot`。依 uuid 更新本機副本，套用依 `definition_version` 冪等（本機已較新則略過），**永不在本機硬刪除問卷或題目**
（`Answer` 對題目為 CASCADE）；封存與停用依定義設定本機 `archived_at`、`is_active=False`，既有答案保留。定義有變更時沿用現有排程機制，
將該問卷分析標為需重算並排入本機 Worker。

**收件**：領取一批後逐筆處理：
1. 本機缺少封套的 `definition_version`：先取回該 revision；仍取不到則回報隔離，不寫入、不 ACK。
2. 以題目 uuid 對應本機題目，在**同一交易**內建立 `FeedbackSubmission`（`idempotency_key`、原始 `submitted_at`、`consent_follow_up`、
   `is_complete`、`voided_at`、`respondent_ref` 與姓名／Email 快照皆取自封套）、`Answer` 與 `PendingAck`。
3. 本機已有同 ID：雜湊相同視為重複，只補 `PendingAck`；不同則回報隔離並在總覽警示。
4. 提交後送 ACK；**只刪除回傳 `acked` 或 `already_acked` 的 `PendingAck`**。啟動時先重送殘留的 `PendingAck`。

**結果上傳**：本機發布結果時，在**同一交易**內建立 `ResultUpload(pending)`；另於每次同步開始時，補排「已發布但沒有上傳紀錄」的版本。
上傳失敗依錯誤分類處理，不影響本機發布。

## 6. ACK 與重送契約

**ACK**：每筆以單一條件式狀態轉換處理——在交易內執行
`DELETE FROM InboxSubmission WHERE submission_uuid = ? AND node = ? AND state = 'pending' AND content_hash = ?`：

| 結果 | 條件 | 雲端動作 |
|---|---|---|
| `acked` | 條件式刪除影響 1 列 | 同一交易：收據標 `synced`、以原子更新扣減 `InboxCounter` 一次 |
| `already_acked` | 影響 0 列，且收據屬本節點、狀態 `synced`、雜湊相符 | 無（不扣減） |
| `conflict` | 影響 0 列，且收據或正文的雜湊與請求不符 | 不改變既有狀態（**已 `synced` 的收據不降級**）；本機保留 `PendingAck` 並警示 |
| `not_found` | 無此收據，或不屬本節點 | 無；本機保留資料與 `PendingAck` 並警示 |

兩個 ACK 同時處理同一筆時，只有一個的條件式刪除會成功（得到 `acked` 並扣減），另一個得到 `already_acked`。
整批 HTTP 200 不代表每筆成功；本機只依逐筆狀態處理。

**顧客重送**：填答表單送出的 `idempotency_key` 即 `submission_uuid`；雲端已有同 ID 時，比對問卷、提交者與 `content_hash`：全部相同回傳原本的成功結果且不再計入額度；
任一不同則拒絕（不視為成功重送），並記錄警示。

## 7. 結果發布契約

- **序號**只防止上傳順序倒退：`publish_sequence` 小於或等於雲端該問卷已套用的最大序號（且 `publish_uuid` 不同）時拒絕為過期。
- **冪等**：`publish_uuid` 已存在且雜湊相同回傳成功、不建立新版本；雜湊不同回 409 並警示。
- **資料一致性另外判定**：上傳內容附 `definition_version`、輸入指紋（沿用現有 Snapshot 的輸入版本／資料指紋）、分析設定與管線版本，
  以及統計、文字、AI 各段各自的版本與完成狀態，和**發布指標** `coverage`：`analyzed_unique`（該版本涵蓋的唯一回覆數）、
  `excluded`（依原因分類的排除筆數），供第 1 節「已分析」「排除」數量使用。雲端套用時：
  - `definition_version` 等於雲端目前版本 → 標為**最新**；
  - 小於目前版本 → 保存為**最後成功結果**並標示「問卷已變更，待重新分析」，不標為最新；
  - 各段完成狀態原樣呈現（例如統計已完成、AI 未執行），網站不把未完成段落當成已完成。
- **序號接續**：本機發布前，以心跳取得雲端該問卷已套用的最大序號，下一個序號取 `max(本機已用最大值, 雲端最大值) + 1`；
  本機從備份還原後因此不會重用或落後於雲端已套用的序號。離線期間只用本機值，上傳遇到過期拒絕時重新取號後重送同一份結果（新 `publish_uuid`）。

## 8. 錯誤分類

| 類別 | 例子 | 處理 |
|---|---|---|
| 暫時性 | 連線失敗、逾時、5xx、429 | 漸進重試（1、2、4… 分鐘，上限 30 分鐘）；有 `Retry-After` 時至少等待其指定時間，不受 30 分鐘上限截短 |
| 權杖失效 | 401 | 停止同步，總覽提示重新連結 |
| 游標失效 | 410 | 改走 `snapshot` 完整重新同步 |
| 版本不一致 | 409（問卷） | 不重試；主控台顯示「版本不一致，請重新載入」 |
| 語意限制 | 422 | 不重試；顯示「此題已有回覆，請新增題目取代並停用舊題」 |
| 內容衝突 | ACK `conflict`、結果雜湊衝突、隔離項目 | 不重試；兩端記錄並警示，待人工處理 |
| 其他 4xx | 格式錯誤 | 不重試；記錄並警示 |

## 9. 處理期限與容量

- **處理期限**：收件匣最舊一筆 `pending` 滿 25 天警示、滿 30 天升級警示（本機總覽與雲端管理頁），不自動刪除。
- **容量門檻（可調預設）**：每節點待收正文 20,000 筆或 100 MB（`size_bytes`＝封套標準序列化後的 UTF-8 位元組數）；單筆上限 64 KB。
- **原子計數**：送出時以單一條件式更新
  `UPDATE InboxCounter SET pending_count = pending_count + 1, pending_bytes = pending_bytes + n WHERE node = ? AND pending_count + 1 <= 上限 AND pending_bytes + n <= 上限`
  判斷額度（影響列數為 0 即超限），與寫入收件匣、收據在同一交易；只有 ACK 的 `acked` 轉換會扣減（第 6 節）。
  超限則不寫入並顯示「目前暫停收件，請稍後再試」。滿額時同步與 ACK 照常；隔離項目仍計入額度，直到人工處理。
- **隔離項目的人工處理**：雲端管理頁列出隔離項目（ID、問卷、原因、時間，不顯示答案正文）與本機回報的原因，提供兩個動作，皆記錄操作者與時間：
  - **重新放回待收**（例如本機已補上缺少的定義版本）：狀態改回 `pending`，不重複計入額度；
  - **放棄**：刪除正文並以原子更新扣減額度，收據保留 `quarantined` 並註記「已放棄」，之後同 ID 重送一律拒絕。放棄會遺失該筆回覆，執行前再次確認。
- **整體資料庫監控**：雲端管理頁與心跳回應提供資料庫實際用量（含收據、索引、結果），達 400 MB 警示
  （Supabase 免費上限 500 MB；刪除資料不保證立即縮小占用）。

## 10. 雲端既有回覆的歷史版本

雲端現有回覆從未記錄題目版本。搬移時以問卷當下定義建立版本 1 作為**搬移基準版本**，封套標記 `definition_history = migration_baseline`，
表示「填答當時版本未知，以搬移時定義對應」，不宣稱是填答當時的版本。因第 2 節禁止改變已有回覆題目的語意，基準版本與既有答案的語意一致。

## 11. 切換與搬移（程式在本階段完成，執行需另行批准）

以問卷為單位切換，避免兩套計數混用：

1. 備份 Supabase，並實際還原到隔離資料庫驗證可用。
2. 對一份問卷設定 `owner_node` 與 `inbox_since`（需 `CLOUD_INBOX_ENABLED`）：此後該問卷新回覆只走收件匣，雲端不再為它排程分析；之前的回覆留在 `FeedbackSubmission`。
3. 搬移指令將該問卷 `inbox_since` 之前的回覆**分批**轉入收件匣（每批受第 9 節容量限制，滿額即暫停等待本機收回），
   保留來源 ID、原始提交資訊、`content_hash`、答案數、題目 uuid 與 `definition_history = migration_baseline`；
   同時建立收據並保存既有改善關聯的最後已知狀態。可重複執行，已轉者略過。
4. 本機收回後產生對帳報告：逐筆比對 ID、雜湊、答案數與題目關聯。
5. 經你確認後執行清理，在 `suppress_analysis_scheduling()` 內進行（該問卷已不在雲端排程，雙重保護）。清理當下**重新核對**雲端資料的內容雜湊與報告一致，只處理逐筆確認者：
   - **刪除**：該回覆的 `Answer` 列（答案正文與 `analysis_text`、`sentiment_score`、`analysis_version` 等衍生文字快取）；
     清空 `FeedbackSubmission` 的 `respondent_name`、`respondent_email`。
   - **保留**：`FeedbackSubmission` 列本身（`ImprovementDispatch.submission` 等關聯為 `SET_NULL`，刪除會失去關聯）、收據、改善項目與通知紀錄、
     雲端已套用的發布結果。
   - 不清空整張表；未通過核對者列入報告並保留。

## 12. 上線門檻

- 本階段不在正式網站開啟 `CLOUD_INBOX_ENABLED`、不設定任何問卷的 `owner_node`／`inbox_since`、不執行搬移。
- 真實顧客回覆接入收件匣之前，須完成加密子專案（瀏覽器以節點公鑰加密、雲端只存密文，含金鑰綁定、備份、輪替、復原與舊資料相容），
  或由你另行明確批准附風險說明的明文過渡方案。本規格不構成該批准。
- 明文收件匣**不符合**架構總覽「雲端只存密文」的設計，只作為隔離環境的原型。

## 13. 測試

- API：權杖驗證、撤銷、範圍限制（非本節點資源 404）、條件式更新與併發衝突、語意限制 422、`snapshot` 與銜接游標、
  變更分頁、封存與停用的同步、`POST surveys/` 冪等、410、提交順序與序號順序一致（兩個重疊交易不產生可見空洞）、snapshot 與同時進行的修改銜接不漏、
  未指派節點的問卷不受語意限制、ACK 逐筆狀態
  （含併發 ACK 只一次 `acked`、只扣減一次、`already_acked` 核對雜湊、`synced` 不降級、`not_found`）、隔離項目不再出現在批次、
  顧客同 ID 重送（相同內容回原結果、不同內容拒絕）、結果冪等、序號不倒退、定義版本較舊的結果不標為最新。
- 收件：原子計數在併發下不超收、單筆上限、未保存不顯示成功；雲端對 `owner_node` 問卷不排程分析；
  指派節點的問卷刪題只停用、不刪雲端舊答案；隔離項目放回與放棄（放棄扣減額度一次、之後同 ID 拒絕）。
- 本機問卷頁：node 模式下編輯經 API、離線時唯讀、本機副本不被頁面直接寫入。
- 本機同步單元測試（假客戶端）：錯誤分類與 `Retry-After`、`PendingAck` 只刪逐筆成功者、游標只在提交後推進、重新連結走 `snapshot`、
  缺少定義版本時不寫入不 ACK、原始 `submitted_at` 與提交資訊保留、`respondent_ref` 不對應本機 User、
  雲端刪除或停用題目時本機答案保留、定義變更觸發本機重算、`ResultUpload` 同交易建立與補排、還原後序號接續。
- `content_hash`：鍵順序不同的相同內容雜湊相同；字串內空白或 Unicode 形式不同則雜湊不同。
- **端對端（驗收）**：雲端以 cloud 模式在子程序啟動（獨立 SQLite），本機以 node 模式（另一個獨立 SQLite）經 API 交換；
  涵蓋問卷雙向修改與版本不一致、填答 → 收件匣 → 本機 → 分析 → 上傳 → 雲端顯示、定義變更後舊結果的標示，
  以及本機寫入後崩潰、ACK 遺失、部分成功、結果上傳回應遺失與重送。
- 併發：原子計數、併發 ACK、變更序列與條件式問卷更新另以隔離 PostgreSQL 驗證（沿用現有 CI job）。
- 搬移：以隔離資料庫驗證分批、可重複執行、基準版本標記、對帳報告、清理前重新核對、清理不觸發分析排程、只清除已確認資料且保留關聯。
