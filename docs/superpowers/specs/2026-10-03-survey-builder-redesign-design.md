# 問卷建立工具改版：Google 表單式編輯與自動資料型態

狀態：規格第二版，待審閱（2026-10-03；第一版經獨立審查後修訂）。相關規格：[雲端同步](2026-10-01-cloud-sync-design.md)。

## 目標

系統只提供建立問卷的工具，題目與量表怎麼設計由問卷設計者決定。管理者像使用 Google 表單一樣建立問卷：
只選題型、填題目與選項，**不需要理解統計術語**；系統依題型推得資料型態，既有分析流程再依資料型態自動選擇統計方法。
已有人回答的題目不會被改壞。

## 背景（2026-10-03 審查發現）

- 新增「線性刻度」「整數」題時，介面預設的資料型態與模型規則衝突，伺服器拒絕且不顯示錯誤，輸入消失；行內編輯可組出同樣的不合法組合。
- 介面把刻度與評分描述為可算平均數的連續資料，違反「評分 ordinal 不得標成 continuous」。
- 「納入文字關鍵字分析」開關在選擇題時只是隱藏、值仍為開，選擇題的選項文字因而進入文字分析。
- 選項只是「一行一個文字」：改選項文字會讓新舊答案分成兩類；複選答案以「, 」串接；統計程式 `analyze_frame` 只要看到逗號就整題切開。
- 刻度題無法設定範圍與標籤；中文題名轉不出代碼（`question`、`question-2`）；一般問卷沒有版本保護。

## 成功標準

1. 七種介面題型都能以預設值成功新增；任何驗證錯誤在該題卡片上就地顯示，輸入保留。
2. 管理者看不到「名目／順序／離散／連續」；資料型態完全由題型與最多一個開關決定，不可能組出不合法的組合。
3. 修改選項文字後，新舊答案在統計中仍是同一選項；停用選項的既有答案仍計入；複選答案可完整還原，選項含逗號也不影響。
4. 已有回答的題目：可改題目文字、說明、選項文字、刻度標籤、必填、文字分析開關，可在**最後面**新增選項；
   不可改題型、不可移除選項（只能停用）、不可改有序題既有選項的相對順序、不可改刻度範圍；刪除題目一律停用。
5. 每筆回覆記錄填答當時的問卷版本。
6. TripAdvisor 的已發布分析結果不受影響；改版後重新模擬的飲料店問卷能完整跑過統計、文字與發布流程。

## 1. 題型與自動資料型態

| 介面題型 | 儲存的 `kind` | 自動 `data_type` | 管理者額外設定 |
|---|---|---|---|
| 簡答 | `short_text` | `text` | 「納入文字分析」開關（預設關） |
| 段落 | `long_text` | `text` | 「納入文字分析」開關（預設開） |
| 選擇題（單選） | `single_choice`、`display=radio` | `nominal`；勾選「選項有高低順序」時 `ordinal` | 「選項有高低順序」開關 |
| 下拉選單 | `single_choice`、`display=dropdown` | 同上 | 同上 |
| 核取方塊（複選） | `multiple_choice` | `nominal`（多重回應） | 無 |
| 線性刻度 | `scale` | `ordinal` | 起點 0 或 1、終點 2–10（預設 1–5）；每一點可選填文字標籤 |
| 數字 | 未勾「允許小數」：`integer`；勾選：`decimal` | `discrete`／`continuous` | 「允許小數」開關 |

- 新增 `Question.display`、`ordered`、`scale_min`、`scale_max`、`scale_labels`（JSON，鍵為刻度值字串、值為標籤，皆選填）。
- `data_type` 仍存於資料庫，但**一律由伺服器以 `derive_data_type(kind, *, ordered)` 推得**，表單送來的 `data_type` 被忽略；
  模型 `save()`、編輯頁、定義同步、外部資料 mapping 驗證都經由這個函式。
