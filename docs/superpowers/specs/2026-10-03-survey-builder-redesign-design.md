# 問卷建立工具改版：Google 表單式編輯、草稿與發布、自動資料型態

狀態：規格第四版之二（2026-10-03）。第三版經兩輪獨立審查；第四版依產品決策改為「草稿可編輯、發布後定義固定」，
並依真實問卷反推加入選項分數與「不納入分析」選項；4.1 納入第四版獨立審查的 12 項修正；4.2 依實作計畫審查新增第 7 節「流程與合約」，並調整白名單新鮮度、文字刻度轉換、初始版本與節點草稿刪除；4.2 第二輪補上受控寫入入口、填答版本核對與重送順序、節點分析版本、收件匣容量與本機副本辨識。
相關文件：[雲端同步](2026-10-01-cloud-sync-design.md)、[真實問卷反推的題目與統計需求](../../survey-instrument-requirements.md)。

## 目標

管理者像使用 Google 表單一樣建立問卷：只選題型、填題目與選項，**不需要理解統計術語**；系統依題型推得資料型態，
既有分析流程再依資料型態自動選擇統計方法。問卷在**草稿**階段編輯，**發布後定義固定**，每筆回覆都對應同一份已發布定義。

本次只做建立工具本身。範本、標準量表、構面計分、前後測與新增統計方法是後續階段，本版的資料結構需能承接它們
（題目與選項有穩定代碼、選項有分數、複製問卷保留代碼）。**現有七種統計方法全部保留，不因簡化而減少。**

## 背景（2026-10-03 審查發現）

- 新增「線性刻度」「整數」題時，介面預設的資料型態與模型規則衝突，伺服器拒絕且不顯示錯誤，輸入消失；行內編輯可組出同樣的不合法組合。
- 介面把刻度與評分描述為可算平均數的連續資料，違反「評分 ordinal 不得標成 continuous」。
- 「納入文字關鍵字分析」開關在選擇題時只是隱藏、值仍為開，選擇題的選項文字因而進入文字分析。
- 選項只是「一行一個文字」：複選答案以「, 」串接；統計程式 `analyze_frame` 只要看到逗號就整題切開。
- 刻度題無法設定範圍與標籤；中文題名轉不出代碼（`question`、`question-2`）；一般問卷沒有版本保護。
- 允許收件中修改題目，會產生題意改變後新舊答案被合併、填答者提交未曾看過的定義等問題（第三版的語意鎖仍無法完全防止）。

## 成功標準

1. 七種介面題型都能以預設值成功新增；任何驗證錯誤在該題卡片上就地顯示，輸入保留。
2. 管理者看不到「名目／順序／離散／連續」；資料型態完全由題型與最多一個開關決定，不可能組出不合法的組合。
3. 草稿可任意修改；**發布後題目、選項、刻度、標籤、分數、問卷標題與說明固定，即使尚無回覆也一樣**；
   只能變更第 4.2 節白名單內的設定（收件狀態、封存等）與複製成新草稿。
4. 每筆回覆記錄填答時的**發布版本**（`published_version`）；白名單變更不影響已開啟的表單；其中收件狀態、分類、感謝信、改善追蹤也不讓分析結果變成「非最新」（第 7.2 節）。
5. 複選答案可完整還原，選項含逗號也不影響；複選統計同時呈現選取人數、選取率（分母為該題填答人數）與勾選次數。
6. 標為「不納入分析」的選項不參與排序與檢定，每題報告有效 N、未作答數、「不納入分析」數與無效數，四者互不重疊。
7. 有序選項有明確分數（可從 0 或 1 起算）；刻度可從 0 起算。
8. TripAdvisor 的已發布分析結果（含 AI 段落）在改版後仍判定為最新；改版後重新模擬的飲料店問卷能完整跑過統計（七種方法皆有產出）、文字與發布流程。

## 1. 題型與自動資料型態

| 介面題型 | 儲存的 `kind` | 自動 `data_type` | 管理者額外設定 |
|---|---|---|---|
| 簡答 | `short_text` | `text` | 「納入文字分析」（預設關） |
| 段落 | `long_text` | `text` | 「納入文字分析」（預設開） |
| 選擇題（單選） | `single_choice`、`display=radio` | `nominal`；勾選「選項有高低順序」時 `ordinal` | 「選項有高低順序」；有序時「分數從 0／1 起算」；每個選項可設「不納入分析」 |
| 下拉選單 | `single_choice`、`display=dropdown` | 同上 | 同上 |
| 核取方塊（複選） | `multiple_choice` | `nominal`（多重回應） | 無 |
| 線性刻度 | `scale` | `ordinal` | 起點 0 或 1、終點 2–10（預設 1–5）；兩端標籤（選填） |
| 數字 | 未勾「允許小數」：`integer`；勾選：`decimal` | `discrete`／`continuous` | 「允許小數」 |

- 新增 `Question.display`、`ordered`、`score_start`、`scale_min`、`scale_max`、`scale_min_label`、`scale_max_label`。
- `data_type` 仍存於資料庫，但**一律由伺服器以 `derive_data_type(kind, *, ordered)` 推得**，表單送來的 `data_type` 被忽略；
  模型 `save()`、編輯頁、定義同步、外部資料 mapping 驗證都經由這個函式。
