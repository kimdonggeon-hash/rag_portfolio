/* ragapp/static/ragapp/javascript/media_search_quota_toast.js */
(function () {
    "use strict";

    function $(sel, root) {
        return (root || document).querySelector(sel);
    }

    function init() {
        var root = document.getElementById("mediaSearchRoot");
        if (!root) return;

        var kind = (root.getAttribute("data-usage-kind") || "image").trim();
        var apiUrl = (root.getAttribute("data-usage-api") || "/api/usage/status/").trim();

        var toast = $("#quotaToast", root);
        var msgEl = $("#quotaToastMsg", root);
        var btnClose = $("#quotaToastClose", root);
        var form = document.getElementById("mediaSearchForm") || $("form", root);

        // ✅ submit UI 복구용(다른 JS에서 submit-lock을 걸어도 여기서 풀 수 있게)
        var submitBtn = document.getElementById("msSubmit") || (form ? form.querySelector('button[type="submit"]') : null);
        var busyEl = document.getElementById("msBusy");

        if (btnClose && toast) {
            btnClose.addEventListener("click", function () {
                toast.classList.remove("is-show");
            });
        }

        function showToast(text) {
            if (!toast || !msgEl) return;
            msgEl.textContent = text;
            toast.classList.add("is-show");
            window.clearTimeout(showToast._t);
            showToast._t = window.setTimeout(function () {
                toast.classList.remove("is-show");
            }, 4200);
        }

        function restoreSubmitUI() {
            try {
                if (submitBtn) submitBtn.disabled = false;
                if (busyEl) busyEl.hidden = true;
            } catch (_) {
                // ignore
            }
        }

        var lastRemain = undefined; // number | null(unlimited) | undefined(unknown)

        function refreshRemain() {
            return fetch(apiUrl, { credentials: "same-origin" })
                .then(function (r) {
                    if (!r.ok) throw new Error("HTTP " + r.status);
                    return r.json();
                })
                .then(function (j) {
                    if (!j) return;

                    if (j.unlimited === true) {
                        lastRemain = null; // unlimited
                        return;
                    }

                    var rem =
                        j.remaining && Object.prototype.hasOwnProperty.call(j.remaining, kind)
                            ? j.remaining[kind]
                            : undefined;

                    lastRemain = rem;

                    if (rem === 0) {
                        showToast("오늘 이미지 검색 횟수를 다 썼어요. 내일 다시 사용할 수 있어요.");
                    }
                })
                .catch(function () {
                    // 네트워크/서버 오류는 조용히 무시(UX 보호)
                });
        }

        // 초기 조회
        refreshRemain();

        // submit 차단
        if (form) {
            form.addEventListener(
                "submit",
                function (e) {
                    // unlimited이면 차단하지 않음
                    if (lastRemain === null) return;

                    if (lastRemain === 0) {
                        e.preventDefault();
                        restoreSubmitUI(); // ✅ quota로 막혔을 때 submit-lock UI 복구
                        showToast("오늘 이미지 검색은 0회 남아 있어요. 내일 다시 시도해 주세요.");
                        return false;
                    }
                },
                true
            );
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
