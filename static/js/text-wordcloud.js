(() => {
    const cloudEl = document.getElementById("keyword-wordcloud");
    const dataEl = document.getElementById("keywords-data");
    const toggleButtons = Array.from(document.querySelectorAll(".wordcloud-toggle"));
    if (!cloudEl || !dataEl) return;

    let keywords = [];
    try {
        keywords = JSON.parse(dataEl.textContent);
    } catch (e) {
        return;
    }
    if (!Array.isArray(keywords) || !keywords.length) {
        cloudEl.innerHTML = "<p class='muted-text'>目前資料不足，暫無文字雲可顯示。</p>";
        return;
    }

    const palette = ["#1a3554", "#c86432", "#2f5b8a", "#2b7a59", "#7c4d9e", "#9c4722", "#0f766e", "#8b5cf6"];
    const categoryColorMap = new Map();

    const resolveCategoryColor = (category, index) => {
        if (!categoryColorMap.has(category)) {
            categoryColorMap.set(category, palette[index % palette.length]);
        }
        return categoryColorMap.get(category);
    };

    document.querySelectorAll(".keyword-signal-row[data-keyword-category]").forEach((row, index) => {
        const category = row.dataset.keywordCategory || "未分類";
        row.style.setProperty("--keyword-color", resolveCategoryColor(category, index));
    });

    const buildCategoryItems = () => {
        const bucket = new Map();
        for (const item of keywords) {
            const category = item.category || "未分類";
            const prev = bucket.get(category) || 0;
            bucket.set(category, prev + (Number(item.count) || 0));
        }
        return [...bucket.entries()].map(([label, count]) => ({
            label,
            count,
            category: label,
        }));
    };

    const renderCloud = (mode) => {
        cloudEl.innerHTML = "";
        const sourceItems = mode === "category"
            ? buildCategoryItems()
            : keywords.map((item) => ({
                label: item.keyword,
                count: Number(item.count) || 0,
                category: item.category || "未分類",
            }));
        const items = sourceItems
            .filter((item) => item.count > 0 && item.label)
            .sort((a, b) => b.count - a.count)
            .slice(0, mode === "category" ? 20 : 40);

        if (!items.length) {
            cloudEl.innerHTML = "<p class='muted-text'>目前資料不足，暫無文字雲可顯示。</p>";
            return;
        }

        const counts = items.map((item) => item.count);
        const min = Math.min(...counts);
        const max = Math.max(...counts);

        for (const [index, item] of items.entries()) {
            const token = document.createElement("span");
            const ratio = max === min ? 0.5 : (item.count - min) / (max - min);
            const size = Math.round(16 + ratio * 26);
            token.className = "wordcloud-token";
            token.textContent = item.label;
            token.title = `${item.label}：${item.count} 次`;
            token.style.fontSize = `${size}px`;
            token.style.color = resolveCategoryColor(item.category, index);
            cloudEl.appendChild(token);
        }
    };

    const setMode = (mode) => {
        for (const btn of toggleButtons) {
            const isActive = btn.dataset.mode === mode;
            btn.classList.toggle("active", isActive);
        }
        renderCloud(mode);
    };

    for (const btn of toggleButtons) {
        btn.addEventListener("click", () => setMode(btn.dataset.mode || "keyword"));
    }

    setMode("keyword");
})();
