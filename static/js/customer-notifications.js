
document.querySelectorAll('.customer-record-clickable').forEach(function(row) {
    row.addEventListener('click', async function () {
        const pk = this.dataset.pk;
        const isRead = this.dataset.isRead === 'true';
        const surveyUrl = this.dataset.surveyUrl;

        if (!isRead) {
            const csrfMatch = document.cookie.match(/csrftoken=([^;]+)/);
            const csrf = csrfMatch ? csrfMatch[1] : '';
            await fetch('/app/notifications/' + pk + '/read/', {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrf,
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });
            this.classList.remove('customer-record-unread');
            this.dataset.isRead = 'true';
            const pill = this.querySelector('.pill');
            if (pill) {
                pill.classList.add('pill-active');
                pill.textContent = '已讀';
            }
            const badge = document.querySelector('.nav-badge');
            if (badge) {
                const count = parseInt(badge.textContent, 10) - 1;
                if (count <= 0) { badge.remove(); }
                else { badge.textContent = count; }
            }
        }

        if (surveyUrl) { window.location.href = surveyUrl; }
    });
});
