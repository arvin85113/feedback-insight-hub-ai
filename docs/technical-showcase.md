# Feedback Insight Hub — 技術展示文件

> 本文件供 NotebookLM 研究用途，涵蓋各核心功能背後的實際程式碼、CSS 設計模式與 JS 互動模式。
> 生成日期：2026-05-10

---

## 1. 平台定位與技術選型

**Feedback Insight Hub** 是一套 B2B 登入制問卷管理平台，目標是把「收集回饋 → 統計分析 → 改善追蹤 → 通知回推」串成一個完整工作流。

| 層次 | 技術選擇 | 原因 |
|---|---|---|
| Web 框架 | Django 6.0.3 | ORM 整合、auth、template 一體化，適合快速搭建管理後台 |
| 微服務 | Flask 3.1.2 | 分離 feedback domain 邏輯，未來可獨立擴展 |
| 統計分析 | pandas + scipy | 靈活的 DataFrame 操作 + 成熟的統計函式 |
| 文字分析 | 字典驅動 pipeline | 可控、可快取、可版本化，不依賴第三方 NLP 服務 |
| 前端 | Django templates + 純手寫 CSS | 無框架依賴，完整掌控設計語言 |
| 資料庫 | SQLite（本機）/ Supabase PostgreSQL（生產） | Supabase 提供 PostgreSQL 相容雲端 DB，無需自建伺服器 |

---

## 2. 服務架構：Circuit-Breaker 設計模式

### 雙層服務設計

```
Browser → Django (port 8000)
               │
               ├── feedback/service_client.py
               │        │
               │        ├── Flask microservice（若 FEEDBACK_SERVICE_URL 設定且健康）
               │        │        └── /api/stats, /api/dashboard, ...
               │        │
               │        └── feedback/local_service.py（fallback，目前主要路徑）
               │                 └── Django ORM → Shared DB
               │
               └── Django ORM → Shared DB（直接查詢）
```

### 關鍵實作：`feedback/service_client.py`

```python
class FeedbackServiceClient:
    def __init__(self):
        self.base_url = os.getenv("FEEDBACK_SERVICE_URL", "").rstrip("/")
        self.connect_timeout = float(os.getenv("FEEDBACK_SERVICE_CONNECT_TIMEOUT", "0.35"))
        self.read_timeout    = float(os.getenv("FEEDBACK_SERVICE_READ_TIMEOUT", "0.8"))
        self.failure_cooldown = float(os.getenv("FEEDBACK_SERVICE_FAILURE_COOLDOWN", "30"))
        self._disabled_until = 0.0          # 時間戳：禁用截止時間
        self._session = requests.Session()

    def _service_available(self):
        return bool(self.base_url) and time.monotonic() >= self._disabled_until

    def _mark_failure(self):
        # 記錄失敗後，停止重試 30 秒（可調整）
        self._disabled_until = time.monotonic() + self.failure_cooldown

    def get_stats(self, slug):
        if self._service_available():
            try:
                return self._get("/api/stats", params={"survey": slug})
            except requests.RequestException:
                self._mark_failure()          # 失敗 → 進入冷卻
        return local_service.get_stats_payload(slug)   # 自動 fallback
```

**設計重點：**
- `_disabled_until` 使用單調時鐘，避免重試風暴（retry storm）
- 冷卻期間所有請求直接走 Django fallback，不嘗試 Flask
- `FEEDBACK_SERVICE_URL` 未設定時，直接略過 Flask，無網路開銷

---

## 3. 角色與授權：Django Mixin 設計

### 兩種使用者角色

```python
# feedback/views.py

class ManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """管理者專用頁面：須登入且 is_manager == True"""
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_manager

class CustomerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """顧客專用頁面：須登入且非管理者"""
    def test_func(self):
        return self.request.user.is_authenticated and not self.request.user.is_manager

class DashboardBaseMixin(ManagerRequiredMixin):
    """管理者 workspace 基底：提供 sidebar nav 與通用 context"""
    dashboard_nav = [
        ("feedback:dashboard",      "營運總覽", "grid"),
        ("feedback:survey-manager", "問卷管理", "clipboard"),
        ("feedback:stats-overview", "統計分析", "chart"),
        ("feedback:text-analysis",  "文字洞察", "message"),
        ("feedback:improvement-list","改善追蹤", "wrench"),
        ("feedback:notice-center",  "通知中心", "send"),
    ]
```

