# feedback/data 使用指南

本資料夾存放文字分析與分類用的字典檔。  
建議維護順序：`stopwords -> synonyms -> keyword_category_map -> sentiment words`。

---

## 1) 檔案用途

- `stopwords.txt`
  - 停用詞清單（會被過濾，不參與關鍵詞統計）。
  - 適合放語氣詞、功能詞、碎詞。

- `synonyms.json`
  - 同義詞歸一（`原詞 -> 代表 keyword`）。
  - 這裡只做「詞彙收斂」，不要放 category 名稱。

- `keyword_category_map.json`
  - 分類對照（`keyword -> category`），同步到 `KeywordCategory` 資料表。
  - 含 `survey` 與 `threshold` 設定。

- `positive_words.txt`
  - 正向情緒詞典。

- `negative_words.txt`
  - 負向情緒詞典。

- `negation_words.txt`
  - 否定詞（例如：不、沒有、不太）。

- `intensifiers.json`
  - 程度詞權重（例如：非常=1.6、很=1.2）。

---

## 2) 維護原則

1. **先收斂再分類**  
   先改 `synonyms.json`，再改 `keyword_category_map.json`。

2. **避免混層**  
   - `synonyms.json` value 應是 keyword，不是 category。
   - category 只放在 `keyword_category_map.json`。

3. **JSON 要合法**  
   - 不可有尾逗號
   - key 不可重複

4. **高頻詞優先**  
   先處理高頻未分類詞，再處理長尾詞。

---

## 3) 常用指令流程

### A. 同步分類對照到資料庫

```bash
python manage.py sync_keyword_categories --dry-run
python manage.py sync_keyword_categories
```

### B. 重算既有文字分析

```bash
python manage.py rebuild_text_analysis --survey beverage-feedback
```

### C. 檢查未分類高頻詞

```bash
python manage.py top_uncategorized_keywords --survey beverage-feedback --limit 30 --min-count 1
```

### D. 重新建立飲料店示範問卷（讀取 keyword_category_map）

```bash
python manage.py seed_demo_beverage
```

---

## 4) 推薦實務節奏（每次調整）

1. 修改字典檔（`stopwords/synonyms/keyword_category_map`）
2. `sync_keyword_categories`
3. `rebuild_text_analysis`
4. `top_uncategorized_keywords` 驗證
5. 進後台檢查文字雲與分類情緒分布

---

## 5) 常見問題

- 問：改了 JSON 為什麼頁面沒變？  
  答：需要執行 `rebuild_text_analysis`，舊資料才會重算。

- 問：新增 keyword/category 要不要 migration？  
  答：不用。這是資料列更新，不是 schema 變更。

- 問：何時才需要 migration？  
  答：只有新增/修改資料表欄位時（model schema 變更）才需要。