- `enable_keyword_tracking` 只對文字題有意義；非文字題一律存 `False`，伺服器忽略送來的值。
- 統計方法沿用既有自動選擇：連續＝t 檢定／ANOVA／Pearson；順序＝Mann-Whitney U／Kruskal-Wallis／Spearman；名目＝卡方；
  **整數（離散）題只提供描述統計**（現有行為），介面於整數題的設定處註明「整數題提供描述統計，不做推論檢定」。
- 刻度題的填答頁顯示為一排可點選的按鈕，有標籤的點顯示標籤。不提供滑條（研究顯示提高中途放棄率，見 Funke 2016）。
- 0–10 刻度可作為推薦意願題使用；本次不計算 NPS 分數。

## 2. 分析用的有序清單與選項代碼

### 2.1 分析用的有序清單（唯一來源）

統計程式以「選項清單」判斷有效值並決定順序題的高低（`background_analysis` 的 `isin(field.options)`、`local_service.encode_ordinal`）。
新增函式 `analysis_levels(question) -> list[str]` 作為唯一來源，`Question.options` 與 `AnalysisField.options` 都改由它產生：

- 刻度題：`[str(v) for v in range(scale_min, scale_max + 1)]`（例如 1–5 → `"1"…"5"`）。
- 選擇題：依 `choices` 陣列順序列出**所有**選項文字（含停用者），以便停用選項的既有答案仍有效、仍有正確順位。
- 其他題型：空清單。

填答頁只顯示啟用選項，由另一個屬性 `active_choices` 提供。

### 2.2 選項代碼

- 新增 `Question.choices`（JSON）：`[{"code": "c1", "label": "非常滿意", "active": true}, ...]`，取代 `options_text` 作為正本；
  `options_text` 改為由 `choices` 產生的唯讀相容欄位，之後另案移除。
- `code` 由**伺服器**產生（`c1`、`c2`… 遞增、不重複使用）；定義同步送來的新選項沒有代碼時由雲端補上，未知代碼一律拒絕。
- 移除選項改為 `active=false`。已有回答的有序題：既有選項的相對順序鎖定，新選項只能加在最後。
- 新增 `Answer.choice_codes`（JSON，可空）：選擇題的正本（單選一個代碼、複選多個）；`Answer.value` 保留填答當時的選項文字，只供顯示。

### 2.3 必須調整的讀取端

| 讀取端 | 調整 |
|---|---|
| `AnswerInput`（`feedback/analysis_adapters.py`） | 選擇題以 `choice_codes` 對應目前的選項文字；複選題以**清單**傳給統計，不再以「, 」串接 |
| `analyze_frame`、`get_survey_pandas_stats`、`build_stats_payload`（`feedback/local_service.py`） | 只對複選題拆分，且接受清單輸入；不再以「是否含逗號」判斷 |
| `background_analysis.descriptors`／有序對應 | 改讀 `analysis_levels` |
| AI Snapshot 指紋（`feedback/ai_snapshot_service.py`） | 指紋納入 `choices`、刻度範圍與標籤、`choice_codes` |
| 匯入器（`feedback/importing/service.py`） | 以 `analysis_levels`／`active_choices` 驗證答案；與既有題目的相容比對改以新結構為準；寫入 `choice_codes` |
| 顧客回饋紀錄預覽、範本 | 讀 `Answer.value`（顯示用），不受影響 |
| C1 定義 dict 與語意鎖（`cloudapi/definition.py`、`cloudapi/writes.py`） | 見第 4 節 |
| C2 封套與本機寫入（`cloudapi/envelope.py`、`cloudsync/inbox.py`） | 選擇題答案改傳代碼（單選字串、複選陣列），`payload_version=2`；本機寫入時由代碼產生 `choice_codes` 與顯示用 `value`；`payload_version=1` 回報隔離（原因 `payload_version`） |
| C3 擷取（`cloudsync/capture.py`） | 不讀答案內容，不受影響 |

