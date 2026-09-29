(function () {
    // ── Tab bar（題目設定 / 問卷設定）──
    const builderTabs = document.querySelectorAll('.builder-tab');
    const panelQuestions = document.getElementById('panel-questions');
    const panelSettings = document.getElementById('panel-settings');

    function activateBuilderTab(key) {
        builderTabs.forEach(btn => btn.classList.toggle('builder-tab-active', btn.dataset.builderTab === key));
        panelQuestions.style.display = key === 'questions' ? '' : 'none';
        panelSettings.style.display = key === 'settings' ? 'block' : 'none';
    }

    builderTabs.forEach(btn => btn.addEventListener('click', () => activateBuilderTab(btn.dataset.builderTab)));

    // POST 後 ?tab=settings 自動切換
    const urlTab = new URLSearchParams(location.search).get('tab');
    if (urlTab === 'settings') activateBuilderTab('settings');

    // ── 新增問題展開/收起 ──
    const btnAddQuestion = document.getElementById('btn-add-question');
    const addQuestionPanel = document.getElementById('add-question-panel');
    btnAddQuestion.addEventListener('click', () => {
        const isOpen = addQuestionPanel.style.display !== 'none';
        addQuestionPanel.style.display = isOpen ? 'none' : 'block';
        btnAddQuestion.textContent = isOpen ? '＋ 新增問題' : '▲ 收起';
    });

    // ── Inline edit toggle ──
    let openPanel = null;
    document.querySelectorAll('.edit-question-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const id = btn.dataset.questionId;
            const panel = document.getElementById('edit-panel-' + id);
            if (openPanel && openPanel !== panel) openPanel.style.display = 'none';
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
            openPanel = panel.style.display === 'block' ? panel : null;
        });
    });

    document.querySelectorAll('.cancel-edit-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const panel = document.getElementById('edit-panel-' + btn.dataset.questionId);
            panel.style.display = 'none';
            openPanel = null;
        });
    });

    // ── Copy survey URL ──
    const copyBtn = document.getElementById('copy-slug-btn');
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const input = copyBtn.previousElementSibling;
            navigator.clipboard.writeText(input.value).then(() => {
                copyBtn.textContent = '已複製！';
                setTimeout(() => copyBtn.textContent = '複製連結', 2000);
            });
        });
    }
})();
