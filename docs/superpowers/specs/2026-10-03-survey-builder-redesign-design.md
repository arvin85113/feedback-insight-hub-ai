# 問卷建立工具改版：Google 表單式編輯、草稿與發布、自動資料型態

狀態：規格第四版（2026-10-03）。第三版經兩輪獨立審查；本版依產品決策改為「草稿可編輯、發布後固定」，
並依真實問卷反推加入選項分數與「不納入分析」選項。相關文件：[雲端同步](2026-10-01-cloud-sync-design.md)、
[真實問卷反推的題目與統計需求](../../survey-instrument-requirements.md)。

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
3. 草稿可任意修改；**發布後題目、選項、刻度、標籤與分數固定，即使尚無回覆也一樣**；只能停止／恢復收件、封存、複製成新草稿。
4. 每筆回覆記錄填答時的問卷版本（即發布版本）。
5. 複選答案可完整還原，選項含逗號也不影響；複選統計同時呈現選取人數、選取率（分母為該題填答人數）與勾選次數。
6. 標為「不納入分析」的選項不參與排序與檢定，每題報告有效 N、未作答數與「不納入分析」數。
7. 有序選項有明確分數（可從 0 或 1 起算）；刻度可從 0 起算。
8. TripAdvisor 的已發布分析結果不受影響；改版後重新模擬的飲料店問卷能完整跑過統計（七種方法皆有產出）、文字與發布流程。

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

- 新增 `Question.display`、`ordered`、`scale_min`、`scale_max`、`scale_min_label`、`scale_max_label`。
- `data_type` 仍存於資料庫，但**一律由伺服器以 `derive_data_type(kind, *, ordered)` 推得**，表單送來的 `data_type` 被忽略；
  模型 `save()`、編輯頁、定義同步、外部資料 mapping 驗證都經由這個函式。
- `enable_keyword_tracking` 只對文字題有意義；非文字題一律存 `False`，伺服器忽略送來的值。
- 統計方法沿用既有自動選擇：連續＝Welch t 檢定／ANOVA／Pearson；順序＝Mann-Whitney U／Kruskal-Wallis／Spearman；名目＝卡方。
  整數（離散）題維持描述統計（現有行為）；介面於「允許小數」旁註明「允許小數的數字題可做平均數比較與相關分析」，由設計者依題意決定。
- 每一點都需要文字的量表（例如「非常不同意…非常同意」）用「有序單選」；只標兩端的量表（例如 0–10 推薦意願）用「線性刻度」。
- 刻度題的填答頁顯示為可點選的按鈕，兩端顯示標籤；窄螢幕自動換行；未作答時不預選。不提供滑條（Funke 2016）。

## 2. 選項、分數與分析用的有序清單

### 2.1 選項代碼與分數

- 新增 `Question.choices`（JSON），取代 `options_text` 作為正本：
  `[{"code": "c1", "label": "非常滿意", "excluded": false, "score": 5}, ...]`。`options_text` 改為由 `choices` 產生的唯讀相容欄位，之後另案移除。
- `code` 由**伺服器**產生（`c1`、`c2`… 遞增、不重複使用）；題目代碼同樣由伺服器產生（`q1`、`q2`…）。
- `excluded`（介面文字「不納入分析」）：只用於單選與下拉選單，例如「不適用」「沒接觸過店員」「不知道」；
  有序題至少要有 2 個未排除的選項，名目題至少 1 個。
- `score`：有序題的未排除選項依清單順序（**由低到高**）由伺服器給分，從 `score_start`（0 或 1，預設 1）起算；名目題與排除選項為 `null`。
  `Question.score_start` 新增於題目。刻度題的分數就是刻度值本身，不存在 `choices`。
  本次分析只用分數決定順序；分數數值供後續構面計分使用。
- 新增 `Answer.choice_codes`（JSON，可空）：選擇題的正本（單選一個代碼、複選多個）；`Answer.value` 保留選項文字，只供顯示。

### 2.2 分析用的有序清單（唯一來源）

統計程式以「選項清單」判斷有效值並決定順序題的高低（`background_analysis` 的 `isin(field.options)`、`local_service.encode_ordinal`）。
新增 `analysis_levels(question) -> list[str]` 作為唯一來源，`Question.options` 與 `AnalysisField.options` 都由它產生：

- 刻度題：`[str(v) for v in range(scale_min, scale_max + 1)]`。
- 選擇題：未排除選項的文字，依分數（有序）或清單順序（名目）排列。
- 其他題型：空清單。

