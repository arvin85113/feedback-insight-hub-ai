# AI 輸出評估：讓 LLM 寫的數字可以被信任

綜合解析階段由 Gemini 撰寫營運摘要、綜合發現與改善草稿。文字裡的每個數字都必須能對應到它引用的 evidence
（[`feedback/ai_grounding.py`](../feedback/ai_grounding.py)）。本頁用固定的測試案例比較兩種讓模型寫數字的方式，
量出各自被驗證規則擋下的情況，作為調整 prompt 與驗證規則的依據，而不是等正式上線出錯才發現。

## 方法

- **測試案例**：兩份已發布問卷（飲料店 103 筆、TripAdvisor 201,295 筆）的綜合解析輸入，與正式流程完全相同
  （同一個 `build_stage_input`、相同的 evidence 短代碼），凍結在本機 `data/local/ai-eval/cases/`。
  案例含評論片段，不進 Git；本頁只放彙總數字。
- **判定**：使用正式流程的驗證函式（`_sanitize_provider_payload` → `validate_output`）。單一不合格的發現或改善草稿只捨棄該項，
  營運摘要不合格則整次判為失敗。
- **兩種寫法**：
  - **照抄數字＋事後查核（現行）**：模型照抄 evidence 的數字，查核程式擋下對不上的數字。
  - **引用代號，由程式填數字**：模型寫 `{E012}`（數值）或 `{E012.n}`（樣本數），程式依 evidence 填入實際值；
    句中用到的代號自動補進該項的 `evidence_refs`（上限 4 筆）。
- **重複**：每組 3 次，觀察穩定度而非單次結果。原始輸出存在本機結果檔，規則調整後可用 `--replay` 免費重新評分。

## 結果

<!-- ai-eval-results:start -->
模型：`gemini-3.6-flash`　每組重複 3 次　20261002-123858

| 問卷 | 寫法 | 次數 | 發布率 | 保留項目 | 捨棄項目 | 未對應證據的數字 | 無效代號 | 直接寫出的數字 | 含具體數字的項目 | 延遲（秒） | 輸入／輸出 token | 失敗原因 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| beverage-feedback | 照抄數字＋事後查核（現行） | 3 | 100% | 5.67 | 2 | 7 | 0 | 0 | 100% | 10.05 | 11805／1790.67 | — |
| beverage-feedback | 引用代號，由程式填數字 | 3 | 100% | 6.33 | 1 | 3 | 0 | 0 | 95% | 9.79 | 11885／1791.33 | — |
| tripadvisor-hotel-review-ratings | 照抄數字＋事後查核（現行） | 3 | 100% | 6 | 0 | 0 | 0 | 0 | 100% | 8.97 | 14115／1539 | — |
| tripadvisor-hotel-review-ratings | 引用代號，由程式填數字 | 3 | 100% | 6 | 0 | 0 | 0 | 6 | 100% | 7.81 | 14195／1598 | — |
<!-- ai-eval-results:end -->

欄位說明：「未對應證據的數字」為 3 次合計被擋下的數字個數；「直接寫出的數字」只對引用代號寫法有意義，表示模型沒有遵守「只寫代號」；
「含具體數字的項目」是保留下來的發現與草稿中，文字含數字的比例（可讀性指標）。

## 發現

1. **主要失敗模式不是「編造數字」，而是「數字取自沒有列入引用的 evidence」。** 第一輪（12 次）逐筆檢查被擋的數字，
   多數是 evidence 裡真實存在的值（例如 55.8449、77.4302），只是該項的 `evidence_refs` 沒有列出那筆 evidence；
   另一類（17、18、29 等）才是模型自行計算或未對應的數字。
2. **引用代號的優勢是「來源明確」。** 照抄寫法裡，一個對得上某筆 evidence 的數字可能只是巧合，程式不能替模型補引用；
   代號則明確指出數字來源，程式可以把來源補進引用清單。第二輪在飲料店問卷上，引用代號被擋的數字為 3、捨棄 1 項，
   照抄寫法為 7、捨棄 2 項；TripAdvisor 兩種寫法都沒有被擋的數字。
3. **引用代號剩下的失敗全部來自「代號數超過引用上限」。** 被擋的項目已引用 4 筆 evidence，句中又用了第 5 個代號，
   沒有空位可補引用。可行的下一步是在 prompt 加上「句中代號也計入 4 筆上限」，再以新的一輪驗證。
4. **模型不會 100% 遵守「只寫代號」。** TripAdvisor 的引用代號組仍有 6 個直接寫出的數字（多為題目標籤中的數字，例如評分範圍），
   因此即使改用代號，數字查核仍必須保留。

## 限制

- 每組只有 3 次、2 份問卷、1 個模型（`gemini-3.6-flash`），結果是方向性的，不是統計顯著的結論。
- 「含具體數字的項目」只衡量有沒有數字，不衡量文字品質；文字品質仍需人工抽查。
- 第一輪的原始輸出沒有保存，只保留彙總結論；第二輪起的結果都可以重播。

## 本機模型（Ollama）現況

開發機已安裝 Ollama（Windows 原生版），但服務未啟動、沒有下載任何模型，Docker 也未執行。評估工具保留了
`OllamaProvider` 介面（[`feedback/ai_eval/providers.py`](../feedback/ai_eval/providers.py)），目前呼叫會明確回報「本機模型尚未啟用」。
之後若要比較本機模型，下載模型並實作該介面的 `generate` 即可，測試案例與評分方式不需改動。

## 重現

```powershell
# 建立測試案例（不呼叫模型）
.\.venv\Scripts\python.exe manage.py run_ai_eval --surveys beverage-feedback,tripadvisor-hotel-review-ratings --dry-run
# 實際評估（付費 Gemini；2 份問卷 × 2 種寫法 × 3 次 = 12 次呼叫）
.\.venv\Scripts\python.exe manage.py run_ai_eval --surveys beverage-feedback,tripadvisor-hotel-review-ratings --repeats 3 --allow-paid-ai
# 規則調整後，用保存的原始輸出重新評分（不呼叫模型）
.\.venv\Scripts\python.exe manage.py run_ai_eval --surveys beverage-feedback --replay data/local/ai-eval/<結果檔>.jsonl
```

結果區塊（兩個 HTML 註解之間）由指令自動更新；其他段落為人工撰寫，數字變動時需一併檢查。