## 3. 編輯介面（Google 表單式）

- 每題一張卡片，直接在卡片上編輯；按「完成」或卡片失去焦點時送出該題的修改。同一時間只展開一張卡片；
  展開另一張時，若目前卡片有未送出的修改，先送出，失敗則留在原卡片並顯示錯誤。
- 卡片內容：題目、說明（選填）、題型下拉、依題型出現的設定（選項清單／刻度範圍與每點標籤／允許小數／選項有高低順序／文字分析開關）、必填開關。
- 卡片動作：複製、刪除（已有回答時改為「停用」並說明原因）、排序。排序提供拖曳，並保留上下移動按鈕供鍵盤與觸控操作；
  移動後以 `aria-live` 宣告新位置。
- 選項清單：每列一個輸入框，可新增、刪除（已有回答時為停用）、排序；Enter 新增下一列；空白列不儲存；同一題選項文字不可重複。
- 驗證錯誤在卡片內就地顯示並保留輸入；版本衝突（409）時重新載入該卡片並顯示「版本不一致，請重新載入」。
- 已有回答的題目：題型與刻度範圍停用，有序題的既有選項不可拖曳，並顯示「此題已有回覆，請新增題目取代並停用舊題」。
- 預覽：以 iframe 載入填答頁的預覽模式呈現，填答頁樣式不得影響管理頁（AGENTS 規則）。
- 題目代碼改為伺服器產生的 `q1`、`q2`…（遞增、不重複使用）；新問卷網址改為隨機 8 個英數字；既有網址不變。
- 本次不提供題目或選項範本、題目寫法提示（題目設計由設計者決定；範本日後可作為獨立的付費服務）。

## 4. 版本保護（所有問卷）

- 語意鎖與不硬刪擴大到**所有問卷**。語意規則：`kind`、`data_type`（含 `ordered`）、刻度範圍不可改；選項代碼不可刪除只能停用；
  有序題既有代碼的相對順序不可改；選項文字、刻度標籤、`display`、必填、文字分析開關可改。
- **版本遞增時機**：問卷**尚無任何回覆**時，修改不遞增版本、不保存 revision（避免自動儲存產生大量版本）；
  有回覆之後，每次定義變更遞增 `Survey.definition_version` 並保存 `SurveyDefinitionRevision`。
  指派給節點的問卷維持 C1 行為（每次變更都遞增並寫入變更序列）。
- 一般填答流程送出時：先 `select_for_update` 鎖定問卷列，再寫入回覆與答案、標記 `has_received_answer`、
  記錄 `FeedbackSubmission.definition_version`（新增欄位，可空）。第一筆回答若讓問卷從「無回覆」變為「有回覆」，同一交易內把版本設為至少 1 並保存 revision。
- **C1 定義 dict 改為 `schema_version: 2`**：題目加入 `choices`、`ordered`、`display`、`scale_min`、`scale_max`、`scale_labels`，保留 `enable_keyword_tracking`，移除 `options_text`。
  `validate_definition` 同時接受 `schema_version` 1（舊 revision 不可修改），讀入時轉換為 v2 結構；語意鎖改用本節規則。

## 5. 舊資料：捨棄並重新模擬

正式資料庫目前的問卷：飲料店（指令產生的模擬資料）、2026 Q1 跨部門（早期測試，已封存）、`123`（空白測試，已封存）、
TripAdvisor（真實外部資料，答案只在本機 Parquet，Supabase 只有問卷與題目定義）。

1. **部署前（需另行授權）**：備份 Supabase 並實際還原到隔離資料庫；確認後刪除飲料店、2026 Q1 跨部門、`123` 三份問卷及其回覆、分析結果與改善紀錄。
2. **Migration**：新增欄位；轉換所有題目定義——`options_text` 逐行產生 `choices`；`ordered` 取自現有 `data_type`；
   刻度題的選項全是連續整數者設為刻度範圍（TripAdvisor 的六題評分 → 1–5），其他刻度設 1–5；`data_type` 依第 1 節重新推導；
   非文字題 `enable_keyword_tracking` 設為 `False`。
