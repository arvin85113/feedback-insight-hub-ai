document.querySelectorAll('.copy-survey-link').forEach(function(btn) {
    btn.addEventListener('click', function() {
        navigator.clipboard.writeText(btn.dataset.url).then(function() {
            btn.textContent = '已複製連結';
            setTimeout(function() {
                btn.textContent = '複製填答連結';
            }, 2000);
        });
    });
});

// Row menus: only one open at a time; a click outside or Escape closes it.
var menus = document.querySelectorAll('.sm-menu');
menus.forEach(function(menu) {
    menu.addEventListener('toggle', function() {
        if (!menu.open) return;
        menus.forEach(function(other) {
            if (other !== menu) other.open = false;
        });
    });
});
document.addEventListener('click', function(e) {
    menus.forEach(function(menu) {
        if (!menu.contains(e.target)) menu.open = false;
    });
});
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    menus.forEach(function(menu) {
        if (menu.open) {
            menu.open = false;
            menu.querySelector('summary').focus();
        }
    });
});

document.querySelectorAll('[data-autosubmit]').forEach(function(field) {
    field.addEventListener('change', function() {
        field.form.submit();
    });
});

var panelToggle = document.getElementById('category-panel-toggle');
var categoryPanel = document.getElementById('category-panel');
if (panelToggle && categoryPanel) {
    panelToggle.addEventListener('click', function() {
        var opening = categoryPanel.hasAttribute('hidden');
        categoryPanel.toggleAttribute('hidden', !opening);
        panelToggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
        panelToggle.textContent = opening ? '收起分類' : '管理分類';
    });
}