- `enable_keyword_tracking` 只對文字題有意義；非文字題一律存 `False`，伺服器忽略送來的值。
- 統計方法沿用既有自動選擇：連續＝Welch t 檢定／ANOVA／Pearson；順序＝Mann-Whitney U／Kruskal-Wallis／Spearman；名目＝卡方。
  整數（離散）題維持描述統計（現有行為）；介面於「允許小數」旁註明「允許小數的數字題可做平均數比較與相關分析」，由設計者依題意決定。
- 每一點都需要文字的量表（例如「非常不同意…非常同意」）用「有序單選」；只標兩端的量表（例如 0–10 推薦意願）用「線性刻度」。
- 刻度題的填答頁顯示為可點選的按鈕，兩端顯示標籤；窄螢幕自動換行；未作答時不預選。不提供滑條（Funke 2016）。

## 2. 選項、分數與分析用的有序清單

### 2.1 代碼、選項與分數

- 新增 `Question.choices`（JSON），取代 `options_text` 作為正本：
  `[{"code": "c1", "label": "非常滿意", "excluded": false, "score": 5}, ...]`。`options_text` 改為由 `choices` 產生的唯讀相容欄位，之後另案移除。
- **代碼由伺服器產生並以計數器保證不重複使用**：新增 `Survey.next_question_number` 與 `Question.next_choice_number`（皆從 1 起）。
  題目代碼 `q<n>`、選項代碼 `c<n>`，產生後計數器加一；刪除題目或選項不回收號碼。複製問卷時代碼與計數器一併複製。
  依 mapping 建立的題目沿用 mapping 的代碼（`feedback/importing/service.py` 的 `_question_codes`），不套用 `q<n>`。
- `excluded`（介面文字「不納入分析」）：只用於單選與下拉選單，例如「不適用」「沒接觸過店員」「不知道」；
  有序題至少要有 2 個未排除的選項，名目題至少 1 個。
- `score`：有序題的未排除選項依清單順序（**由低到高**）由伺服器給分，從 `score_start`（0 或 1，預設 1）起算；名目題與排除選項為 `null`。
  刻度題的分數就是刻度值本身，不存在 `choices`。本次分析只用分數決定順序；分數數值供後續構面計分使用。
- 新增 `Answer.choice_codes`（JSON，可空）：選擇題的正本（單選一個代碼、複選多個）；`Answer.value` 保留選項文字，只供顯示。

### 2.2 分析用的選項清單（唯一來源與傳遞方式）

- 新增 `analysis_levels(question) -> list[str]`：刻度題為 `[str(v) for v in range(scale_min, scale_max + 1)]`；
  選擇題為未排除選項的文字，依分數（有序）或清單順序（名目）排列；其他題型為空清單。
- 新增 `analysis_excluded(question) -> list[str]`：排除選項的文字；其他題型為空清單。
- `AnalysisField` 新增 `excluded: tuple[str, ...] = ()`；`AnalysisField.options` 等於 `analysis_levels`、`excluded` 等於 `analysis_excluded`。
- **`analyze_frame` 只透過題目物件的 `analysis_options` 與 `analysis_excluded_options` 兩個屬性取得清單**，ORM 與 Worker 兩條路徑相同：
  ORM 的 `Question` 由上述函式計算；`background_analysis.descriptors` 建立的臨時題目物件直接設定為 `AnalysisField.options`／`excluded`，
  不從臨時物件的其他欄位重新推算（臨時物件沒有 `choices` 與刻度欄位）。`encode_ordinal` 改讀 `analysis_options`。
- **排除答案的傳遞與計數**：adapter 對選到排除選項的答案**原樣輸出選項文字**（不轉成空值）。每題計數規則：
  `missing_n`＝空值或空字串；`excluded_n`＝值屬於排除清單；`valid_n`＝值屬於分析清單（選擇題與刻度題）或可轉成數字（數字題）或非空（文字題）；
  `invalid_n`＝其餘。四者互不重疊、總和為 `total_n`。`background_analysis` 的 `field_coverage` 依此規則加入 `excluded_n`，
  選擇題（含名目）一律以分析清單判定有效。檢定前，排除與無效的值視為缺失；描述統計的分布表另列排除選項的人數並標示「不納入分析」。
  Parquet 路徑沒有排除選項，`excluded_n` 固定為 0。

### 2.3 必須調整的程式

