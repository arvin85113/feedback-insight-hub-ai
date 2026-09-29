document.querySelectorAll('.copy-survey-link').forEach(function(btn) {
    btn.addEventListener('click', function() {
        navigator.clipboard.writeText(btn.dataset.url).then(function() {
            btn.textContent = '已複製！';
            btn.classList.add('copied');
            setTimeout(function() {
                btn.textContent = '複製連結';
                btn.classList.remove('copied');
            }, 2000);
        });
    });
});

function closeQrPopovers(exceptWrapper) {
    document.querySelectorAll('.qr-popover-wrapper').forEach(function(wrapper) {
        if (wrapper === exceptWrapper) return;
        wrapper.classList.remove('open');
        var popover = wrapper.querySelector('.qr-popover');
        var trigger = wrapper.querySelector('.qr-trigger');
        if (popover) popover.hidden = true;
        if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
}

document.querySelectorAll('.qr-trigger').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
        e.preventDefault();
        e.stopPropagation();
        var wrapper = btn.closest('.qr-popover-wrapper');
        var popover = wrapper.querySelector('.qr-popover');
        var shouldOpen = popover.hidden;
        closeQrPopovers(wrapper);
        wrapper.classList.toggle('open', shouldOpen);
        popover.hidden = !shouldOpen;
        btn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    });
});

document.querySelectorAll('.qr-popover').forEach(function(popover) {
    popover.addEventListener('click', function(e) {
        e.stopPropagation();
    });
});

document.addEventListener('click', function() {
    closeQrPopovers();
});

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeQrPopovers();
});

var panelToggle = document.getElementById('category-panel-toggle');
var categoryPanel = document.getElementById('category-panel');
if (panelToggle && categoryPanel) {
    panelToggle.addEventListener('click', function() {
        var hidden = categoryPanel.hasAttribute('hidden');
        if (hidden) {
            categoryPanel.removeAttribute('hidden');
            panelToggle.textContent = '收起分類';
        } else {
            categoryPanel.setAttribute('hidden', '');
            panelToggle.textContent = '管理分類';
        }
    });
}
