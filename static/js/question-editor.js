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
        if (select.renderPicker) select.renderPicker();
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

    // ── Question type menu with icons and groups, like Google Forms. The native select stays
    //    in the form (and stays visible if this script fails); the menu only drives it. ──
    const svg = body => `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
    const TYPE_ICONS = {
        short_text: svg('<path d="M4 9h16M4 15h10"/>'),
        long_text: svg('<path d="M4 6h16M4 10h16M4 14h16M4 18h10"/>'),
        radio: svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/>'),
        checkbox: svg('<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M8 12.5l3 3 5-6"/>'),
        dropdown: svg('<circle cx="12" cy="12" r="8"/><path d="M8.5 10.5 12 14l3.5-3.5"/>'),
        scale: svg('<path d="M4 12h16"/><circle cx="6" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="18" cy="12" r="1.6" fill="currentColor"/>'),
        number: svg('<path d="M9 4 7 20M17 4l-2 16M4.5 9h15M3.5 15h15"/>'),
    };
    const GROUP_ENDS = ['long_text', 'dropdown'];  // a divider follows these, as in Google Forms

    function buildTypePicker(card) {
        const select = card.querySelector('[data-ui-type]');
        if (!select) return;
        const picker = document.createElement('div');
        picker.className = 'type-picker';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'type-picker-button';
        button.setAttribute('aria-haspopup', 'listbox');
        button.setAttribute('aria-expanded', 'false');
        const menu = document.createElement('ul');
        menu.className = 'type-picker-menu';
        menu.setAttribute('role', 'listbox');
        menu.hidden = true;
        Array.from(select.options).filter(option => option.value).forEach(option => {
            const item = document.createElement('li');
            item.setAttribute('role', 'option');
            item.tabIndex = -1;
            item.dataset.value = option.value;
            item.innerHTML = `${TYPE_ICONS[option.value] || ''}<span></span>`;
            item.querySelector('span').textContent = option.text;
            menu.appendChild(item);
            if (GROUP_ENDS.includes(option.value)) {
                const divider = document.createElement('li');
                divider.className = 'type-picker-divider';
                divider.setAttribute('role', 'separator');
                menu.appendChild(divider);
            }
        });
        picker.append(button, menu);
        select.after(picker);
        select.classList.add('type-picker-native');

        const items = () => Array.from(menu.querySelectorAll('[role="option"]'));
        const render = () => {
            const value = select.value;
            button.innerHTML = `${TYPE_ICONS[value] || ''}<span></span><span class="type-picker-caret" aria-hidden="true">▾</span>`;
            button.querySelector('span').textContent = value ? select.options[select.selectedIndex].text : '請選擇題型';
            button.classList.toggle('is-empty', !value);
            items().forEach(item => item.setAttribute('aria-selected', item.dataset.value === value ? 'true' : 'false'));
        };
        const close = () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); };
        const open = () => {
            menu.hidden = false;
            button.setAttribute('aria-expanded', 'true');
            (items().find(item => item.dataset.value === select.value) || items()[0]).focus();
        };
        const choose = value => {
            close();
            button.focus();
            if (select.value === value) return;
            select.value = value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
        };
        button.addEventListener('click', () => (menu.hidden ? open() : close()));
        menu.addEventListener('click', event => {
            const item = event.target.closest('[role="option"]');
            if (item) choose(item.dataset.value);
        });
        menu.addEventListener('keydown', event => {
            const list = items();
            const index = list.indexOf(document.activeElement);
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                const step = event.key === 'ArrowDown' ? 1 : -1;
                list[(index + step + list.length) % list.length].focus();
            } else if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                if (index >= 0) choose(list[index].dataset.value);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                close();
                button.focus();
            }
        });
        document.addEventListener('click', event => { if (!picker.contains(event.target)) close(); });
        select.addEventListener('change', render);
        select.renderPicker = render;  // form.reset() changes the select without a change event
        render();
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
        buildTypePicker(card);

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