| 程式 | 調整 |
|---|---|
| `AnswerInput`（`feedback/analysis_adapters.py`） | 選擇題以 `choice_codes` 對應選項文字；排除選項原樣輸出；複選題以**清單**傳給統計，不再以「, 」串接 |
| `analyze_frame`、`get_survey_pandas_stats`、`build_stats_payload`（`feedback/local_service.py`） | 依第 2.2 節取清單與計數；只對複選題拆分且接受清單輸入，不再以「是否含逗號」判斷；複選分布改報選取人數、選取率與勾選次數 |
| `background_analysis.descriptors`、`field_coverage`（`feedback/background_analysis.py`） | 依第 2.2 節 |
| 統計頁範本 `templates/feedback/stats_overview.html` | 顯示複選的選取人數、選取率、勾選次數；顯示 `excluded_n` |
| AI Snapshot（`feedback/ai_snapshot_service.py`：分布組裝與 `_sanitize_distribution`、指紋） | 分布接受新的複選欄位；指紋規則見第 5 節第 4 步 |
| AI 數字核對（`feedback/ai_grounding.py`） | 複選百分比以「選取率」與「勾選次數占比」兩種名稱分別核對，不混用 |
| 填答表單 `SurveyFormBuilder`（`feedback/forms.py`） | 選項值為代碼；下拉選單用 `Select`；刻度依範圍產生按鈕並顯示兩端標籤；數字依「允許小數」產生整數或小數欄位 |
| 建立工具寫入端：`QuestionCreateForm`（`feedback/forms.py`）、`cloudapi/builder.py` 的 `add_question`／`update_question`、`templates/feedback/survey_builder.html`、`static/js/question-editor.js` | 改寫為第 3 節的卡片介面與新欄位；不再寫 `options_text` 與 `data_type` |
| 一般填答寫入 `submit_survey_payload`（`feedback/local_service.py`）與 `SurveyDetailView` | 只接受已發布且收件中的問卷；寫入 `choice_codes` 與 `definition_version`（＝發布版本）；`value` 存顯示用文字 |
| 收件匣送出 `accept_submission`（`cloudapi/inbox.py`）與 `encode_answers`（`cloudapi/envelope.py`） | 在問卷列鎖內再核對「已發布且收件中」；表單版本與 `published_version` 比對；以代碼建立封套答案 |
| C2 封套與本機寫入（`cloudapi/envelope.py`、`cloudsync/inbox.py`） | 封套新增 `answers_format: 2`（選擇題答案為代碼：單選字串、複選陣列），納入雜湊；本機寫入時產生 `choice_codes` 與顯示用 `value`。缺少或不是 2 者隔離，`QUARANTINE_REASONS` 新增 `answers_format`。`payload_version` 維持 1（保留給加密） |
| 結果新鮮度 `node_freshness`（`cloudapi/freshness.py`） | 「定義是否最新」改比對 `analysis_definition_version`（第 7.2 節）；`legacy_unmigrated` 與網站的「雲端既有 N 筆未納入分析」移除（不搬移，見雲端同步規格第 10 節） |
| C1 定義與寫入（`cloudapi/definition.py`、`cloudapi/writes.py`、`cloudapi/views.py`） | 見第 4 節；`check_semantic_lock` 與 `SEMANTIC_FIELDS` 移除；`Question.has_received_answer` 欄位保留（收件時照舊標記，不再用於鎖定） |
| 種子與測試資料：`seed_demo_beverage.py`、`seed_random_responses.py`、`seed_demo.py`、`scripts/seed_demo_data.py` | 以新欄位建立題目並寫入 `choice_codes`；建立後發布 |
| 匯入器（`feedback/importing/service.py`） | 以 `analysis_levels` 驗證答案；與既有題目的相容比對改以新結構為準；寫入 `choice_codes`；建立的問卷直接為已發布 |
| 顧客回饋紀錄預覽、範本 | 讀 `Answer.value`，不受影響 |
| C3 擷取（`cloudsync/capture.py`） | 不讀答案內容，不受影響 |

## 3. 編輯介面（Google 表單式）

- 每題一張卡片，直接在卡片上編輯。**以卡片上的「儲存」按鈕送出**，不做失焦自動儲存；同一時間只展開一張卡片，
  展開另一張時若有未儲存的修改，詢問「儲存／捨棄／繼續編輯」。離開頁面時有未儲存修改則提醒。
- 卡片內容：題目、說明（選填）、題型下拉、依題型出現的設定（選項清單與「不納入分析」／刻度範圍與兩端標籤／允許小數／
  選項有高低順序與分數起點／文字分析開關）、必填開關。
- 卡片動作：複製、刪除、上移、下移（不做拖曳）；移動後以 `aria-live` 宣告新位置。
- 選項清單：每列一個輸入框，可新增、刪除、上下移動；Enter 新增下一列；空白列不儲存；同一題選項文字不可重複。
- 驗證錯誤在卡片內就地顯示並保留輸入。**版本衝突（409）時保留輸入**，提示「此問卷已在其他視窗修改」，
  提供「重新載入（捨棄我的修改）」與「繼續編輯」兩個選項。
- 預覽：以 iframe 載入填答頁的預覽模式呈現，填答頁樣式不得影響管理頁（AGENTS 規則）。
- 已發布的問卷以唯讀方式顯示題目，仍可變更第 4.2 節白名單設定，並提供「複製為新草稿」；由匯入流程建立的外部資料問卷同樣唯讀，定義由 mapping 管理。
- 新問卷網址改為隨機 8 個英數字（網站建立與 C1 `create_node_survey` 都是）；既有網址不變。

## 4. 生命週期與版本

### 4.1 狀態與版本號

| 狀態 | 判定 | 可做的事 |
|---|---|---|
| 草稿 | `published_version` 為空 | 編輯、預覽、設定收件節點；**不接受填答**（顧客開啟顯示「問卷尚未開放」） |
| 已發布、收件中 | `published_version` 有值且 `accepts_responses` | 白名單變更、複製為新草稿 |
| 已發布、停止收件 | `is_active=False` | 同上 |
| 封存 | `archived_at` 有值（沿用） | 複製為新草稿 |