**設計重點：**
- 利用 Django `UserPassesTestMixin` 統一攔截，不需在每個 view 手動寫判斷
- `DashboardBaseMixin` 集中管理 sidebar 結構，新增功能只需在 `dashboard_nav` 加一筆

---

## 4. 問卷填答流程：逐步式問卷

### 填答頁存取控制（`SurveyDetailView.dispatch`）

```python
def dispatch(self, request, *args, **kwargs):
    survey = get_object_or_404(Survey, slug=kwargs["slug"])
    # 1. 未登入 → redirect to login
    if not request.user.is_authenticated:
        return redirect(f"/accounts/login/?next={request.path}")
    # 2. 問卷未開放
    if not survey.is_active:
        return render(request, "feedback/survey_detail.html", {
            "survey": survey,
            "survey_notice": "此問卷目前未開放填答。"
        })
    # 3. 問卷無題目
    if not survey.questions.exists():
        return render(request, "feedback/survey_detail.html", {
            "survey": survey,
            "survey_notice": "此問卷尚未建立任何題目。"
        })
    # 4. 顧客已填答（管理者豁免）
    if not request.user.is_manager:
        if FeedbackSubmission.objects.filter(
            survey=survey, respondent_email=request.user.email
        ).exists():
            return render(request, "feedback/survey_detail.html", {
                "survey": survey,
                "survey_notice": "您已完成此問卷填答，感謝您的回饋！"
            })
    return super().dispatch(request, *args, **kwargs)
```

### 步驟控制（Template 端 JavaScript）

填答頁使用一個隱藏式多步驟卡片，JavaScript 控制「目前顯示哪一步」：

```javascript
// templates/feedback/survey_detail.html（簡化版）
const cards = document.querySelectorAll('.survey-step-card');
let current = 0;

function showStep(idx) {
    cards.forEach((card, i) => card.hidden = (i !== idx));
    updateProgress(idx);
}

// 下一步按鈕
document.querySelectorAll('[data-next]').forEach(btn =>
    btn.addEventListener('click', () => showStep(current + 1))
);

// 後端驗證失敗時導航到第一個有錯誤的步驟
const firstError = Array.from(cards)
    .findIndex(card => card.dataset.hasError === 'true');
if (firstError >= 0) showStep(firstError);
```

**設計重點：**
- `<form novalidate>` 關閉 HTML5 原生驗證，避免隱藏步驟的必填欄位阻擋 submit
- 伺服器端 Django 驗證仍完整執行，`data-has-error` 屬性由 template 設定
- 步驟 0 為唯讀填答者資訊（姓名、Email 從 `request.user` 讀取，不可修改）

---

## 5. 統計分析引擎：Pandas + SciPy 自動推論

### 資料型態模型（Analysis-purpose）

| 型態 | 定義 | 在統計中的角色 |
|---|---|---|
| `continuous` | 有意義數量（評分、金額、時間）| 描述統計 + 可作為 DV（t-test / ANOVA / Pearson）|
| `discrete` | 計次、代碼類數字 | 描述統計 only |
| `nominal` | 無序類別（部門、地區）| 頻率分布 + 可作為 IV |
| `ordinal` | 有序類別（非常滿意…非常不滿意）| 頻率分布 + 非母數秩次檢定 |
| `text` | 開放式文字 | 交由文字洞察 pipeline 處理 |

### 核心推論邏輯（`feedback/local_service.py`）