3. **防呆**：migration 偵測到仍有選擇題答案（`Answer` 屬於單選或複選題）時中止並列出問卷，不轉換答案。舊問卷應已於步驟 1 刪除。
4. **TripAdvisor**：只轉換題目定義；本機 Parquet 分析依 mapping 執行，已發布結果不變。轉換後題目定義與 mapping 的相容比對必須通過。
5. **重新模擬飲料店**：更新 `seed_demo_beverage` 使用新題型（滿意度題改為線性刻度 1–10、標籤由指令定義），
   填答者名稱維持「飲料店模擬填答」標示；在本機或隔離環境驗證後，經授權寫入正式資料庫並重新發布分析（AI 段落使用 Gemini 需另行授權）。

## 6. 外部資料（TripAdvisor、Amazon Beauty）

- 大型外部資料由本機 Parquet 依 mapping 檔分析；**mapping 格式與 Parquet 分析路徑本次不變**。
- mapping 中的題型與資料型態必須通過 `derive_data_type`；現有兩份 mapping 皆符合（刻度＝順序、整數＝離散、文字＝文字、單選＝名目）。
- 依 mapping 建立的資料庫題目採新結構：選項產生代碼；整數選項的刻度設定範圍；`enable_keyword_tracking` 沿用 mapping（僅文字題可為真）。
- 小型資料匯入成 `FeedbackSubmission`／`Answer` 時（例如 Amazon Beauty「是否驗證購買」），匯入器以選項文字比對寫入 `choice_codes`，比對不到者列入匯入報告。

## 不在本次範圍

分頁與段落、依答案跳題、矩陣題、日期與時間、檔案上傳、滑條、題目與選項範本（日後可作為付費服務）、題目寫法提示、
NPS 分數計算、整數題的推論檢定、關鍵字建議與自動分類（關鍵字規則維持現狀）。雲端同步 C4（搬移）在本改版完成後依新格式修訂。

## 測試

- 題型：七種題型以預設值新增成功；`derive_data_type` 所有組合；表單送來的 `data_type`、非文字題的文字分析開關被忽略。
- 有序清單：刻度範圍產生正確清單；停用選項的既有答案仍有效且順位正確；改選項文字後新舊答案統計合併。
- 統計輸入：選項含逗號的單選與複選題統計正確；只有複選題被拆分。
- 錯誤顯示：不合法輸入時卡片內顯示錯誤、輸入保留；409 時重新載入並提示。
- 版本保護：一般問卷已有回答時改題型、移除選項、改有序順序、改刻度範圍被拒；尚無回覆時修改不產生 revision；
  送出時鎖問卷列並記錄版本；第一筆回答與語意修改同時發生時序列化（PostgreSQL，沿用 C1 測試方式）。
- C1：`schema_version` 1 的舊 revision 可讀入並轉換；雲端補發選項代碼、拒絕未知代碼。
- C2：選擇題以代碼傳送，雜湊依新封套；`payload_version=1` 被隔離；本機寫入 `choice_codes` 與 `value`。
- 文字分析：選擇題、數字題、刻度題不進入關鍵字與情緒分析；簡答預設不納入。
- 外部資料：兩份 mapping 通過 `derive_data_type`；TripAdvisor 評分題遷移為 1–5 範圍；題目與 mapping 相容比對通過；Amazon 小型匯入寫入 `choice_codes`。
- Migration：在隔離資料庫以正式資料備份（刪除舊問卷後）執行；仍有選擇題答案時中止；TripAdvisor 題目轉換正確。
- 重新模擬：新的 `seed_demo_beverage` 產生的資料能完整跑過統計、文字與發布流程。