- 新增 `Survey.published_version`（可空）與 `Survey.published_at`；`accepts_responses` 加上「已發布」條件。
- **兩個版本號分工**：
  - `definition_version`：**任何**定義變更都遞增（草稿編輯、發布、發布後的白名單變更），**每個版本都保存 `SurveyDefinitionRevision`**
    （所有問卷皆然；儲存是手動按鈕，不會產生大量版本）。節點問卷另照 C1 寫入 `SurveyChange`，同步沿用現有機制。
  - `published_version`：發布時設為當下的 `definition_version`，之後不再改變。填答表單核對、`FeedbackSubmission.definition_version`、
    收件封套的 `definition_version` 都使用它，因此白名單變更不會讓已開啟的表單被拒；分析是否需要重算依第 7.2 節。
  - 新建立（含複製、匯入）的問卷即為版本 1，並保存 revision；不存在沒有 revision 的版本。
  - `analysis_definition_version`（新增欄位）：最後一次**影響分析**的定義版本——發布時設為發布版本，`analysis_enabled` 或 `archived_at` 變更時設為新的 `definition_version`，其他白名單變更不改。隨定義 dict 同步到節點；節點上傳結果時申報它，雲端以它判定節點結果的定義是否最新（第 7.2 節）。
- **草稿編輯**：請求帶上載入時的 `definition_version`，不符回 409。
- **發布**：在交易內 `select_for_update` 鎖定問卷列，核對請求版本等於目前版本（拒絕發布未看過的修改），驗證完整性
  （至少一題；選擇題選項數符合第 2.1 節），遞增 `definition_version` 並保存 revision，設定 `published_version` 與 `published_at`。
  入口：網站的「發布」按鈕；C1 為定義 dict 的 `published` 由 `false` 改為 `true`（只能單向）。

### 4.2 發布後的白名單

已發布問卷只接受以下問卷層級設定的變更（遞增 `definition_version`、不改 `published_version`）：
`is_active`（停止／恢復收件）、`archived_at`（封存）、`category`（含刪除分類時自動清空）、`analysis_enabled`、
`thank_you_email_enabled`、`improvement_tracking_enabled`。其他欄位與所有題目變更，經任何路徑（編輯頁、行內編輯、C1 寫入）
一律拒絕，C1 的拒絕原因為 `published_locked`。

### 4.3 收件節點（C1）

- 收件節點只能在**草稿**設定（`assign_survey_to_node`、管理指令 `assign_survey_node` 對已發布問卷一律拒絕）；節點建立的問卷（`create_node_survey`）為草稿。
- 有收件節點的問卷**只在 `CLOUD_INBOX_ENABLED` 開啟時能發布**，發布交易內同時設定 `inbox_since`；因此第一筆回覆起就走收件匣，
  不會有留在雲端 `FeedbackSubmission` 的回覆，不需要搬移。管理指令 `enable_survey_inbox` 不再需要，對已發布問卷拒絕執行。
- 發布後收件節點不可變更；需要改由其他節點收件時，複製成新問卷。
- **C1 定義 dict 改為 `schema_version: 2`**：問卷加入 `published`；題目加入 `code`、`choices`（含 `excluded`、`score`）、`ordered`、`display`、
  `score_start`、`scale_min`、`scale_max`、`scale_min_label`、`scale_max_label`，保留 `enable_keyword_tracking`，移除 `options_text`。
  `validate_definition` 仍讀得懂 `schema_version` 1（舊 revision 不可修改），讀入時以固定、可重現的規則轉換：
  選項代碼依 `options_text` 行序產生 `c1..cn`；`ordered` 取自 `data_type`；有序題分數從 1 起算；無排除選項；刻度範圍依第 5 節第 3 步；`published` 為真。
  雲端與本機用同一個轉換函式。新寫入只接受 v2；雲端同步原型目前未在正式環境開啟，節點須與雲端一起升級。

### 4.4 填答與複製

- **填答**：網站與收件匣都只接受已發布且收件中的問卷；回覆記錄 `FeedbackSubmission.definition_version`（新增欄位，可空）＝發布版本。
- **複製為新草稿**：建立新問卷（新網址、未發布、版本 1；雲端複製不帶收件節點，在本機複製時由雲端指派給該節點，仍為草稿可再變更），題目與選項**保留原代碼與計數器**，以利日後跨問卷對應（例如前後測）；
  新舊問卷的分析各自獨立。

## 5. 舊資料：捨棄並重新模擬

正式資料庫目前的問卷：飲料店（指令產生的模擬資料）、2026 Q1 跨部門（早期測試，已封存）、`123`（空白測試，已封存）、
TripAdvisor（真實外部資料，答案只在本機 Parquet，Supabase 只有問卷與題目定義）。

1. **先發布 `purge_survey`**：以獨立 PR 發布 `purge_survey` 指令（第 5.1 節，**不含 schema 變更**），讓它能在舊 schema 上執行。
2. **部署前清除（需另行授權）**：備份 Supabase 並實際還原到隔離資料庫；確認後以 `purge_survey` 刪除飲料店、2026 Q1 跨部門、`123` 三份問卷。
   開發資料庫與本機節點資料庫以同一指令清除測試與模擬問卷後再升級。