Worker 的分析經由 `background_analysis.descriptors` 以 `AnalysisField` 臨時重建未存檔的題目物件，再交給 `analyze_frame`。
因此 **levels 必須隨 `AnalysisField` 傳遞**：`descriptors` 與 `analyze_frame` 直接使用 `AnalysisField.options`，不從臨時物件重新推算。

選到排除選項的答案在分析中視為缺失，但與未作答分開計數：每題描述統計報告 `valid_n`、`missing_n`（未作答）與 `excluded_n`（不納入分析），
排除選項的人數另列於分布表並標示「不納入分析」。

### 2.3 必須調整的讀取端

| 讀取端 | 調整 |
|---|---|
| `AnswerInput`（`feedback/analysis_adapters.py`） | 選擇題以 `choice_codes` 對應選項文字；排除選項輸出為缺失並計數；複選題以**清單**傳給統計，不再以「, 」串接 |
| `analyze_frame`、`get_survey_pandas_stats`、`build_stats_payload`（`feedback/local_service.py`） | 只對複選題拆分且接受清單輸入，不再以「是否含逗號」判斷；複選分布改報選取人數、選取率與勾選次數；報告 `missing_n`／`excluded_n` |
| `background_analysis.descriptors`／有序對應（`feedback/background_analysis.py`） | 直接使用 `AnalysisField.options` |
| 填答表單 `SurveyFormBuilder`（`feedback/forms.py`） | 選項值為代碼；下拉選單用 `Select`；刻度依範圍產生按鈕並顯示兩端標籤；數字依「允許小數」產生整數或小數欄位 |
| 一般填答寫入 `submit_survey_payload`（`feedback/local_service.py`） | 寫入 `choice_codes` 與 `definition_version`；`value` 存顯示用文字 |
| 收件匣送出 `accept_submission`（`cloudapi/inbox.py`）與 `encode_answers`（`cloudapi/envelope.py`） | 以代碼建立封套答案 |
| AI Snapshot 指紋（`feedback/ai_snapshot_service.py`） | 指紋納入 `choices`（含分數與排除）、刻度範圍與標籤、`choice_codes` |
| 匯入器（`feedback/importing/service.py`） | 以 `analysis_levels` 驗證答案；與既有題目的相容比對改以新結構為準；寫入 `choice_codes` |
| 顧客回饋紀錄預覽、範本 | 讀 `Answer.value`，不受影響 |
| C1 定義 dict 與寫入（`cloudapi/definition.py`、`cloudapi/writes.py`） | 見第 4 節 |
| C2 封套與本機寫入（`cloudapi/envelope.py`、`cloudsync/inbox.py`） | 選擇題答案改傳代碼（單選字串、複選陣列），`payload_version=2`；本機寫入時產生 `choice_codes` 與顯示用 `value`；`payload_version=1` 回報隔離（原因 `payload_version`） |
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
- 已發布的問卷以唯讀方式顯示，提供「複製為新草稿」；由匯入流程建立的外部資料問卷同樣唯讀，定義由 mapping 管理。
- 新問卷網址改為隨機 8 個英數字；既有網址不變。

## 4. 生命週期與版本

| 狀態 | 判定 | 可做的事 |
|---|---|---|
| 草稿 | `published_at` 為空 | 編輯、預覽；**不接受填答**（顧客開啟顯示「問卷尚未開放」） |
| 已發布、收件中 | `published_at` 有值且 `accepts_responses` | 停止收件、封存、複製為新草稿 |
| 已發布、停止收件 | `is_active=False` | 恢復收件、封存、複製為新草稿 |
| 封存 | `archived_at` 有值（沿用） | 複製為新草稿 |

- 新增 `Survey.published_at`；`accepts_responses` 加上「已發布」條件。
- **草稿**：每次儲存遞增 `Survey.definition_version`，作為編輯衝突檢查（請求帶上載入時的版本，不符回 409）；草稿期間不保存 revision。
- **發布**：在交易內 `select_for_update` 鎖定問卷列，核對請求版本等於目前版本（拒絕發布未看過的修改），驗證完整性
  （至少一題；選擇題選項數符合第 2.1 節），設定 `published_at`，並以目前版本保存 `SurveyDefinitionRevision`。
- **已發布**：題目、選項、刻度、標籤、分數、問卷標題與說明的所有寫入路徑（編輯頁、行內編輯、C1 寫入）一律拒絕；
  不再需要第三版的語意鎖規則與「第一筆回答才建立版本」的規則。
- **填答**：只接受已發布且收件中的問卷；回覆記錄 `FeedbackSubmission.definition_version`（新增欄位，可空）。
- **複製為新草稿**：建立新問卷（新網址、`published_at` 為空、版本 0），題目與選項**保留原代碼**，以利日後跨問卷對應（例如前後測）；
  新舊問卷的分析各自獨立。
