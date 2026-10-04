/* Question cards of the survey builder (builder spec §3), laid out like Google Forms.
 * One card open at a time; switching away from unsaved edits asks save / discard / keep editing;
 * leaving the page with unsaved edits is confirmed. Option rows can be added (Enter), removed,
 * dragged by their handle or moved with Alt+Arrow; ordered options show their scores. */
(function () {
    const cards = Array.from(document.querySelectorAll('[data-card]'));
    if (!cards.length) return;
    const dialog = document.getElementById('card-switch-dialog');
    const live = document.querySelector('[data-builder-live]');
    const CHOICE_TYPES = ['radio', 'dropdown', 'checkbox'];
    let openCard = cards.find(card => card.classList.contains('is-open')) || null;
    let submitting = false;

    const formOf = card => card.querySelector('[data-card-form]');
    const snapshot = form => new URLSearchParams(new FormData(form)).toString();
    const initial = new Map();
    // Snapshot only after each card's type sections are applied: disabled inputs drop out of FormData,
    // so an earlier snapshot would mark every card as edited on page load.
    cards.forEach(card => { if (formOf(card)) applyType(card); });
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
        applyType(card);
        initial.set(form, snapshot(form));
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

    // ── Sections follow the chosen question type (none until one is picked) ──
    function applyType(card) {
        const form = formOf(card);
        const select = card.querySelector('[data-ui-type]');
        if (!form || !select) return;
        const type = select.value;
        form.dataset.type = type;
        card.querySelectorAll('[data-section]').forEach(section => {
            const visible = section.dataset.section.split(' ').includes(type);
            section.hidden = !visible;
            section.querySelectorAll('input, select').forEach(input => { input.disabled = !visible; });
        });
        const pill = card.querySelector('[data-type-pill]');
        if (pill) pill.textContent = select.selectedIndex >= 0 && type ? select.options[select.selectedIndex].text : '';
        const rows = card.querySelector('[data-choice-rows]');
        if (rows && CHOICE_TYPES.includes(type) && !rows.querySelector('[data-choice-row]')) {
            addRow(card); addRow(card);
        }
        refresh(card);
    }

    // ── Option rows ──
    function rowsOf(card) { return Array.from(card.querySelectorAll('[data-choice-rows] [data-choice-row]')); }

    // Positions, placeholders, excluded tags, scores and scale numbers in one pass.
    function refresh(card) {
        const form = formOf(card);
        if (!form) return;
        const ordered = form.querySelector('[data-ordered]');
        const isOrdered = Boolean(ordered && ordered.checked && !ordered.disabled);
        const startField = form.querySelector('[data-score-start]');
        if (startField) startField.hidden = !isOrdered;
        const startSelect = form.querySelector('[data-score-start-select]');
        let score = startSelect ? parseInt(startSelect.value, 10) || 0 : 1;
        let number = 0;
        rowsOf(card).forEach((row, index) => {
            const position = row.querySelector('[data-choice-position]');
            if (position) position.value = index;
            const excluded = row.querySelector('[data-choice-excluded]').value === '1';
            row.classList.toggle('is-excluded', excluded);
            const label = row.querySelector('[data-choice-label]');
            if (excluded) {
                label.placeholder = '例如：不適用、不記得';
            } else {
                number += 1;
                label.placeholder = `選項 ${number}`;
            }
            const scoreTag = row.querySelector('[data-choice-score]');
            const counted = isOrdered && !excluded && label.value.trim();
            scoreTag.textContent = counted ? `${score} 分` : '';
            if (counted) score += 1;
        });
        const min = form.querySelector('[data-scale-min]');
        const max = form.querySelector('[data-scale-max]');
        const minNumber = form.querySelector('[data-scale-min-number]');
        const maxNumber = form.querySelector('[data-scale-max-number]');
        if (min && minNumber) minNumber.textContent = min.value;
        if (max && maxNumber) maxNumber.textContent = max.value;
    }

    function addRow(card, { after = null, excluded = false } = {}) {
        const template = card.querySelector('[data-choice-template]');
        const rows = card.querySelector('[data-choice-rows]');
        const index = Date.now().toString().slice(-6) + rows.children.length;
        const holder = document.createElement('div');
        holder.innerHTML = template.innerHTML.replace(/__N__/g, index).trim();
        const row = holder.firstElementChild;
        if (excluded) row.querySelector('[data-choice-excluded]').value = '1';
        const firstExcluded = rows.querySelector('.is-excluded');
        if (after) after.after(row);
        else if (!excluded && firstExcluded) firstExcluded.before(row);  // ordinary options stay above the excluded ones
        else rows.appendChild(row);
        refresh(card);
        return row;
    }

    function moveRow(card, row, direction) {
        const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (!sibling) return;
        if (direction < 0) sibling.before(row); else sibling.after(row);
        refresh(card);
    }

    function enableDrag(card) {
        const rows = card.querySelector('[data-choice-rows]');
        if (!rows) return;
        let dragged = null;
        // Only the handle starts a drag, so text in the option inputs can still be selected.
        rows.addEventListener('mousedown', event => {
            const handle = event.target.closest('[data-choice-handle]');
            if (handle) handle.closest('[data-choice-row]').draggable = true;
        });
        rows.addEventListener('dragstart', event => {
            dragged = event.target.closest('[data-choice-row]');
            if (!dragged) return;
            dragged.classList.add('is-dragging');
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', '');
        });
        rows.addEventListener('dragover', event => {
            if (!dragged) return;
            event.preventDefault();
            const target = event.target.closest('[data-choice-row]');
            if (!target || target === dragged) return;
            const box = target.getBoundingClientRect();
            if (event.clientY < box.top + box.height / 2) target.before(dragged); else target.after(dragged);
        });
        rows.addEventListener('dragend', () => {
            if (!dragged) return;
            dragged.classList.remove('is-dragging');
            dragged.draggable = false;
            dragged = null;
            refresh(card);
        });
    }

    function toggleCard(card) {
        const form = formOf(card);
        if (!form) return;
        if (openCard === card) { askBeforeLeaving(card, () => setOpen(card, false)); return; }
        const open = () => {
            setOpen(card, true);
            const title = form.querySelector('input[name="title"]');
            if (title) title.focus();
        };
        if (openCard) askBeforeLeaving(openCard, () => { setOpen(openCard, false); open(); });
        else open();
    }

    cards.forEach(card => {
        const form = formOf(card);
        const toggle = card.querySelector('[data-card-toggle]');
        if (toggle && form) {
            toggle.addEventListener('click', event => {
                // The move buttons inside the summary submit their own form.
                if (event.target !== toggle && event.target.closest('button, a, input, select')) return;
                toggleCard(card);
            });
            toggle.addEventListener('keydown', event => {
                if (event.target !== toggle || (event.key !== 'Enter' && event.key !== ' ')) return;
                event.preventDefault();
                toggleCard(card);
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
        enableDrag(card);

        form.addEventListener('input', () => refresh(card));
        form.addEventListener('change', () => refresh(card));
        card.addEventListener('click', event => {
            if (event.target.closest('[data-choice-add]')) addRow(card).querySelector('[data-choice-label]').focus();
            if (event.target.closest('[data-choice-add-excluded]')) addRow(card, { excluded: true }).querySelector('[data-choice-label]').focus();
            const row = event.target.closest('[data-choice-row]');
            if (row && event.target.closest('[data-choice-remove]')) { row.remove(); refresh(card); }
        });
        card.addEventListener('keydown', event => {
            if (!event.target.matches('[data-choice-label]')) return;
            const row = event.target.closest('[data-choice-row]');
            if (event.key === 'Enter') {
                event.preventDefault();
                addRow(card, { after: row }).querySelector('[data-choice-label]').focus();
            } else if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
                event.preventDefault();
                moveRow(card, row, event.key === 'ArrowUp' ? -1 : 1);
                event.target.focus();
            }
        });
        form.addEventListener('submit', () => { refresh(card); submitting = true; });
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