3. **Migration**（schema 與資料轉換分成兩個 migration；資料轉換只用 queryset `update()`／`bulk_update()`，**不呼叫 `save()`、不觸動 `updated_at`**）：
   新增欄位；轉換所有題目定義——`options_text` 逐行產生 `choices`（不排除、有序題分數從 1 起算）並依序設定代碼與計數器；
   `ordered` 取自現有 `data_type`；刻度題的選項全是連續整數者設為刻度範圍（TripAdvisor 的六題評分 → 1–5）；選項含文字者轉為有序單選（`single_choice`、`ordered`、`display=radio`，選項沿用）；沒有選項者設 1–5；
   `data_type` 依第 1 節重新推導；非文字題 `enable_keyword_tracking` 設為 `False`；
   **既有問卷全部視為已發布**：`published_version`＝目前 `definition_version`（為 0 者兩者都設為 1），`published_at`＝建立時間，並補存該版本的 revision。
   單選題的既有答案，若 `Answer.value` 恰好等於一個選項文字，寫入對應 `choice_codes`。
4. **防呆**：資料轉換偵測到下列任一情況時中止並列出問卷，不做部分轉換——複選題已有答案；單選題答案對不到唯一選項；選項不是連續整數、且已有答案的刻度題。
   中止時 schema migration 已套用（只新增欄位，舊程式不受影響），以 `purge_survey` 清除後重新執行 `migrate`。
5. **TripAdvisor 結果維持最新**：只轉換題目定義；本機 Parquet 分析依 mapping 執行。AI Snapshot 指紋改用 `analysis_levels` 取代 `options_text`，
   新欄位只在不同於預設值時（有排除選項、分數從 0 起算、有刻度標籤、`display` 為下拉）才加入指紋，
   因此 TripAdvisor 轉換前後的指紋完全相同，已發布的統計與 AI 段落仍判定為最新，不需重跑 Gemini。轉換後題目定義與 mapping 的相容比對必須通過。
6. **重新模擬飲料店**：改寫 `seed_demo_beverage`，題目需涵蓋新功能並讓七種方法都有產出：門市（名目單選，4 家 → ANOVA、Kruskal-Wallis）、
   內用或外帶（名目單選，2 項 → Welch t、Mann-Whitney；與門市 → 卡方）、品項（複選）、
   整體滿意度（刻度 1–10，兩端標籤）、推薦意願（刻度 0–10）、等候時間感受（有序單選，含「不適用」排除選項）、
   等候分鐘數與消費金額（允許小數 → Pearson）、來店次數（整數）、改善建議（段落）。
   填答者名稱維持「飲料店模擬填答」標示；在本機或隔離環境驗證後，經授權寫入正式資料庫並重新發布分析（AI 段落使用 Gemini 需另行授權）。
   收件匣在正式網站開啟後，此模擬問卷以 `purge_survey` 清除，改建為指派節點的問卷再重新模擬（不搬移）。

### 5.1 問卷刪除政策與 `purge_survey`

- 一般介面的「刪除問卷」：未指派節點的草稿可直接刪除（經同一個刪除程序）；已指派節點的草稿與已發布的問卷只能封存（不硬刪）。
- 管理指令 `purge_survey --survey <slug> [--confirm]` 只用於清除測試或模擬資料：預設 dry-run，列出將刪除的筆數；
  加 `--confirm` 才刪除。刪除順序、dry-run 計數方式與拒絕條件見第 7.4 節。
- `seed_demo_beverage --reset` 改為呼叫同一個刪除程序，不再直接 `delete()`。

## 6. 外部資料（TripAdvisor、Amazon Beauty）

- 大型外部資料由本機 Parquet 依 mapping 檔分析；**mapping 格式與 Parquet 分析路徑本次不變**。
- mapping 中的題型與資料型態必須通過 `derive_data_type`；現有兩份 mapping 皆符合（刻度＝順序、整數＝離散、文字＝文字、單選＝名目）。
- 依 mapping 建立的資料庫題目採新結構並直接為已發布：沿用 mapping 代碼；選項產生代碼；整數選項的刻度設定範圍；`enable_keyword_tracking` 沿用 mapping（僅文字題可為真）。
- 小型資料匯入成 `FeedbackSubmission`／`Answer` 時（例如 Amazon Beauty「是否驗證購買」），匯入器以選項文字比對寫入 `choice_codes`，比對不到者列入匯入報告。

## 7. 流程與合約

本節是各層共同遵守的規則；與前面章節衝突時以本節為準。雲端同步的 ACK、重送、隔離與結果上傳沿用[雲端同步規格](2026-10-01-cloud-sync-design.md)，本節只列改動。

### 7.1 狀態轉換（單一寫入路徑）

**問卷定義只能經由下列受控入口寫入**，全部位於 `cloudapi/writes.py`，共用同一個內部程序 `_locked_write`：在交易內鎖問卷列 → 核對版本與狀態 → `apply_definition` → `record_version`。