```python
def get_survey_pandas_stats(survey):
    # 一次查詢：把所有回答壓成 DataFrame
    answer_rows = Answer.objects.filter(question__survey=survey) \
                        .values("submission_id", "question_id", "value")
    records = {}
    for row in answer_rows:
        records.setdefault(row["submission_id"], {})[f"Q_{row['question_id']}"] = row["value"]
    df = pd.DataFrame(list(records.values()))

    # Nominal IV × Continuous DV → Welch t-test or One-way ANOVA
    for iv_col in nominal_cols:
        for dv_col in continuous_cols:
            working = pd.DataFrame({
                "iv": nominal_columns[iv_col],
                "dv": numeric_columns[dv_col]
            }).dropna()
            valid_groups = working["iv"].value_counts()
            valid_groups = valid_groups[valid_groups >= 2].index.tolist()

            if len(valid_groups) == 2:
                stat, p = stats.ttest_ind(group_a, group_b, equal_var=False)  # Welch
                effect_size = cohens_d(group_a, group_b)
            elif 3 <= len(valid_groups) <= 5:
                stat, p = stats.f_oneway(*groups_data)                         # ANOVA
                effect_size = eta_squared(groups_data)
            else:
                result["skipped_reason"] = "有效分組不足 2 組 / 超過 5 組"
```

### 支援的推論方法矩陣

| IV 型態 | DV 型態 | 方法 | 效果量 |
|---|---|---|---|
| Nominal（2 組）| Continuous | Welch t-test | Cohen's d |
| Nominal（3–5 組）| Continuous | One-way ANOVA | η²（eta squared）|
| Nominal | Nominal | Chi-square | Cramér's V |
| Nominal（2 組）| Ordinal | Mann-Whitney U | — |
| Nominal（3–5 組）| Ordinal | Kruskal-Wallis | — |
| Continuous | Continuous | Pearson 相關 | — |
| 含 Ordinal | Continuous | Spearman 相關 | — |

每一組合在執行前都先檢查資料條件，不符合時回傳 `skipped_reason` 而非靜默略過。

---

## 6. 文字洞察 Pipeline

### Pipeline 架構（`feedback/text_pipeline.py`）

```
原始文字輸入
    → tokenize_feedback()
        → _jieba_tokenize()（有安裝 jieba）
          or _regex_tokenize()（fallback：正規表達式）
        → 同義詞正規化（synonyms.json）
        → 停用詞過濾（stopwords.txt）
        → 長度過濾（< 2 字元捨棄）
    → 輸出：normalized token list

    → estimate_sentiment_score()
        → 比對正向詞典（positive_words.txt）
        → 比對負向詞典（negative_words.txt）
        → 否定詞偵測（negation_words.txt）前兩個位置
        → 強度詞加權（intensifiers.json，如「非常」× 1.6）
        → 正規化到 [-1.0, 1.0]
```

### 情緒評分邏輯

```python
def estimate_sentiment_score(text):
    for idx, token in enumerate(tokens):
        if token in positive_words:
            base = 1.0
        elif token in negative_words:
            base = -1.0
        else:
            continue

        # 往前看 2 個 token 找否定詞
        window = tokens[max(0, idx-2): idx]
        negation_count = sum(1 for t in window if t in negation_words)
        intensity_weight = 1.0
        for t in window:
            intensity_weight *= intensifiers.get(t, 1.0)

        if negation_count % 2 == 1:   # 奇數否定 → 反轉極性
            base *= -1.0
        score_sum += base * intensity_weight

    return round(max(-1.0, min(1.0, score_sum / sentiment_hits)), 3)
```

### 快取機制（`Answer` 模型欄位）

| 欄位 | 說明 |
|---|---|
| `analysis_text` | 正規化後的 token 字串（空格分隔） |
| `sentiment_score` | 情緒分數 -1.0 ~ +1.0 |
| `analysis_version` | pipeline 版本（目前 `"v2"`） |

版本不符時，管理指令 `rebuild_text_analysis` 重算並更新快取，避免每次請求都重新計算。

---

## 7. 統計分析頁面：三 Tab 工作台

### View 層的 Context 準備

