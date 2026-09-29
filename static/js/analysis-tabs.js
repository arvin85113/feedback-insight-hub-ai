// Step tabs for the statistics and text-insight pages.
// The nav's data-default-tab picks the initial panel when the URL has no hash.
(() => {
    const tabs = Array.from(document.querySelectorAll('.analytics-tab[data-tab]'));
    const panels = Array.from(document.querySelectorAll('[data-tab-panel]'));
    if (!tabs.length || !panels.length) return;
    const nav = document.querySelector('.analytics-tab-nav');

    function activate(tabId, scroll) {
        const valid = tabs.some((tab) => tab.dataset.tab === tabId);
        const id = valid ? tabId : tabs[0].dataset.tab;
        tabs.forEach((tab) => {
            const active = tab.dataset.tab === id;
            tab.classList.toggle('analytics-tab-active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        panels.forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== id; });
        history.replaceState(null, '', '#' + id);
        if (scroll && nav) nav.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    tabs.forEach((tab) => tab.addEventListener('click', () => activate(tab.dataset.tab, false)));
    document.querySelectorAll('[data-tab-next]').forEach((button) =>
        button.addEventListener('click', () => activate(button.dataset.tabNext, true)));
    document.querySelectorAll('[data-tab-prev]').forEach((button) =>
        button.addEventListener('click', () => activate(button.dataset.tabPrev, true)));

    activate(location.hash.slice(1) || (nav && nav.dataset.defaultTab) || tabs[0].dataset.tab, false);
})();