| 受控入口 | 用途 |
|---|---|
| `change_definition(survey_uuid, *, expected_version, definition)` | 草稿編輯、發布、白名單變更、封存 |
| `create_survey(definition)` | 雲端網站建立草稿、雲端複製 |
| `create_node_survey(node, definition)` | 本機建立草稿、本機複製（雲端指派給該節點） |
| `assign_survey_to_node(survey, node)` | 草稿指派節點 |
| `create_imported_survey(definition)` | 外部資料匯入建立問卷（版本 1、直接發布） |

網站的建立工具一律先編輯定義 dict，再交給 `feedback.survey_lifecycle`：雲端模式直接呼叫上表入口；本機模式經 API 送到雲端，成功後以回傳的定義更新本機副本。
**建立問卷的**種子指令（`seed_demo_beverage`、`seed_demo`、`seed_notification_test`、`scripts/seed_demo_data.py`）也經由 `feedback.survey_lifecycle` 建立與發布；問卷已存在時，未加 `--reset` 一律拒絕，不直接修改既有題目。只產生回覆的 `seed_random_responses` 不建立問卷，對既有已發布問卷經一般填答 service 送出（帶發布版本）。
唯一不經由上表的寫入是資料轉換 migration（第 5 節），它在單一交易內以歷史模型自行建立 revision。

| 動作 | 入口 | 前置條件 | 結果版本 | revision／SurveyChange |
|---|---|---|---|---|
| 建立草稿 | 雲端網頁、本機網頁（API `POST`）、種子指令（經 `survey_lifecycle`） | — | `definition_version=1`、未發布 | 建立／節點問卷另建 |
| 儲存題目或設定 | 雲端網頁、本機網頁（API `PUT`） | 草稿；版本相符（否則 409） | ＋1 | 建立／節點問卷另建 |
| 發布 | 同上（dict `published` 由假變真） | 草稿；版本相符；至少一題；有節點時收件匣已開 | ＋1、`published_version`＝新版本、有節點時設 `inbox_since` | 建立／節點問卷另建 |
| 白名單變更 | 同上；刪除分類時自動清空 | 已發布；只改白名單欄位（否則 `published_locked`） | ＋1、`published_version` 不變 | 建立／節點問卷另建 |
| 封存 | 刪除按鈕（已發布或已指派節點的草稿） | 版本相符 | ＋1 | 建立／節點問卷另建 |
| 刪除 | 刪除按鈕（未指派節點的草稿） | 鎖內重新確認仍為未指派節點的草稿、版本相符 | 問卷消失 | 依第 7.4 節刪除 |
| 複製為新草稿 | 雲端網頁、本機網頁（API `POST`） | 任何狀態 | 新問卷版本 1、未發布 | 建立（本機複製由雲端指派給該節點） |
| 指派節點 | 管理指令 `assign_survey_node` | 草稿 | ＋1 | 建立＋SurveyChange |
| 填答 | 網站表單（GET 顯示限制，POST 一律交給 service 判定）、收件匣（兩者的表單都帶發布版本） | 鎖內依序：①同一回覆 ID 已成功者：屬於同一問卷、同一人，且發布版本、答案與追蹤同意完全相同時，直接回傳先前結果（沿用重送合約，即使之後已停收或封存）；同 ID 但任何一項不同則拒絕；②已發布且收件中；③表單版本＝`published_version`，缺少或不符時拒絕、不建立回覆 | 不變 | 無 |
| 清除 | 管理指令 `purge_survey` | 未指派節點 | 問卷消失 | 依第 7.4 節 |

`enable_survey_inbox` 不再使用（發布時設定 `inbox_since`），對任何問卷都拒絕並提示改用發布。雲端 API 的 `POST`／`PUT` 只接受 `schema_version` 2；`schema_version` 1 只在讀取既有 revision 時自動轉換（第 4.3 節）。

### 7.2 分析重算對照

分析狀態的 `input_version`（輸入）與 `config_version`（設定）只在下表標示的情況遞增；其餘寫入必須以只更新變動欄位的方式儲存，不觸發重算。
寫入定義時只儲存實際變動的問卷欄位與題目，未變動的題目不得重新儲存。

| 變動 | 重算 |
|---|---|
| 新回覆、作廢回覆、答案變動 | 輸入 |
| 草稿的任何編輯、發布 | 不重算（草稿沒有回覆，不排程分析） |
| 白名單：`is_active`、`category`、`thank_you_email_enabled`、`improvement_tracking_enabled` | 不重算 |
| 白名單：`analysis_enabled`、`archived_at` | 設定（沿用現行規則）；並更新 `analysis_definition_version` |
| 關鍵字規則 | 設定（沿用現行規則） |
| 資料轉換 migration | 不重算（只用 queryset `update()`／`bulk_update()`，不送出模型訊號） |

AI Snapshot 指紋（`calculate_data_fingerprint`）屬於內部重用判斷，白名單變更可能改變它，但不影響網站顯示的「最新」判定；
網站以 `feedback/published_analysis.py` 的版本比對為準；節點問卷由雲端比對上傳結果申報的定義版本與 `analysis_definition_version`，因此 `analysis_enabled`／`archived_at` 變更後，舊結果（含延遲上傳者）不再標為最新，其他四個白名單欄位不影響。

### 7.3 回答合約