- **節點問卷（C1）**：收件節點在發布時決定——草稿可設定節點，發布時同時設定 `inbox_since`；**發布後不可變更**（`assign_survey_to_node` 與 `enable_survey_inbox` 對已發布問卷一律拒絕），因此不需要搬移既有回覆。定義 dict 加入 `published`；`change_definition` 只在草稿時接受題目變更，
  已發布時只接受收件狀態與封存的變更（拒絕原因 `published_locked`）。
- **C1 定義 dict 改為 `schema_version: 2`**：題目加入 `choices`（含 `excluded`、`score`）、`ordered`、`display`、`score_start`、
  `scale_min`、`scale_max`、`scale_min_label`、`scale_max_label`，保留 `enable_keyword_tracking`，移除 `options_text`。
  `validate_definition` 仍讀得懂 `schema_version` 1（舊 revision 不可修改），讀入時以固定、可重現的規則轉換：
  選項代碼依 `options_text` 行序產生 `c1..cn`；`ordered` 取自 `data_type`；有序題分數從 1 起算；無排除選項；刻度範圍依第 5 節第 2 步。
  雲端與本機用同一個轉換函式。新寫入只接受 v2；雲端同步原型目前未在正式環境開啟，節點須與雲端一起升級。

## 5. 舊資料：捨棄並重新模擬

正式資料庫目前的問卷：飲料店（指令產生的模擬資料）、2026 Q1 跨部門（早期測試，已封存）、`123`（空白測試，已封存）、
TripAdvisor（真實外部資料，答案只在本機 Parquet，Supabase 只有問卷與題目定義）。

1. **部署前（需另行授權）**：備份 Supabase 並實際還原到隔離資料庫；確認後以 `purge_survey` 指令刪除飲料店、2026 Q1 跨部門、`123` 三份問卷。
2. **Migration**：新增欄位；轉換所有題目定義——`options_text` 逐行產生 `choices`（不排除、有序題分數從 1 起算）；`ordered` 取自現有 `data_type`；
   刻度題的選項全是連續整數者設為刻度範圍（TripAdvisor 的六題評分 → 1–5），其他刻度設 1–5；`data_type` 依第 1 節重新推導；
   非文字題 `enable_keyword_tracking` 設為 `False`；**既有問卷的 `published_at` 設為建立時間**（全部視為已發布）。
3. **防呆**：migration 偵測到下列任一情況時中止並列出問卷，不轉換答案——仍有選擇題答案；或選項不是連續整數、且已有答案的刻度題。
4. **TripAdvisor**：只轉換題目定義；本機 Parquet 分析依 mapping 執行，已發布結果不變。轉換後題目定義與 mapping 的相容比對必須通過。
5. **重新模擬飲料店**：改寫 `seed_demo_beverage`，題目需涵蓋新功能並讓七種方法都有產出：門市（名目單選，4 家 → ANOVA、Kruskal-Wallis）、
   內用或外帶（名目單選，2 項 → Welch t、Mann-Whitney；與門市 → 卡方）、品項（複選）、
   整體滿意度（刻度 1–10，兩端標籤）、推薦意願（刻度 0–10）、等候時間感受（有序單選，含「不適用」排除選項）、
   等候分鐘數與消費金額（允許小數 → Pearson）、來店次數（整數）、改善建議（段落）。
   填答者名稱維持「飲料店模擬填答」標示；在本機或隔離環境驗證後，經授權寫入正式資料庫並重新發布分析（AI 段落使用 Gemini 需另行授權）。
   收件匣在正式網站開啟後，此模擬問卷以 `purge_survey` 清除，改建為指派節點的問卷再重新模擬（不搬移）。

### 5.1 問卷刪除政策與 `purge_survey`

- 一般介面的「刪除問卷」：草稿可直接刪除；已發布的問卷只能封存（不硬刪）。
- 管理指令 `purge_survey --survey <slug> [--confirm]` 只用於清除測試或模擬資料：預設 dry-run，列出將刪除的筆數；
  加 `--confirm` 才在一個交易內依序刪除：改善通知（`ImprovementNotice`，對改善紀錄為 `PROTECT`）→ 改善發送紀錄 → 改善紀錄
  （`ImprovementUpdate.survey` 為 `SET_NULL`，不先刪會留下孤兒）→ 問卷定義 revision（對問卷為 `PROTECT`）→ 分析工作、Snapshot、AI Stage、分析狀態 → 回覆與答案 → 問卷。
  指派給節點的問卷（`owner_node` 有值）拒絕執行。
