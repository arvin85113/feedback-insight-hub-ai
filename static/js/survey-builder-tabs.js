(function () {
    // ── Tab bar（題目設定 / 問卷設定）──
    const builderTabs = document.querySelectorAll('.builder-tab');
    const panelQuestions = document.getElementById('panel-questions');
    const panelSettings = document.getElementById('panel-settings');
    if (!panelQuestions || !panelSettings) return;

    function activateBuilderTab(key) {
        builderTabs.forEach(btn => btn.classList.toggle('builder-tab-active', btn.dataset.builderTab === key));
        panelQuestions.style.display = key === 'questions' ? '' : 'none';
        panelSettings.style.display = key === 'settings' ? 'block' : 'none';
    }

    builderTabs.forEach(btn => btn.addEventListener('click', () => activateBuilderTab(btn.dataset.builderTab)));

    // POST 後 ?tab=settings 自動切換
    const urlTab = new URLSearchParams(location.search).get('tab');
    if (urlTab === 'settings') activateBuilderTab('settings');

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
