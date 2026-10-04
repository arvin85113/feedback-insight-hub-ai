(function () {
    const form = document.getElementById('surveyForm');
    if (!form) return;

    const steps    = Array.from(form.querySelectorAll('.survey-step'));
    const totalQ   = parseInt(form.dataset.totalQ, 10);
    const last     = steps.length - 1;
    let current    = 0;

    const strip     = document.getElementById('surveyProgressStrip');
    const label     = document.getElementById('surveyProgressLabel');
    const fraction  = document.getElementById('surveyProgressFraction');
    const fill      = document.getElementById('surveyProgressFill');
    const nav       = document.getElementById('surveyNav');
    const prevBtn   = document.getElementById('surveyPrev');
    const nextBtn   = document.getElementById('surveyNext');
    const submitBtn = document.getElementById('surveySubmit');
    const fallback  = document.getElementById('surveyFallbackSubmit');
    // The builder preview renders the questions without navigation; leave them all visible.
    if (!nav || !fallback) return;

    // Activate step mode
    strip.style.display    = '';
    nav.style.display      = '';
    fallback.style.display = 'none';

    function showStep(n) {
        steps.forEach(function (s, i) { s.style.display = i === n ? '' : 'none'; });

        const isInfo = n === 0;
        const isLast = n === last;

        // Progress
        var pct = isInfo ? 0 : (n / totalQ) * 100;
        fill.style.width = pct + '%';

        if (isInfo) {
            label.textContent    = '填答者資訊';
            fraction.textContent = '';
        } else {
            label.textContent    = '第 ' + n + ' 題';
            fraction.textContent = n + ' / ' + totalQ;
        }

        // Buttons
        prevBtn.style.display   = n === 0 ? 'none' : '';
        nextBtn.style.display   = isLast ? 'none' : '';
        submitBtn.style.display = isLast ? '' : 'none';
        nextBtn.textContent     = isInfo ? '開始填答 →' : '下一題 →';

        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    prevBtn.addEventListener('click', function () {
        if (current > 0) { current--; showStep(current); }
    });

    nextBtn.addEventListener('click', function () {
        if (current < last) { current++; showStep(current); }
    });

    // On validation errors: jump to first step that has an error
    var errorIdx = steps.findIndex(function (s) { return s.dataset.hasError === '1'; });
    current = errorIdx >= 0 ? errorIdx : 0;
    showStep(current);
})();
