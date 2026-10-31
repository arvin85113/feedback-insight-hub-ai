// Filters and pickers marked data-autosubmit submit their form on change; a <noscript> button covers no-JS.
document.querySelectorAll('[data-autosubmit]').forEach(function(field) {
    field.addEventListener('change', function() {
        field.form.submit();
    });
});
