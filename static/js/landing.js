// Homepage store/customer switch: the links work without JS (?mode=...); this swaps in place.
(function () {
    var root = document.querySelector(".lp[data-mode]");
    if (!root) return;
    var KEY = "landing-mode";
    var links = document.querySelectorAll("[data-set-mode]");

    function apply(mode, remember) {
        root.setAttribute("data-mode", mode);
        links.forEach(function (link) {
            if (link.getAttribute("data-set-mode") === mode) link.setAttribute("aria-current", "page");
            else link.removeAttribute("aria-current");
        });
        if (remember) {
            root.classList.remove("is-switched");
            void root.offsetWidth; // restart the settle animation
            root.classList.add("is-switched");
            try { localStorage.setItem(KEY, mode); } catch (e) { /* storage unavailable */ }
            var url = new URL(window.location.href);
            url.searchParams.set("mode", mode);
            url.hash = "";
            history.replaceState(null, "", url);
        }
    }

    links.forEach(function (link) {
        link.addEventListener("click", function (event) {
            event.preventDefault();
            var mode = link.getAttribute("data-set-mode");
            if (mode !== root.getAttribute("data-mode")) apply(mode, true);
        });
    });

    // Without ?mode in the URL, reopen the mode the visitor picked last time.
    if (!new URL(window.location.href).searchParams.has("mode")) {
        var saved = null;
        try { saved = localStorage.getItem(KEY); } catch (e) { /* storage unavailable */ }
        if (saved === "store" || saved === "customer") apply(saved, false);
    }
})();