| 情況 | 儲存 | 分析 |
|---|---|---|
| 選擇題作答 | `Answer.choice_codes`（正本）＋ `Answer.value`（選項文字，複選以「, 」串接，只供顯示） | 依代碼對應目前選項文字 |
| 選到「不納入分析」選項 | 同上（代碼照存） | 計入 `excluded_n`，不進入檢定 |
| 未作答 | 不建立 `Answer`，或值為空字串 | 計入 `missing_n` |
| 代碼不在題目選項內 | 網站表單驗證拒絕；收件匣封套在本機以 `answers_format` 隔離 | 不會進入分析 |
| 數字題 | `Answer.value` 為數字字串 | 無法轉成數字者計入 `invalid_n` |
| 文字題 | `Answer.value` | 依「納入文字分析」 |

ORM 與 Worker 兩條統計路徑的母體一致：以該問卷完成且未作廢的 `FeedbackSubmission` 為列（沒有任何答案的回覆也算一列），各題缺值即未作答。
收件封套 `answers_format: 2` 中，選擇題答案為代碼（單選字串、複選陣列），其他題型與現行相同。

### 7.4 刪除關聯與 `purge_survey`

問卷相關的 `PROTECT` 與孤兒來源（以模型關聯盤點，三個 app）：

| 關聯 | 刪除方式 |
|---|---|
| `ImprovementNotice.improvement`（`PROTECT`）、`ImprovementUpdate.survey`（`SET_NULL`，會留下孤兒） | 先刪該問卷改善紀錄的通知，再刪改善紀錄（發送紀錄、狀態歷史隨之 CASCADE） |
| `ImportedSubmissionSource.batch`（`PROTECT`） | 先刪該問卷匯入批次的來源紀錄 |
| `SurveyAnalysisSource.active_external_version`（`PROTECT`，與 `ExternalDatasetVersion.source` 互相指向） | 先清空指標 |
| `cloudapi`：`InboxSubmission`、`SubmissionReceipt`、`PublishedResultRecord`、`SurveyChange`、`SurveyDefinitionRevision`（皆 `PROTECT`） | 逐一刪除；每刪除一筆 `InboxSubmission`（pending 與 quarantined 都占容量）就依其 `size_bytes` 退回該節點 `InboxCounter` 一次 |
| `cloudsync.PendingAck`（無外鍵，以回覆 uuid 對應） | 依該問卷回覆的 `idempotency_key` 刪除 |
| 其餘（題目、回覆、答案、匯入批次、關鍵字、Snapshot、AI Stage、分析狀態與來源、分析工作、同步狀態、結果上傳） | 刪除問卷時 CASCADE |
| 其他問卷的改善發送紀錄指向本問卷回覆、改善紀錄指向本問卷 AI Stage（`SET_NULL`） | 保留，指標清空 |

- 全部在一個交易內，先鎖問卷列，並在 `suppress_analysis_scheduling()` 內執行（刪除不得排程任何分析工作）。
- 收件匣的**全域鎖序**：問卷列（只有 purge）→ `SubmissionReceipt`（依 pk）→ `InboxSubmission`（依 pk）→ `InboxCounter`。abandon 已是此順序；ACK 與隔離（`quarantine_items`）改為先鎖該回覆的收據再鎖正文（原本先動正文）。purge **只對實際鎖定並刪除的正文**依節點加總後更新 `InboxCounter`，已被 ACK 或 abandon 先刪除的正文不再扣減。
- 雲端拒絕 `owner_node` 有值的問卷。本機以 `SurveySyncState` 辨識雲端副本：**本機套用雲端定義（`upsert_definition`）建立問卷時，在同一交易內建立 `SurveySyncState`**，因此建立或複製後尚未經過同步循環也能辨識；本機拒絕清除雲端副本，刪除按鈕改走雲端（第 7.1 節）。本機資料庫的測試資料改以整個重建處理（需授權）。
- **dry-run**：在同一個交易內實際執行刪除、記錄各模型筆數後回滾，列出的筆數即實際會刪除的量；不得以 `Collector.collect()` 預估（遇到 `PROTECT` 會直接失敗）。
- 不為了方便刪除而把任何 `PROTECT` 改成 `CASCADE`。

### 7.5 部署與轉換流程

1. 合併並部署 `purge_survey`（不含 schema 變更）。
2. 備份 Supabase，還原到隔離資料庫。在隔離資料庫上依序：`purge_survey` 三份舊問卷 → `migrate` → 執行唯讀檢查指令 `survey_conversion_report`，
   確認每份問卷皆已發布且有對應 revision、題目轉換結果、TripAdvisor 已發布統計／文字／AI 判定仍為最新。
3. 經授權，在正式資料庫執行 `purge_survey` 三份舊問卷，再部署改版（`build.sh` 套用 migration）。
4. 若資料轉換中止：部署失敗、舊程式繼續執行；schema migration（只新增欄位）已套用，不影響舊程式。依中止訊息處理後重新部署。
   若轉換後發現資料錯誤：以第 2 步的備份還原。
5. 經授權重新模擬飲料店並發布分析；Gemini 另行授權。
6. 本機節點資料庫（目前只有測試資料）經授權後整個重建，不做資料轉換。

## 不在本次範圍

