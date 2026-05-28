(function () {
    "use strict";

    var input = document.getElementById("mineFilter");
    var clearBtn = document.querySelector(".mine-search-clear");
    var hint = document.getElementById("mineHint");
    var lists = [document.getElementById("minePosts"), document.getElementById("mineComments")].filter(Boolean);

    function norm(s) { return (s || "").toString().trim().toLowerCase(); }

    function apply() {
        var q = norm(input && input.value);
        var shown = 0, total = 0;

        lists.forEach(function (wrap) {
            wrap.querySelectorAll(".mine-item").forEach(function (it) {
                total++;
                var hay = norm(it.getAttribute("data-filter"));
                var ok = !q || hay.indexOf(q) !== -1;
                it.style.display = ok ? "" : "none";
                if (ok) shown++;
            });
        });

        if (hint) {
            hint.textContent = q
                ? ("필터: “" + q + "” · 표시 " + shown + " / " + total)
                : "리스트는 화면에서만 필터링돼요 (DB 검색 아님)";
        }
        if (clearBtn) clearBtn.style.opacity = q ? "1" : ".35";
    }

    if (clearBtn) {
        clearBtn.addEventListener("click", function () {
            if (!input) return;
            input.value = "";
            input.focus();
            apply();
        });
    }

    document.addEventListener("keydown", function (e) {
        if (e.key === "/" && !(e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA"))) {
            e.preventDefault();
            input && input.focus();
        }
        if (e.key === "Escape" && document.activeElement === input) {
            input.value = "";
            apply();
        }
    });

    if (input) {
        input.addEventListener("input", apply);
        apply();
    }
})();
