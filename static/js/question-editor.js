document.addEventListener('DOMContentLoaded', function() {
    const kindSelect = document.getElementById('id_kind');
    const dataTypeInput = document.getElementById('id_data_type');
    const kindUsageHint = document.getElementById('kind-usage-hint');
    const questionOrderLabel = document.getElementById('question-order-label');
    const questionOrderInput = document.querySelector('input[name="order"]');

    if (questionOrderLabel && questionOrderInput) {
        questionOrderLabel.textContent = '第 ' + (questionOrderInput.value || '?') + ' 題';
    }

    const sectionOptions = document.getElementById('section-options');
    const sectionScale = document.getElementById('section-secondary-scale');
    const sectionNumber = document.getElementById('section-secondary-number');
    const sectionSingle = document.getElementById('section-secondary-single');
    const sectionKeyword = document.getElementById('section-keyword');

    const dataTypeBtns = document.querySelectorAll('.data-type-btn');
    const kindUsageHints = {
        short_text: '適合收集姓名、職稱、簡短補充；通常作為文字資料保存。',
        long_text: '適合收集原因、建議、開放式回饋；可啟用關鍵字分析。',
        single_choice: '適合部門、地區、角色等分類，或滿意度等順序選項；請再判斷是否有順序。',
        multiple_choice: '適合收集多個原因、偏好或問題來源；會以多重回應頻率分析。',
        scale: '適合 1-10 滿意度、推薦意願、感受強度；可視為連續數值或順序資料。',
        integer: '適合次數、件數、人數，也可用於整數分數；請判斷是連續數值還是離散計數。',
        decimal: '適合金額、時間、比例、平均值等可帶小數的數值；通常視為連續資料。',
    };

    function updateFormDisplay() {
        const kind = kindSelect.value;
        if (kindUsageHint) {
            kindUsageHint.textContent = kindUsageHints[kind] || '選擇題型後，這裡會顯示常見用途與分析方式。';
        }

        sectionOptions.style.display = 'none';
        sectionScale.style.display = 'none';
        sectionNumber.style.display = 'none';
        sectionSingle.style.display = 'none';
        sectionKeyword.style.display = 'none';

        dataTypeBtns.forEach(btn => btn.classList.remove('selected'));

        switch(kind) {
            case 'short_text':
            case 'long_text':
                sectionKeyword.style.display = 'flex';
                dataTypeInput.value = 'text';
                break;
            case 'integer':
            case 'decimal':
                sectionNumber.style.display = 'block';
                selectSecondaryButton(sectionNumber.querySelector('[data-type-value="continuous"]'));
                break;
            case 'scale':
                sectionScale.style.display = 'block';
                selectSecondaryButton(sectionScale.querySelector('[data-type-value="continuous"]'));
                break;
            case 'single_choice':
                sectionOptions.style.display = 'block';
                sectionSingle.style.display = 'block';
                selectSecondaryButton(sectionSingle.querySelector('[data-type-value="ordinal"]'));
                break;
            case 'multiple_choice':
                sectionOptions.style.display = 'block';
                dataTypeInput.value = 'nominal';
                break;
        }
    }

    function selectSecondaryButton(clickedBtn) {
        if (!clickedBtn) return;
        const parentChoiceGroup = clickedBtn.closest('.data-type-choice');
        parentChoiceGroup.querySelectorAll('.data-type-btn').forEach(btn => {
            btn.classList.remove('selected');
        });
        clickedBtn.classList.add('selected');
        dataTypeInput.value = clickedBtn.getAttribute('data-type-value');
    }

    kindSelect.addEventListener('change', updateFormDisplay);

    dataTypeBtns.forEach(btn => {
        btn.addEventListener('click', function() {
            selectSecondaryButton(this);
        });
    });

    updateFormDisplay();

    // ── 選項動態新增（新增題目 form）──
    (function initAddFormOptions() {
        const section = document.getElementById('section-options');
        if (!section) return;
        const textarea = section.querySelector('textarea[name="options_text"]');
        textarea.style.display = 'none';

        const list = document.createElement('div');
        list.className = 'option-input-list';
        ['', '', ''].forEach(() => list.appendChild(makeOptionRow('')));
        section.appendChild(list);

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'option-add-btn';
        addBtn.textContent = '＋ 新增選項';
        addBtn.addEventListener('click', () => list.appendChild(makeOptionRow('')));
        section.appendChild(addBtn);

        document.getElementById('question-form').addEventListener('submit', () => {
            textarea.value = [...list.querySelectorAll('.option-input')]
                .map(i => i.value.trim()).filter(Boolean).join('\n');
        });
    })();

    // ── 選項動態新增（inline-edit-panel）──
    document.querySelectorAll('.inline-edit-panel').forEach(panel => {
        const textarea = panel.querySelector('textarea[name="options_text"]');
        if (!textarea) return;
        textarea.style.display = 'none';

        const list = document.createElement('div');
        list.className = 'option-input-list';
        const existing = textarea.value.trim().split('\n').filter(Boolean);
        (existing.length ? existing : ['']).forEach(val => list.appendChild(makeOptionRow(val)));
        textarea.parentElement.appendChild(list);

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'option-add-btn';
        addBtn.textContent = '＋ 新增選項';
        addBtn.addEventListener('click', () => list.appendChild(makeOptionRow('')));
        textarea.parentElement.appendChild(addBtn);

        panel.querySelector('form').addEventListener('submit', () => {
            textarea.value = [...list.querySelectorAll('.option-input')]
                .map(i => i.value.trim()).filter(Boolean).join('\n');
        });
    });

    function makeOptionRow(value) {
        const row = document.createElement('div');
        row.className = 'option-input-row';
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'option-input';
        input.value = value;
        input.placeholder = '輸入選項';
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'option-del-btn';
        del.textContent = '×';
        del.addEventListener('click', () => {
            const list = row.closest('.option-input-list');
            if (list.querySelectorAll('.option-input-row').length > 1) row.remove();
        });
        row.appendChild(input);
        row.appendChild(del);
        return row;
    }
});