- `seed_demo_beverage --reset` 改為呼叫同一個刪除程序，不再直接 `delete()`。

## 6. 外部資料（TripAdvisor、Amazon Beauty）

- 大型外部資料由本機 Parquet 依 mapping 檔分析；**mapping 格式與 Parquet 分析路徑本次不變**。
- mapping 中的題型與資料型態必須通過 `derive_data_type`；現有兩份 mapping 皆符合（刻度＝順序、整數＝離散、文字＝文字、單選＝名目）。
- 依 mapping 建立的資料庫題目採新結構並直接為已發布：選項產生代碼；整數選項的刻度設定範圍；`enable_keyword_tracking` 沿用 mapping（僅文字題可為真）。
- 小型資料匯入成 `FeedbackSubmission`／`Answer` 時（例如 Amazon Beauty「是否驗證購買」），匯入器以選項文字比對寫入 `choice_codes`，比對不到者列入匯入報告。

## 不在本次範圍

範本與標準量表內容、題組（矩陣）、反向計分與構面總分、分頁與說明區塊、依答案跳題、排序題、日期與時間、檔案上傳、滑條、
前後測與同人配對（後測為獨立區域或後測專用問卷）、新增統計方法（信度、NPS／前兩高分、離散與複選題檢定、事後比較、多重比較校正、配對檢定）、
多人即時協作與拖曳排序、關鍵字建議與自動分類。需求來源見[真實問卷反推的題目與統計需求](../../survey-instrument-requirements.md)。
雲端同步 C4（搬移）擱置：收件節點在發布時決定，不搬移既有回覆（見雲端同步規格第 10 節）。

## 測試

- 題型：七種題型以預設值新增成功；`derive_data_type` 所有組合；表單送來的 `data_type`、非文字題的文字分析開關被忽略。
- 選項與分數：伺服器產生代碼；有序題分數依順序從 0 或 1 起算、排除選項無分數；有序題少於 2 個未排除選項被拒；複選題不可設排除。
- 有序清單：刻度範圍（含 0 起算）產生正確清單；排除選項不在清單中；**Worker 路徑**上 1–10 刻度題與有序單選題的順序檢定都有產出。
- 缺失計數：未作答與選到排除選項分別計入 `missing_n`、`excluded_n`，兩者都不進入檢定。
- 複選統計：選取率以該題填答人數為分母（例：10 人都選 A、其中 5 人另選 B → A 選取率 100%、勾選次數占比 66.7%）；選項含逗號仍正確；只有複選題被拆分。
- 填答表單：選項值為代碼；下拉、刻度範圍與兩端標籤、整數與小數欄位正確；一般填答與收件匣都寫入 `choice_codes` 與版本。
- 生命週期：草稿不接受填答；草稿儲存遞增版本，舊版本儲存回 409 且畫面保留輸入；發布時版本不符被拒；
  發布後所有編輯路徑被拒（零回覆亦然）；停止／恢復收件、封存可用；複製為新草稿保留題目與選項代碼、版本為 0。
- 刪除：草稿可刪除、已發布只能封存；`purge_survey` dry-run 不寫入；`--confirm` 依序刪除且不留下孤兒改善紀錄；拒絕指派給節點的問卷；
  `seed_demo_beverage --reset` 在已有回覆與 revision 時仍可重建。
- 錯誤顯示：不合法輸入時卡片內顯示錯誤、輸入保留。
- C1：`schema_version` 1 的舊 revision 可讀入並轉換，雲端與本機結果相同；只接受 v2 寫入；已發布問卷的題目變更以 `published_locked` 拒絕；草稿指派節點後發布時設定 `inbox_since`；已發布問卷改指派節點或啟用收件匣被拒。
- C2：選擇題以代碼傳送，雜湊依新封套；`payload_version=1` 被隔離；本機寫入 `choice_codes` 與 `value`。
- 文字分析：選擇題、數字題、刻度題不進入關鍵字與情緒分析；簡答預設不納入。
- 外部資料：兩份 mapping 通過 `derive_data_type`；TripAdvisor 評分題遷移為 1–5 範圍且為已發布；題目與 mapping 相容比對通過；Amazon 小型匯入寫入 `choice_codes`。
- Migration：在隔離資料庫以正式資料備份（刪除舊問卷後）執行；仍有選擇題答案時中止；既有問卷皆為已發布；TripAdvisor 題目轉換正確。
- 重新模擬：新的 `seed_demo_beverage` 跑過統計（七種方法皆有產出）、文字與發布流程。
