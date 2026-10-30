// Customer pages: the forms work without JS; this saves switches on change and marks notices read in place.
(function () {
    document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
        form.addEventListener("change", function () { form.submit(); });
    });

    document.querySelectorAll("form[data-mark-read]").forEach(function (form) {
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            var token = form.querySelector("[name=csrfmiddlewaretoken]");
            fetch(form.action, {
                method: "POST",
                headers: {
                    "X-CSRFToken": token ? token.value : "",
                    "X-Requested-With": "XMLHttpRequest",
                },
            }).then(function (response) {
                if (!response.ok) { form.submit(); return; }
                var item = form.closest("[data-notice]");
                if (item) {
                    item.classList.remove("is-new");
                    var tag = item.querySelector("[data-new-tag]");
                    if (tag) tag.remove();
                }
                form.remove();
                document.querySelectorAll("[data-unread-badge]").forEach(function (badge) {
                    var count = parseInt(badge.textContent, 10) - 1;
                    if (count > 0) badge.textContent = count;
                    else badge.remove();
                });
            }).catch(function () { form.submit(); });
        });
    });
})();