```python
# feedback/views.py — StatsOverviewView.get_context_data

inferential = payload.get("inferential_analysis", [])
context["available_tests_count"] = sum(1 for r in inferential if not r.get("skipped_reason"))
context["skipped_tests_count"]   = sum(1 for r in inferential if r.get("skipped_reason"))

# 推論結果預先依方法族群分組，避免 template 中的複雜邏輯
_group_defs = [
    {"key": "mean_comparison",       "badge": "平均數比較",  "badge_class": "method-mean",
     "title": "名目分組 × 連續結果",
     "desc": "2 組跑 Welch t-test，3-5 組跑單因子 ANOVA，並附上效果量。",
     "families": ("mean_comparison",)},
    {"key": "categorical_association","badge": "類別關聯",   "badge_class": "method-category",
     "title": "名目 × 名目",
     "desc": "單選名目題之間跑卡方檢定；多選題只做多重回應頻率，不當分組。",
     "families": ("categorical_association",)},
    {"key": "rank_correlation",       "badge": "順序 / 相關","badge_class": "method-rank",
     "title": "排序與關聯",
     "desc": "名目 × 順序跑非母數檢定；連續 × 連續跑 Pearson，涉及順序資料跑 Spearman。",
     "families": ("nonparametric_rank", "correlation")},
]
context["inference_groups"] = [
    {**g, "results": [r for r in inferential if r.get("analysis_family") in g["families"]]}
    for g in _group_defs
]
```

### Tab 切換 JavaScript（`stats_overview.html`）

```javascript
(() => {
    const tabs   = Array.from(document.querySelectorAll('.analytics-tab[data-tab]'));
    const panels = Array.from(document.querySelectorAll('[data-tab-panel]'));

    function activate(tabId, scroll) {
        const id = tabs.some(t => t.dataset.tab === tabId) ? tabId : tabs[0].dataset.tab;

        tabs.forEach(t => {
            t.classList.toggle('analytics-tab-active', t.dataset.tab === id);
            t.setAttribute('aria-selected', t.dataset.tab === id ? 'true' : 'false');
        });
        panels.forEach(p => { p.hidden = p.dataset.tabPanel !== id; });

        history.replaceState(null, '', '#' + id);    // URL hash 同步，重整後保持位置
        if (scroll) {
            document.querySelector('.analytics-tab-nav')
                    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }

    tabs.forEach(t => t.addEventListener('click', () => activate(t.dataset.tab, false)));
    document.querySelectorAll('[data-tab-next]').forEach(btn =>
        btn.addEventListener('click', () => activate(btn.dataset.tabNext, true)));
    document.querySelectorAll('[data-tab-prev]').forEach(btn =>
        btn.addEventListener('click', () => activate(btn.dataset.tabPrev, true)));

    activate(location.hash.slice(1) || 'data-map', false);   // 預設 Tab 1
})();
```

### Tab 3 推論分析：`<details>` 原生折疊

每個方法族群用 `<details>/<summary>` 實作可折疊區塊，無需額外 JavaScript：

```html
<details class="inference-group" open>
    <summary class="inference-group-summary">
        <div class="inference-group-header">
            <div class="inference-group-info">
                <span class="analysis-badge {{ group.badge_class }}">{{ group.badge }}</span>
                <strong>{{ group.title }}</strong>
                <span>{{ group.desc }}</span>
            </div>
            <div class="inference-group-right">
                <span class="inference-group-count">{{ group.results|length }} 組</span>
                <span class="inference-chevron">▾</span>
            </div>
        </div>
    </summary>
    <div class="inference-group-body analytics-result-grid">
        {% for result in group.results %}
            <article class="analytics-result-card {% if result.skipped_reason %}inference-card-skipped{% endif %}">
                ...
            </article>
        {% endfor %}
    </div>
</details>
```

---

## 8. 前端設計系統：純手寫 CSS

### CSS 設計原則

所有樣式集中在 `static/css/app.css`，無外部框架。設計語言以 CSS 自訂屬性（Custom Properties）為核心：

```css
:root {
    --bg:           #f4f1e8;   /* 主背景：米色暖調 */
    --bg-soft:      #fbf8f1;
    --ink:          #122033;   /* 主文字：深海藍 */
    --muted:        #66768c;   /* 次要文字 */
    --line:         rgba(18, 32, 51, 0.12);  /* 邊框線 */
    --surface:      rgba(255, 255, 255, 0.82);  /* 卡片底色 */
    --brand:        #c86432;   /* 主色：磚橙 */
    --brand-deep:   #9c4722;
    --accent:       #1a3554;   /* 強調色：深藍 */
    --accent-soft:  #dde8f5;
    --success:      #1c8f5b;
    --shadow:       0 20px 48px rgba(18, 32, 51, 0.08);
}
```