範本與標準量表內容、題組（矩陣）、反向計分與構面總分、分頁與說明區塊、依答案跳題、排序題、日期與時間、檔案上傳、滑條、
前後測與同人配對（後測為獨立區域或後測專用問卷）、新增統計方法（信度、NPS／前兩高分、離散與複選題檢定、事後比較、多重比較校正、配對檢定）、
多人即時協作與拖曳排序、關鍵字建議與自動分類。需求來源見[真實問卷反推的題目與統計需求](../../survey-instrument-requirements.md)。
雲端同步 C4（搬移）擱置：收件節點在發布時決定，不搬移既有回覆（見雲端同步規格第 10 節）。

## 測試

- 題型：七種題型以預設值新增成功；`derive_data_type` 所有組合；表單送來的 `data_type`、非文字題的文字分析開關被忽略。
- 代碼：題目與選項代碼由計數器產生，刪除最後一個再新增不會重用；複製問卷保留代碼與計數器；mapping 題目沿用 mapping 代碼；新問卷網址為隨機 8 碼（含 C1 建立）。
- 選項與分數：有序題分數依順序從 0 或 1 起算、排除選項無分數；有序題少於 2 個未排除選項被拒；複選題不可設排除。
- 有序清單：刻度範圍（含 0 起算）產生正確清單；排除選項不在清單中；**ORM 與 Worker 兩條路徑**上 1–10 刻度題與有序單選題的順序檢定都有產出，且結果相同。
- 計數：同一題的 `missing_n`、`excluded_n`、`valid_n`、`invalid_n` 互不重疊、總和為 `total_n`；排除與無效值不進入檢定；Parquet 路徑 `excluded_n` 為 0。
- 複選統計：選取率以該題填答人數為分母（例：10 人都選 A、其中 5 人另選 B → A 選取率 100%、勾選次數占比 66.7%）；選項含逗號仍正確；只有複選題被拆分；
  統計頁與 AI Snapshot 讀得到新欄位；AI 數字核對分辨兩種百分比。
- 填答表單：選項值為代碼；下拉、刻度範圍與兩端標籤、整數與小數欄位正確；一般填答與收件匣都寫入 `choice_codes` 與發布版本。
- 生命週期：草稿不接受填答（網站與收件匣）；草稿儲存遞增版本並存 revision，舊版本儲存回 409 且畫面保留輸入；發布時版本不符被拒；
  發布後題目與標題變更被拒（零回覆亦然）；白名單變更遞增 `definition_version` 但不改 `published_version`；
  停止收件後再恢復，已開啟的表單仍可送出；白名單變更後分析結果仍為最新；刪除分類會清空已發布問卷的分類；複製為新草稿版本為 1、雲端複製不帶節點；分析開關與封存照現行規則標記需重算。
- 收件節點：草稿可指派節點；已發布問卷改指派或執行 `enable_survey_inbox` 被拒；收件匣未開啟時有節點的問卷不能發布；發布時設定 `inbox_since`；
  `accept_submission` 拒收未發布或停止收件的問卷。
- 刪除：未指派節點的草稿可刪除，已指派節點的草稿與已發布只能封存；`purge_survey` 在舊 schema 上可執行；dry-run 的計數與實際刪除一致且不留任何寫入；`--confirm` 依第 7.4 節刪除、不留下孤兒改善紀錄、收件匣容量正確退回；
  拒絕指派給節點的問卷；`seed_demo_beverage --reset` 在已有回覆、revision 與同步變更紀錄時仍可重建。
- 錯誤顯示：不合法輸入時卡片內顯示錯誤、輸入保留。
- C1：`schema_version` 1 的舊 revision 可讀入並轉換，雲端與本機結果相同；只接受 v2 寫入；草稿節點問卷出現在 snapshot 與變更序列；
  發布經 `published` 單向生效；已發布問卷的非白名單變更以 `published_locked` 拒絕；語意鎖相關的 422 不再出現。
- C2：封套帶 `answers_format: 2`，選擇題以代碼傳送，雜湊依新封套；缺少或不是 2 的封套以 `answers_format` 隔離；本機寫入 `choice_codes` 與 `value`。
- 文字分析：選擇題、數字題、刻度題不進入關鍵字與情緒分析；簡答預設不納入。
- 外部資料：兩份 mapping 通過 `derive_data_type`；TripAdvisor 評分題遷移為 1–5 範圍且為已發布；題目與 mapping 相容比對通過；Amazon 小型匯入寫入 `choice_codes`。
- Migration：在隔離資料庫以正式資料備份（刪除舊問卷後）執行；複選題有答案或單選答案對不到選項時中止；既有問卷皆為已發布且有對應 revision；
  不改變任何 `updated_at`；**轉換後 TripAdvisor 的已發布統計與 AI 段落仍判定為最新（`feedback/published_analysis.py` 的 `is_published_ai_stage_current` 等判定）、AI Snapshot 指紋不變**。
- 重新模擬：新的 `seed_demo_beverage` 跑過統計（七種方法皆有產出）、文字與發布流程。
- 流程與合約：第 7.1 節每一列至少一個測試（含本機經 API 的路徑）；第 7.2 節每一列驗證重算與不重算；第 7.3 節 ORM 與 Worker 路徑的計數、統計量與 p 值一致（含全未作答的回覆）；第 7.4 節每一種關聯都有 fixture；`survey_conversion_report` 為唯讀。
