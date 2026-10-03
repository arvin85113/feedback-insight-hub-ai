/* Question cards of the survey builder (builder spec §3).
 * One card open at a time; switching away from unsaved edits asks save / discard / keep editing;
 * leaving the page with unsaved edits is confirmed; option rows can be added, moved and removed. */
(function () {
    const cards = Array.from(document.querySelectorAll('[data-card]'));
    if (!cards.length) return;
    const dialog = document.getElementById('card-switch-dialog');
    const live = document.querySelector('[data-builder-live]');
    let openCard = cards.find(card => card.classList.contains('is-open')) || null;
    let submitting = false;

    const formOf = card => card.querySelector('[data-card-form]');
    const snapshot = form => new URLSearchParams(new FormData(form)).toString();
    const initial = new Map();
    cards.forEach(card => { const form = formOf(card); if (form) initial.set(form, snapshot(form)); });
    const isDirty = card => {
        const form = card && formOf(card);
        return Boolean(form) && snapshot(form) !== initial.get(form);
    };
    // A card re-rendered after a failed save (errors or a version conflict) still holds unsaved input.
    if (openCard && formOf(openCard)) initial.set(formOf(openCard), '');

    function setOpen(card, open) {
        const form = formOf(card);
        if (!form) return;
        form.hidden = !open;
        card.classList.toggle('is-open', open);
        const toggle = card.querySelector('[data-card-toggle]');
        if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) openCard = card; else if (openCard === card) openCard = null;
    }

    function discard(card) {
        const form = formOf(card);
        form.reset();
        initial.set(form, snapshot(form));
        applyType(card);
    }

    function askBeforeLeaving(card, next) {
        if (!isDirty(card) || !dialog || typeof dialog.showModal !== 'function') { next(); return; }
        dialog.showModal();
        dialog.onclick = event => {
            const choice = event.target.dataset && event.target.dataset.dialog;
            if (!choice) return;
            dialog.close();
            if (choice === 'save') { submitting = true; formOf(card).requestSubmit(); }
            else if (choice === 'discard') { discard(card); next(); }
        };
    }

    // ── Sections follow the chosen question type ──
    function applyType(card) {
        const select = card.querySelector('[data-ui-type]');
        if (!select) return;
        const type = select.value;
        card.querySelectorAll('[data-section]').forEach(section => {
            const visible = section.dataset.section.split(' ').includes(type);
            section.hidden = !visible;
            section.querySelectorAll('input, select').forEach(input => { input.disabled = !visible; });
        });
        const rows = card.querySelector('[data-choice-rows]');
        if (rows && ['radio', 'dropdown', 'checkbox'].includes(type) && !rows.querySelector('[data-choice-row]')) {
            addRow(card); addRow(card);
        }
    }

    // ── Option rows ──
    function renumber(card) {
        card.querySelectorAll('[data-choice-row]').forEach((row, index) => {
            const position = row.querySelector('[data-choice-position]');
            if (position) position.value = index;
        });
    }

    function addRow(card, after) {
        const template = card.querySelector('[data-choice-template]');
        const rows = card.querySelector('[data-choice-rows]');
        const index = Date.now().toString().slice(-6) + rows.children.length;
        const html = template.innerHTML.replace(/__N__/g, index);
        const holder = document.createElement('div');
        holder.innerHTML = html.trim();
        const row = holder.firstElementChild;
        if (after) after.after(row); else rows.appendChild(row);
        applyType(card);
        renumber(card);
        return row;
    }

    cards.forEach(card => {
        const form = formOf(card);
        const toggle = card.querySelector('[data-card-toggle]');
        if (toggle && form) {
            toggle.addEventListener('click', () => {
                if (openCard === card) { askBeforeLeaving(card, () => setOpen(card, false)); return; }
                const open = () => { setOpen(card, true); const title = form.querySelector('input[name="title"]'); if (title) title.focus(); };
                if (openCard) askBeforeLeaving(openCard, () => { setOpen(openCard, false); open(); });
                else open();
            });
        }
        if (!form) return;

        const select = card.querySelector('[data-ui-type]');
        const tracking = card.querySelector('[data-tracking]');
        if (select) {
            select.addEventListener('change', () => {
                // New cards: long text is analysed by default, short text is not (spec §1).
                if (form.dataset.trackingDefaultLong && tracking) tracking.checked = select.value === 'long_text';
                applyType(card);
            });
        }
        applyType(card);

        card.addEventListener('click', event => {
            const row = event.target.closest('[data-choice-row]');
            if (event.target.closest('[data-choice-add]')) { const added = addRow(card); added.querySelector('[data-choice-label]').focus(); }
            if (!row) return;
            if (event.target.closest('[data-choice-remove]')) { row.remove(); renumber(card); }
            if (event.target.closest('[data-choice-up]') && row.previousElementSibling) { row.previousElementSibling.before(row); renumber(card); }
            if (event.target.closest('[data-choice-down]') && row.nextElementSibling) { row.nextElementSibling.after(row); renumber(card); }
        });
        card.addEventListener('keydown', event => {
            if (event.key === 'Enter' && event.target.matches('[data-choice-label]')) {
                event.preventDefault();
                addRow(card, event.target.closest('[data-choice-row]')).querySelector('[data-choice-label]').focus();
            }
        });
        form.addEventListener('submit', () => { renumber(card); submitting = true; });
    });

    document.querySelectorAll('form[data-confirm]').forEach(form => {
        form.addEventListener('submit', event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
    });
    document.querySelectorAll('form:not([data-card-form])').forEach(form => form.addEventListener('submit', () => { submitting = true; }));

    const dismiss = document.querySelector('[data-dismiss-alert]');
    if (dismiss) dismiss.addEventListener('click', () => dismiss.closest('[role="alert"]').remove());

    window.addEventListener('beforeunload', event => {
        if (submitting || !cards.some(isDirty)) return;
        event.preventDefault();
        event.returnValue = '';
    });

    // Announce the last success message (e.g. "已移到第 2 題") to screen readers.
    const notice = document.querySelector('.messages .message-success, .message-success');
    if (live && notice) live.textContent = notice.textContent.trim();
})();