### 元件模式：卡片系統

```css
/* 分析結果卡片：統一邊框 + 圓角 + 半透明白底 */
.analytics-question-card,
.analytics-result-card {
    padding: 16px;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.72);
}

/* 條件未通過的推論卡片：左側橙色邊框提示 */
.inference-card-skipped {
    background: rgba(26, 53, 84, 0.045);
    border-left: 4px solid #d08a2a;
}

/* 洞察文字區塊：左側品牌色邊框 */
.analytics-insight {
    padding: 12px 14px;
    border-left: 4px solid var(--brand);
    border-radius: 10px;
    background: rgba(196, 91, 43, 0.08);
}
```

### 元件模式：方法徽章（Badge）

```css
.analysis-badge {
    display: inline-flex;
    align-items: center;
    padding: 5px 10px;
    border-radius: 999px;          /* 膠囊形 */
    background: rgba(26, 53, 84, 0.08);
    font-size: 0.75rem;
    font-weight: 800;
    white-space: nowrap;
}

/* 各統計方法族群的顏色語義 */
.method-mean     { background: rgba(28, 143, 91, 0.14); color: var(--success); }   /* 綠 = 平均比較 */
.method-category { background: rgba(73, 116, 190, 0.14); color: #335f9f; }          /* 藍 = 類別關聯 */
.method-rank     { background: rgba(196, 91, 43, 0.14); color: var(--brand); }      /* 橙 = 順序/相關 */
```

### 元件模式：Tab 導覽列

```css
/* Sticky 定位：問卷卷動時 Tab 保持在視窗頂部 */
.analytics-tab-nav {
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 12px 16px;
    border: 1px solid rgba(79, 125, 100, 0.18);
    border-radius: 14px;
    background: linear-gradient(135deg,
        rgba(79, 125, 100, 0.06),
        rgba(26, 53, 84, 0.03)
    );
}

.analytics-tab-active,
.analytics-tab-active:hover {
    background: var(--brand);
    border-color: var(--brand);
    color: #fff;
}

/* Tab badge 計數：active 狀態改白底 */
.analytics-tab-active .analytics-tab-badge {
    background: rgba(255, 255, 255, 0.22);
    color: #fff;
}
```

### 元件模式：`<details>` 折疊的 Chevron 動畫

```css
.inference-chevron {
    font-size: 0.85rem;
    color: var(--muted);
    transition: transform 200ms ease;
}

/* details[open] 時 chevron 旋轉 180°，純 CSS，無 JS */
.inference-group[open] .inference-chevron {
    transform: rotate(180deg);
}
```

### 版面模式：Manager Sidebar 固定

```css
/* 管理者工作台整體：整頁高度不溢出 */
.manager-shell {
    height: 100vh;
    overflow: hidden;
    display: grid;
    grid-template-columns: 240px 1fr;
}

/* 側邊欄固定，右側內容區可獨立捲動 */
.manager-sidebar {
    position: sticky;
    height: 100vh;
    overflow-y: auto;
}

.manager-main {
    overflow-y: auto;
    height: 100vh;
}
```

---

## 9. AJAX 互動：通知已讀標記

### 後端 View（`feedback/views.py`）

```python
class MarkNoticeReadView(CustomerRequiredMixin, View):
    def post(self, request, pk):
        notice = get_object_or_404(
            ImprovementDispatch,
            pk=pk,
            submission__respondent_email=request.user.email
        )
        if not notice.is_read:
            notice.is_read = True
            notice.save(update_fields=["is_read"])

        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        if is_ajax:
            return JsonResponse({"ok": True})
        return redirect(notice.submission.survey.get_absolute_url())
```

### 前端 JavaScript（`customer_notifications.html`）

```javascript
document.querySelectorAll('.record-row[data-pk]').forEach(row => {
    row.addEventListener('click', async (e) => {
        e.preventDefault();
        const pk       = row.dataset.pk;
        const isRead   = row.dataset.isRead === 'true';
        const nextUrl  = row.dataset.surveyUrl;

        if (!isRead) {
            await fetch(`/app/notifications/${pk}/read/`, {
                method: 'POST',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': getCookie('csrftoken'),   // Django CSRF
                }
            });
            // 前端即時更新：移除未讀樣式、更新 badge 數字
            row.classList.remove('record-row-unread');
            updateUnreadBadge(-1);
        }
        window.location.href = nextUrl;
    });
});
```

---

## 10. 問卷管理：ORM 查詢優化

### 避免 N+1：一次 annotate 取得統計數字

```python
# feedback/views.py — SurveyManagerView
surveys = (
    Survey.objects
        .select_related("category")
        .annotate(
            question_count      = Count("questions", distinct=True),
            response_count      = Count("submissions", distinct=True),
            latest_submission_at= Max("submissions__submitted_at"),
        )
)
```

### 三日趨勢圖：單次查詢 + Python 端組裝

```python
today = timezone.now().date()
three_days = [today - timedelta(days=i) for i in range(2, -1, -1)]

trend_qs = (
    FeedbackSubmission.objects
        .filter(survey=survey, submitted_at__date__gte=three_days[0])
        .annotate(day=TruncDate("submitted_at"))
        .values("day")
        .annotate(count=Count("id"))
)
# 轉成 {date: count} dict，再補 0 給沒有回覆的日期
daily = {row["day"]: row["count"] for row in trend_qs}
trend_data = [daily.get(day, 0) for day in three_days]
```

---

## 11. 通知系統：Context Processor + 全域 Badge

### 注入未讀數量（`feedback/context_processors.py`）

```python
def unread_notification_count(request):
    if not request.user.is_authenticated or request.user.is_manager:
        return {"unread_notification_count": 0}
    count = ImprovementDispatch.objects.filter(
        submission__respondent_email=request.user.email,
        is_read=False
    ).count()
    return {"unread_notification_count": count}
```

注册在 `config/settings.py` 的 `TEMPLATES.context_processors`，每個 template 渲染都自動帶入，不需要各 view 單獨處理。

### Navbar Badge（`base.html`）

```html
<a href="{% url 'feedback:customer-notifications' %}" class="nav-link">
    通知
    {% if unread_notification_count > 0 %}
        <span class="nav-badge">{{ unread_notification_count }}</span>
    {% endif %}
</a>
```

```css
/* 紅色圓點 badge，絕對定位於連結右上角 */
.nav-badge {
    position: absolute;
    top: -4px;
    right: -8px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border-radius: 999px;
    background: #e03434;
    color: #fff;
    font-size: 0.68rem;
    font-weight: 800;
    line-height: 18px;
    text-align: center;
}
```

---

## 12. 資料模型關鍵欄位

| Model | 欄位 | 型態 | 說明 |
|---|---|---|---|
| `Survey` | `slug` | SlugField unique | URL 識別子，從 title auto-generate |
| `Survey` | `category` | FK → SurveyCategory | 可選分類，用於篩選與統計 |
| `Survey` | `improvement_tracking_enabled` | BooleanField | 控制改善追蹤功能開關 |
| `Question` | `data_type` | CharField choices | `continuous / discrete / nominal / ordinal / text` |
| `Question` | `options_text` | TextField | 選項文字（換行分隔），ordinal 題必填以支援秩次編碼 |
| `Answer` | `analysis_text` | TextField null | 文字分析快取：正規化 token 字串 |
| `Answer` | `sentiment_score` | FloatField null | 情緒分數快取：-1.0 ~ 1.0 |
| `Answer` | `analysis_version` | CharField | Pipeline 版本，用於判斷是否需重算 |
| `ImprovementDispatch` | `is_read` | BooleanField default=False | 顧客已讀狀態，AJAX 標記 |

---

## 13. 部署與建置

### Render 部署流程（`build.sh`）

```bash
pip install -r requirements.txt
python manage.py migrate          # 套用所有 migration
python manage.py ensure_superuser # 從環境變數建立 admin 帳號
python manage.py seed_demo        # 填入 demo 資料
python manage.py collectstatic --noinput
```

### 環境變數自動偵測 Email Backend

```python
# config/settings.py
if os.getenv("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    # 本機開發無需設定 SMTP，Email 內容輸出到 console
```

---

*文件生成：2026-05-10 | 對應程式碼版本：feedback 0010 migration、text pipeline v2*
